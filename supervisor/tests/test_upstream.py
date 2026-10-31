"""Upstream updates (SPEC §3.2.12 "Upstream updates"): the `upstream` job fetches `origin main` every 3 days and
queues a self-update when the running release is strictly behind; the updater finishes it without a push and moves
the local `main` only as a safe fast-forward. git, uv, make, launchd and the bots are simulated (`testing_update`)."""

import json
import unittest

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.releases import link_target
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.testing_update import APP_CHANGE, BASE_SHA, SUPERVISOR_CHANGE, Behaviour, World, version_of
from yordamchi_supervisor.updater import INTERVAL, UPSTREAM, Updater, UpdateRequest, parse_request
from yordamchi_supervisor.upstream import CHECK_EVERY, RETRY_AFTER, STATE_SECTION, Upstream

GITHUB_SHA = "d" * 40
LOCAL_SHA = "c" * 40


class Mac:
    """A fresh install from the GitHub clone: `origin` exists, the release runs `BASE_SHA`, the copy is on `main`."""

    def __init__(self, case: unittest.TestCase, *, origin: bool = True) -> None:
        self.world = World(case)
        self.world.tools.origin = origin
        self.world.tools.upstream = BASE_SHA  # GitHub's `main` is what was installed
        self.restart()

    def restart(self, *, release: str = "") -> None:
        """A supervisor (re)start: the updater and the upstream job share one fresh state file, as in `main.py`."""
        self.updater = self.world.new_updater(release=release)
        self.upstream = Upstream(
            self.world.paths,
            runner=self.world.tools,
            alerter=self.world.alerter,
            log=self.world.data.log,
            state=self.world.state,
        )

    def github(self, behaviour: Behaviour, change: dict[str, str], *, sha: str = GITHUB_SHA, kind: str = "app") -> None:
        """A new commit on GitHub's `main`; the working copy has it only once it fetches."""
        self.world.tools.add(sha, behaviour, extra=change, kind=kind)
        self.world.tools.upstream = sha

    def tick(self) -> None:
        self.upstream.tick(self.world.clock.now())

    def section(self) -> dict[str, object]:
        return StateFile(self.world.paths.state_file).section(STATE_SECTION)

    def requests(self) -> list[str]:
        root = self.world.paths.updates
        return sorted(p.name for p in root.iterdir() if (p / "request.json").exists()) if root.is_dir() else []

    def status(self, update_id: str) -> dict[str, object]:
        path = self.world.paths.update_dir(update_id) / "status.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        assert isinstance(raw, dict)
        return raw

    def run(self, update_id: str, *, updater: Updater | None = None, ticks: int = 200) -> dict[str, object]:
        for _ in range(ticks):
            self.world.tick(updater or self.updater)
            status = self.status(update_id)
            if status.get("final"):
                for _ in range(3):
                    self.world.tick(updater or self.updater)
                return status
        raise AssertionError(f"no final status for {update_id}: {self.status(update_id)}")

    def events(self, name: str) -> list[dict[str, object]]:
        return [event for event in self.world.data.log_events() if event.get("event") == name]


class WhenToCheck(unittest.TestCase):
    def test_the_first_tick_checks_and_the_next_one_waits_three_days(self) -> None:
        mac = Mac(self)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 1)
        mac.world.clock.advance(seconds=CHECK_EVERY.total_seconds() - 60)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 1, "not due yet")
        mac.world.clock.advance(seconds=60)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 2)

    def test_a_restart_does_not_check_again(self) -> None:
        mac = Mac(self)
        mac.tick()
        mac.restart()
        mac.world.clock.advance(hours=5)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 1)

    def test_without_origin_nothing_is_fetched_and_the_next_look_is_in_three_days(self) -> None:
        mac = Mac(self, origin=False)
        mac.tick()
        self.assertEqual(mac.world.tools.fetches, [])
        self.assertEqual(len(mac.events("upstream_no_origin")), 1)
        mac.world.clock.advance(hours=1)
        mac.tick()
        self.assertEqual(len(mac.events("upstream_no_origin")), 1)
        self.assertEqual(mac.requests(), [])

    def test_a_failed_fetch_is_logged_never_alerted_and_retried_after_an_hour(self) -> None:
        mac = Mac(self)
        mac.world.tools.fetch_fails = True
        mac.github(Behaviour(), APP_CHANGE)
        mac.tick()
        mac.world.clock.advance(seconds=RETRY_AFTER.total_seconds() - 60)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 1)
        self.assertEqual(mac.section()["failures"], 1)
        mac.world.clock.advance(seconds=60)
        mac.tick()
        self.assertEqual(len(mac.world.tools.fetches), 2)
        self.assertEqual(mac.section()["failures"], 2)
        self.assertEqual(mac.world.opener.sent, [])
        self.assertEqual(mac.requests(), [])
        mac.world.tools.fetch_fails = False
        mac.world.clock.advance(seconds=RETRY_AFTER.total_seconds())
        mac.tick()
        self.assertEqual(mac.section()["failures"], 0)
        self.assertEqual(len(mac.requests()), 1)

    def test_the_fetch_never_prompts(self) -> None:
        mac = Mac(self)
        mac.tick()
        env = mac.world.tools.fetches[0]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])
        fetch = next(call for call in mac.world.tools.calls if call[3:4] == ["fetch"])
        self.assertEqual(fetch[-2:], ["origin", "+refs/heads/main:refs/remotes/origin/main"])

    def test_an_update_in_progress_postpones_the_check(self) -> None:
        mac = Mac(self)
        mac.world.request(Behaviour())  # a chat self-update the dev bot queued
        mac.tick()
        self.assertEqual(mac.world.tools.fetches, [])
        self.assertNotIn("next_at", mac.section())


class WhatToQueue(unittest.TestCase):
    def test_github_equal_to_the_release_queues_nothing(self) -> None:
        mac = Mac(self)
        mac.world.tools.upstream = BASE_SHA
        mac.tick()
        self.assertEqual(mac.requests(), [])
        self.assertIs(mac.section()["pending"], False)

    def test_github_ahead_writes_one_app_request_the_updater_accepts(self) -> None:
        mac = Mac(self)
        mac.github(Behaviour(), APP_CHANGE)
        mac.tick()
        mac.tick()
        (update_id,) = mac.requests()
        self.assertTrue(update_id.startswith(f"upstream-app-{GITHUB_SHA[:7]}-"))
        request = parse_request(mac.world.paths.update_dir(update_id))
        self.assertIsInstance(request, UpdateRequest)
        assert isinstance(request, UpdateRequest)
        self.assertEqual((request.kind, request.commit, request.source), ("app", GITHUB_SHA, UPSTREAM))
        self.assertEqual(request.branch, f"selfupdate/upstream-{GITHUB_SHA[:12]}")
        self.assertEqual(mac.section()["queued"], update_id)

    def test_a_local_main_ahead_or_diverged_queues_nothing(self) -> None:
        for upstream in (BASE_SHA, GITHUB_SHA):  # GitHub behind the local commit, or beside it
            with self.subTest(upstream=upstream):
                mac = Mac(self)
                mac.world.tools.add(LOCAL_SHA, Behaviour(), extra=APP_CHANGE, branch="main")
                mac.github(Behaviour(), {"src/yordamchi/other.py": "# github\n"}, sha=GITHUB_SHA)
                mac.world.tools.upstream = upstream
                mac.tick()
                self.assertEqual(mac.requests(), [])
                self.assertEqual(mac.events("upstream_nothing_to_do")[-1]["reason"], "local_ahead")

    def test_a_clone_without_a_local_main_gets_one_at_github_and_updates(self) -> None:
        mac = Mac(self)
        mac.world.tools.refs["feature"] = mac.world.tools.refs.pop("main")
        mac.world.tools.head = "feature"
        mac.world.tools.dirty = True  # the owner's work on that branch is never touched
        mac.github(Behaviour(), APP_CHANGE)
        mac.tick()
        self.assertEqual(mac.world.tools.refs["main"], GITHUB_SHA)
        (update_id,) = mac.requests()
        self.assertEqual(mac.events("upstream_main_created")[-1]["commit"], GITHUB_SHA[:12])
        self.assertEqual(mac.run(update_id)["status"], "merged")
        self.assertEqual((mac.world.tools.head, mac.world.tools.refs["feature"]), ("feature", BASE_SHA))

    def test_a_running_release_beside_github_queues_nothing(self) -> None:
        mac = Mac(self)
        mac.world.tools.add(LOCAL_SHA, Behaviour(), extra=APP_CHANGE)
        state = mac.world.state.section("update")
        state["last_good"] = {"app": {"commit": LOCAL_SHA}}
        mac.world.state.save_section("update", state)
        (mac.world.previous / ".yordamchi-release.json").unlink()  # an old release without a marker: last_good
        mac.github(Behaviour(), {"src/yordamchi/other.py": "# github\n"})
        mac.tick()
        self.assertEqual(mac.requests(), [])
        self.assertEqual(mac.events("upstream_nothing_to_do")[-1]["reason"], "running_ahead_app")

    def test_a_supervisor_only_change_queues_only_the_supervisor(self) -> None:
        mac = Mac(self)
        mac.github(Behaviour(), SUPERVISOR_CHANGE, kind="supervisor")
        mac.tick()
        (update_id,) = mac.requests()
        self.assertTrue(update_id.startswith("upstream-supervisor-"))


class EndToEnd(unittest.TestCase):
    def test_a_mixed_range_updates_the_app_then_the_supervisor_without_a_push(self) -> None:
        mac = Mac(self)
        mac.github(Behaviour(), {**APP_CHANGE, **SUPERVISOR_CHANGE})
        mac.tick()
        (app_id,) = mac.requests()
        mac.tick()
        self.assertEqual(mac.requests(), [app_id], "the supervisor half waits for the app half")

        status = mac.run(app_id)
        self.assertEqual((status["status"], status["result_code"], status["source"]), ("merged", "", UPSTREAM))
        new = version_of(GITHUB_SHA)
        self.assertEqual(mac.world.current(), new)
        self.assertEqual(mac.world.tools.refs["main"], GITHUB_SHA)  # fast-forwarded
        self.assertEqual(mac.world.tools.pushed, [])  # origin exists, nothing is pushed
        self.assertEqual(link_target(mac.world.paths.supervisor_current), version_of(BASE_SHA))

        mac.tick()  # tells the owner, then queues the supervisor half
        self.assertTrue(
            any(t.endswith(S.UPSTREAM_UPDATED(part="botlar", version=new)) for t in mac.world.opener.texts())
        )
        (supervisor_id,) = [name for name in mac.requests() if name != app_id]
        self.assertTrue(supervisor_id.startswith("upstream-supervisor-"))
        for _ in range(40):
            mac.world.tick(mac.updater)
            if mac.updater.exit_requested:
                break
        self.assertTrue(mac.updater.exit_requested)
        mac.restart(release=new)  # launchd starts the new supervisor
        status = mac.run(supervisor_id)
        self.assertEqual(status["status"], "merged")
        self.assertEqual(link_target(mac.world.paths.supervisor_current), new)
        mac.tick()
        self.assertTrue(
            any(t.endswith(S.UPSTREAM_UPDATED(part="supervisor", version=new)) for t in mac.world.opener.texts())
        )
        mac.world.clock.advance(hours=1)
        mac.tick()
        self.assertEqual(len(mac.requests()), 2, "both sides are on GitHub's commit: nothing more")
        self.assertIs(mac.section()["pending"], False)
        self.assertEqual(mac.world.tools.pushed, [])

    def test_a_dirty_copy_on_main_keeps_main_and_the_new_release_runs(self) -> None:
        mac = Mac(self)
        mac.github(Behaviour(), APP_CHANGE)
        mac.tick()
        mac.world.tools.dirty = True
        status = mac.run(mac.requests()[0])
        self.assertEqual((status["status"], status["result_code"]), ("healthy", "copy_dirty"))
        self.assertEqual(mac.world.tools.refs["main"], BASE_SHA)
        self.assertEqual(mac.world.current(), version_of(GITHUB_SHA))
        mac.tick()
        reason = S.update_reason("copy_dirty")
        self.assertIn(S.UPSTREAM_MAIN_LEFT(reason=reason), mac.world.opener.texts()[-1])

    def test_on_another_branch_main_moves_by_update_ref(self) -> None:
        mac = Mac(self)
        mac.world.tools.refs["feature"] = BASE_SHA
        mac.world.tools.head = "feature"
        mac.world.tools.dirty = True  # the owner's work on that branch is never touched
        mac.github(Behaviour(), APP_CHANGE)
        mac.tick()
        status = mac.run(mac.requests()[0])
        self.assertEqual(status["status"], "merged")
        self.assertEqual(mac.world.tools.refs["main"], GITHUB_SHA)
        self.assertEqual(mac.world.tools.head, "feature")
        self.assertFalse(any(call[3:4] == ["merge"] for call in mac.world.tools.calls))

    def test_a_refused_update_is_told_once_and_held_until_github_moves_on(self) -> None:
        mac = Mac(self)
        mac.github(Behaviour(failing=("tests/unit/test_x.py::test_y",)), {**APP_CHANGE, **SUPERVISOR_CHANGE})
        mac.tick()
        status = mac.run(mac.requests()[0])
        self.assertEqual((status["status"], status["result_code"]), ("refused", "tests_failed"))
        mac.tick()
        mac.tick()
        text = S.UPSTREAM_NOT_APPLIED(version=version_of(GITHUB_SHA), reason=S.update_reason("tests_failed"))
        self.assertEqual([t.split("] ", 1)[1] for t in mac.world.opener.texts()], [text])
        self.assertEqual(len(mac.requests()), 1, "no retry of the same commit, no supervisor half")
        self.assertEqual(mac.world.current(), mac.world.previous.name)
        fixed = "e" * 40
        mac.github(Behaviour(), {"src/yordamchi/fix.py": "# fixed\n"}, sha=fixed)
        mac.world.clock.advance(seconds=CHECK_EVERY.total_seconds())
        mac.tick()
        self.assertEqual(len(mac.requests()), 2)

    def test_an_upstream_request_for_a_commit_github_lacks_is_refused(self) -> None:
        mac = Mac(self)
        mac.world.tools.add(LOCAL_SHA, Behaviour(), extra=APP_CHANGE)
        mac.world.tools.refs["origin/main"] = BASE_SHA
        directory = mac.world.paths.update_dir("upstream-app-ccccccc-1")
        directory.mkdir(parents=True)
        body = {"id": directory.name, "kind": "app", "commit": LOCAL_SHA, "branch": "selfupdate/upstream-x"}
        (directory / "request.json").write_text(json.dumps({**body, "source": UPSTREAM}), encoding="utf-8")
        status = mac.run(directory.name)
        self.assertEqual((status["status"], status["result_code"]), ("refused", "not_upstream"))
        self.assertEqual(mac.world.current(), mac.world.previous.name)

    def test_a_job_tick_is_cheap_when_nothing_is_due(self) -> None:
        mac = Mac(self)
        mac.tick()
        calls = len(mac.world.tools.calls)
        for _ in range(10):
            mac.world.clock.advance(seconds=INTERVAL.total_seconds() * 12)
            mac.tick()
        self.assertEqual(len(mac.world.tools.calls), calls)


if __name__ == "__main__":
    unittest.main()
