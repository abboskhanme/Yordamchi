"""The self-updater (SPEC §3.2.12, UC-19) on temp dirs and virtual time: the three broken-update fixtures, `kill -9`
mid-switch, DB equality after a failed migration, 0 lost owner messages, the test-count rule, R-32 (never the app and
the supervisor together) and the supervisor's own self-switch. git, uv, make, launchd and the bots are simulated
(`testing_update`); every file the updater touches is real."""

import json
import os
import stat
import unittest
from datetime import timedelta

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.releases import RELEASE_MARKER, link_target
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.testing import START, UID, FakeClock, FakeOpener, TempData
from yordamchi_supervisor.testing_update import (
    BASE_SHA,
    Behaviour,
    FakeBots,
    FakeTelegram,
    FakeTools,
    InlineWorker,
    dump,
    install_release,
    make_bot_db,
    offset,
    processed,
    version_of,
)
from yordamchi_supervisor.updater import (
    INTERVAL,
    SANDBOX_EXEC,
    Job,
    Journal,
    Step,
    StepResult,
    Updater,
    collected_count,
    failure_summary,
    parse_request,
    unittest_count,
)

NEW_SHA = "b" * 40
BRANCH = "selfupdate/7-ovoz-sozlamasi"
UPDATE_ID = "u000007"
APP_CHANGE = {"src/yordamchi/dev/selfupdate/domain.py": "# new\n"}
SUPERVISOR_CHANGE = {"supervisor/src/yordamchi_supervisor/updater.py": "# new\n"}
GUARD = (SANDBOX_EXEC, "-p", "(version 1)")


class StalledWorker:
    def run(self, key: str, job: Job) -> StepResult | None:
        return None


class Killed(Exception):
    """`kill -9` of the supervisor at a checkpoint."""


class CrashingUpdater(Updater):
    crash_at = ""

    def checkpoint(self, name: str) -> None:
        if name == self.crash_at:
            raise Killed(name)


class World:
    """One Mac: the data dir, an installed release, the dev bot loaded and polling, a working copy, Telegram."""

    def __init__(self, case: unittest.TestCase, *, previous_tests: dict[str, int] | None = None) -> None:
        self.data = TempData()
        case.addCleanup(self.data.cleanup)
        self.paths = self.data.paths
        self.clock = FakeClock(START)
        self.opener = FakeOpener()
        self.telegram = FakeTelegram()
        self.bots = FakeBots(self.paths, self.clock, self.telegram)
        self.tools = FakeTools()
        self.tools.add(BASE_SHA, Behaviour(), parent=None, branch="main")
        self.previous = install_release(self.paths.releases, self.paths.current_release, BASE_SHA, Behaviour())
        install_release(
            self.paths.supervisor_releases,
            self.paths.supervisor_current,
            BASE_SHA,
            Behaviour(),
            treeish=f"{BASE_SHA}:supervisor",
        )
        self.db = self.paths.db_file("dev")
        self.telegram.send(5)
        make_bot_db(self.db, offset=6, processed=[1, 2, 3, 4, 5])
        self.bots.load("dev")
        self.state = StateFile(self.paths.state_file)
        if previous_tests is not None:
            self.state.save_section(
                "update", {"last_good": {"app": {"version": self.previous.name, "tests": previous_tests}}}
            )
        self.alerter = Alerter(
            self.paths.env_file("dev"), self.data.log, self.data.scrubber, self.clock, opener=self.opener
        )
        self.updater = self.new_updater()
        self.bots.step(self.clock.now())

    def new_updater(self, cls: type[Updater] = Updater, *, release: str = "") -> Updater:
        """A supervisor (re)start: a fresh process reading the same state file."""
        self.state = StateFile(self.paths.state_file)
        return cls(
            self.paths,
            runner=self.tools,
            launchctl=LaunchCtl(self.bots.launchctl, UID),
            selftester=self.bots,
            alerter=self.alerter,
            log=self.data.log,
            scrubber=self.data.scrubber,
            state=self.state,
            worker=InlineWorker(),
            release=release,
            network_guard=GUARD,
        )

    # --- the dev bot's side ---------------------------------------------------------------------------------------
    def request(
        self,
        behaviour: Behaviour,
        *,
        kind: str = "app",
        change: dict[str, str] | None = None,
        sha: str = NEW_SHA,
        health_timeout_s: int = 120,
    ) -> None:
        self.tools.add(sha, behaviour, extra=change if change is not None else APP_CHANGE, branch=BRANCH, kind=kind)
        self.tools.head = BRANCH
        directory = self.paths.update_dir(UPDATE_ID)
        directory.mkdir(parents=True, exist_ok=True)
        body = {
            "id": UPDATE_ID,
            "kind": kind,
            "commit": sha,
            "branch": BRANCH,
            "health_timeout_s": health_timeout_s,
            "drain_max_min": 30,
        }
        (directory / "request.json").write_text(json.dumps(body), encoding="utf-8")

    # --- time -----------------------------------------------------------------------------------------------------
    def tick(self, updater: Updater | None = None) -> None:
        self.bots.step(self.clock.now())
        (updater or self.updater).tick(self.clock.now())
        self.clock.advance(seconds=INTERVAL.total_seconds())

    def run(self, *, ticks: int = 200, updater: Updater | None = None, during: int = 0) -> dict[str, object]:
        """Tick until the status is final; `during` owner messages arrive on every tick meanwhile."""
        for _ in range(ticks):
            if during:
                self.telegram.send(during)
            self.tick(updater)
            status = self.status()
            if status.get("final"):
                for _ in range(3):  # the bots work off what Telegram held
                    self.tick(updater)
                return status
        raise AssertionError(f"no final status after {ticks} ticks: {self.status()}")

    def run_until(self, step: Step, *, ticks: int = 100, updater: Updater | None = None) -> Journal:
        for _ in range(ticks):
            self.tick(updater)
            journal = Journal.from_json(StateFile(self.paths.state_file).section("update").get("journal"))
            if journal is not None and journal.step == step.value:
                return journal
        raise AssertionError(f"never reached {step}")

    # --- reading ----------------------------------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        path = self.paths.update_dir(UPDATE_ID) / "status.json"
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        return raw

    def current(self) -> str | None:
        return link_target(self.paths.current_release)

    def beat(self) -> dict[str, object]:
        raw = json.loads(self.paths.heartbeat("dev").read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        return raw

    def kickstarts(self) -> int:
        return len(self.bots.launchctl.kicks())


# --- the healthy path ---------------------------------------------------------------------------------------------
class HealthyUpdate(unittest.TestCase):
    def test_build_test_drain_switch_probation_then_merge(self) -> None:
        world = World(self, previous_tests={"app": 110, "supervisor": 99})
        world.request(Behaviour(app_tests=121))
        status = world.run(during=1)

        new = version_of(NEW_SHA)
        self.assertEqual((status["status"], status["final"], status["result_code"]), ("merged", True, ""))
        self.assertEqual(status["tests"], {"app": 121, "supervisor": 100})
        self.assertEqual(status["previous_tests"], {"app": 110, "supervisor": 99})
        self.assertEqual(world.current(), new)
        self.assertEqual((world.beat()["version"], world.beat()["mode"]), (new, "normal"))
        release = world.paths.releases / new
        self.assertTrue((release / RELEASE_MARKER).exists())
        self.assertFalse(os.access(release / "FIXTURE.json", os.W_OK))  # chmod -R a-w
        for flag in (world.paths.drain_flag, world.paths.probation_flag, world.paths.drained_marker("dev")):
            self.assertFalse(flag.exists(), flag.name)
        self.assertFalse((world.paths.update_dir(UPDATE_ID) / "check").exists())  # the test workspace is gone
        self.assertTrue(world.tools.guarded, "make check ran with the network closed")
        self.assertTrue(all(cmd[0].endswith(("make", "uv")) for cmd in world.tools.guarded))
        self.assertEqual(world.tools.refs["main"], NEW_SHA)  # fast-forwarded
        self.assertNotIn(BRANCH, world.tools.refs)  # the branch is deleted
        self.assertEqual(world.tools.head, "main")
        self.assertEqual(world.tools.pushed, [])  # no origin: nothing is pushed
        good = world.updater.last_good("app")
        self.assertEqual((good["version"], good["tests"]), (new, {"app": 121, "supervisor": 100}))
        self.assertEqual(world.opener.sent, [])  # the dev bot reports a healthy update, not the supervisor
        latest = json.loads(world.paths.latest_update.read_text(encoding="utf-8"))
        self.assertEqual(latest["status"], "merged")
        self.assertIsNone(StateFile(world.paths.state_file).section("update").get("journal"))

    def test_zero_owner_messages_are_lost_and_probation_only_peeks(self) -> None:
        world = World(self)
        world.bots.unanswered = 4  # probation lasts a few ticks
        world.request(Behaviour())
        held: set[int] = set()
        steps: list[str] = []
        for _ in range(60):
            world.telegram.send(1)  # the owner keeps writing, through drain, switch and probation
            world.tick()
            journal = Journal.from_json(StateFile(world.paths.state_file).section("update").get("journal"))
            if journal is not None and journal.step in ("snapshot", "migrating", "switching", "probation"):
                held.add(offset(world.db))
                steps.append(journal.step)
            if world.status().get("final"):
                break
        self.assertIn("probation", steps)
        self.assertEqual(len(held), 1, "the offset never moves between the drain and healthy")
        self.assertGreater(world.telegram.peeks, 0)
        for _ in range(3):
            world.tick()
        self.assertEqual(processed(world.db), world.telegram.sent)

    def test_status_names_a_main_that_moved_and_leaves_the_branch(self) -> None:
        world = World(self)
        world.request(Behaviour())
        world.tools.add("c" * 40, Behaviour(), parent=BASE_SHA)
        world.tools.refs["main"] = "c" * 40  # the owner committed to main meanwhile
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("healthy", "merge_not_ff"))
        self.assertEqual(world.tools.refs["main"], "c" * 40)
        self.assertIn(BRANCH, world.tools.refs)

    def test_an_origin_gets_main_and_loses_the_branch(self) -> None:
        world = World(self)
        world.tools.origin = True
        world.request(Behaviour())
        world.run()
        self.assertEqual([cmd[1:] for cmd in world.tools.pushed], [["origin", "main"], ["origin", "--delete", BRANCH]])

    def test_a_bot_still_in_probation_after_the_flag_is_cleared_is_restarted_once(self) -> None:
        world = World(self)
        world.request(Behaviour())
        world.run_until(Step.FINISHING)
        world.tick()  # healthy: flags cleared
        process = world.bots.processes["dev"]
        world.paths.probation_flag.write_text(json.dumps({"version": process.version}), encoding="utf-8")
        before = world.kickstarts()
        status = world.run()
        self.assertEqual(status["status"], "merged")
        self.assertEqual(world.kickstarts(), before + 1)


# --- the three broken-update fixtures (UC-19) ---------------------------------------------------------------------
class BrokenUpdates(unittest.TestCase):
    def assert_rolled_back(self, world: World, status: dict[str, object], code: str) -> None:
        self.assertEqual((status["status"], status["final"], status["result_code"]), ("rolled_back", True, code))
        self.assertEqual(world.current(), world.previous.name)
        self.assertEqual((world.beat()["version"], world.beat()["mode"]), (world.previous.name, "normal"))
        self.assertIs(status["repair_needed"], False)
        texts = world.opener.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn(S.update_reason(code), texts[0])
        self.assertIn(world.previous.name, texts[0])
        self.assertEqual(processed(world.db), world.telegram.sent, "0 owner messages lost")
        for flag in (world.paths.drain_flag, world.paths.probation_flag):
            self.assertFalse(flag.exists())
        self.assertEqual(world.tools.head, "main")  # the working copy is back on main
        self.assertIn(BRANCH, world.tools.refs)  # the branch stays for a fix

    def test_crash_at_import_rolls_back_early(self) -> None:
        world = World(self)
        world.request(Behaviour(bot="crash_at_import"))
        world.run_until(Step.PROBATION)
        started = world.clock.now()
        status = world.run(during=1)
        self.assert_rolled_back(world, status, "crash_loop")
        self.assertLess(world.clock.now() - started, timedelta(minutes=3))  # back on the last good within 3 min

    def test_polling_never_starts_rolls_back_at_the_health_timeout(self) -> None:
        world = World(self)
        world.request(Behaviour(bot="no_polling"), health_timeout_s=60)
        journal = world.run_until(Step.PROBATION)
        status = world.run(during=1)
        self.assert_rolled_back(world, status, "polling_not_started")
        switched = journal.switched_at
        self.assertTrue(switched)

    def test_failing_migration_restores_the_db_exactly(self) -> None:
        world = World(self)
        world.request(Behaviour(migration="fail"))
        before = dump(world.db)
        world.run_until(Step.SNAPSHOT)
        snapshot_dump = dump(world.db)
        status = world.run()
        self.assert_rolled_back(world, status, "migration_failed")
        self.assertTrue(status["migration_ran"])
        copy = world.paths.pre_update_backup(UPDATE_ID) / "dev.db"
        world.tick()
        restored = dump(world.db)
        self.assertNotIn("half_done", "\n".join(restored))
        self.assertEqual(snapshot_dump, before)
        self.assertEqual(dump(copy), before)
        self.assertEqual(restored, before)  # schema and every row, after the old bot ran on it again
        self.assertEqual(world.kickstarts(), 0)  # never switched: bootout, restore, bootstrap
        self.assertIn(S.ROLLED_BACK_DB(), world.opener.texts()[0])

    def test_failing_migration_db_equals_the_pre_update_copy_before_the_bot_runs(self) -> None:
        world = World(self)
        world.request(Behaviour(migration="fail"))
        world.run_until(Step.MIGRATING)
        world.tick()  # the migration fails; rollback restores at once
        copy = world.paths.pre_update_backup(UPDATE_ID) / "dev.db"
        self.assertEqual(dump(world.db), dump(copy))

    def test_a_failing_test_is_refused_and_nothing_is_switched(self) -> None:
        world = World(self)
        world.request(Behaviour(failing=("tests/unit/dev/gate/test_domain.py::test_push_main_is_forbidden",)))
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("refused", "tests_failed"))
        self.assertIn("test_push_main_is_forbidden", str(status["detail"]))
        self.assertEqual(world.current(), world.previous.name)
        self.assertFalse((world.paths.releases / version_of(NEW_SHA)).exists())  # the refused build is deleted
        self.assertFalse(world.paths.drain_flag.exists())
        self.assertEqual(world.kickstarts(), 0)
        self.assertEqual(world.opener.sent, [])
        self.assertEqual(world.tools.head, "main")

    def test_a_failed_selftest_rolls_back(self) -> None:
        world = World(self)
        world.request(Behaviour(bot="selftest_fails"))
        status = world.run()
        self.assert_rolled_back(world, status, "selftest_failed")
        self.assertIn("outbox", str(status["detail"]))

    def test_a_release_that_ignores_probation_is_rolled_back(self) -> None:
        world = World(self)
        world.request(Behaviour(bot="ignores_probation"))
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("rolled_back", "probation_ignored"))
        self.assertEqual(world.current(), world.previous.name)

    def test_when_the_last_good_also_fails_the_owner_gets_manual_repair_steps(self) -> None:
        world = World(self)
        world.request(Behaviour(bot="crash_at_import"))
        world.run_until(Step.VERIFYING)
        (world.previous / "FIXTURE.json").chmod(0o644)
        (world.previous / "FIXTURE.json").write_text(Behaviour(bot="no_polling").to_json(), encoding="utf-8")
        world.bots.kill("dev")
        status = world.run()
        self.assertEqual((status["status"], status["repair_needed"]), ("rolled_back", True))
        self.assertIn("qo'lda tiklash", world.opener.texts()[0])
        self.assertIn(f"backups/pre-update/{UPDATE_ID}", world.opener.texts()[0])


# --- kill -9 -------------------------------------------------------------------------------------------------------
class KillNine(unittest.TestCase):
    def test_kill_9_mid_switch_recovers_on_the_next_supervisor_start(self) -> None:
        world = World(self)
        world.request(Behaviour(migration="ok"))
        doomed = world.new_updater(CrashingUpdater)
        assert isinstance(doomed, CrashingUpdater)
        doomed.crash_at = "after_swap"
        snapshot = None
        for _ in range(100):
            world.bots.step(world.clock.now())
            try:
                doomed.tick(world.clock.now())
            except Killed:
                break
            world.clock.advance(seconds=5)
            copy = world.paths.pre_update_backup(UPDATE_ID) / "dev.db"
            if snapshot is None and copy.exists():
                snapshot = dump(copy)
        else:
            self.fail("never reached the switch")
        self.assertEqual(world.current(), version_of(NEW_SHA))  # the link moved, then the supervisor died
        world.telegram.send(3)
        restarted = world.new_updater()
        status = world.run(updater=restarted, during=1)
        self.assertEqual((status["status"], status["result_code"]), ("rolled_back", "supervisor_restarted"))
        self.assertEqual(world.current(), world.previous.name)
        self.assertEqual(world.beat()["version"], world.previous.name)
        self.assertEqual(processed(world.db), world.telegram.sent)
        self.assertNotIn("self_updates", "\n".join(dump(world.db)))  # the migration was undone with the copy

    def test_kill_9_of_the_bot_during_probation_still_ends_healthy(self) -> None:
        world = World(self)
        world.request(Behaviour())
        world.run_until(Step.PROBATION)
        world.bots.kill("dev")
        status = world.run()
        self.assertEqual(status["status"], "merged")

    def test_kill_9_during_the_build_rebuilds_on_restart(self) -> None:
        world = World(self)
        world.request(Behaviour())
        stalled = world.new_updater()
        stalled._worker = StalledWorker()  # the build never returns: the process is killed meanwhile
        world.tick(stalled)
        half = world.paths.releases / version_of(NEW_SHA)
        half.mkdir(parents=True)
        (half / "partial").write_text("x", encoding="utf-8")  # what a killed `uv sync` leaves
        restarted = world.new_updater()
        status = world.run(updater=restarted)
        self.assertEqual(status["status"], "merged")
        self.assertFalse((half / "partial").exists())
        self.assertTrue((half / RELEASE_MARKER).exists())


# --- refusals before anything runs -----------------------------------------------------------------------------------
class Refusals(unittest.TestCase):
    def test_a_dropped_test_count_is_refused(self) -> None:
        world = World(self, previous_tests={"app": 130, "supervisor": 100})
        world.request(Behaviour(app_tests=125))
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("refused", "tests_dropped"))
        self.assertIn("app 125 < 130", str(status["detail"]))
        self.assertEqual(world.current(), world.previous.name)

    def test_an_update_touching_the_supervisor_and_the_app_is_refused(self) -> None:
        world = World(self)
        world.request(Behaviour(), change={**APP_CHANGE, **SUPERVISOR_CHANGE})
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("refused", "split_needed"))
        self.assertFalse((world.paths.releases / version_of(NEW_SHA)).exists())

    def test_an_app_request_that_changes_the_supervisor_is_refused(self) -> None:
        world = World(self)
        world.request(Behaviour(), change=SUPERVISOR_CHANGE)
        self.assertEqual(world.run()["result_code"], "kind_mismatch")

    def test_a_commit_the_working_copy_does_not_have_is_refused(self) -> None:
        world = World(self)
        world.request(Behaviour())
        del world.tools.commits[NEW_SHA]
        self.assertEqual(world.run()["result_code"], "commit_missing")

    def test_a_broken_build_is_refused(self) -> None:
        world = World(self)
        world.request(Behaviour(build="fail"))
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("refused", "build_failed"))
        self.assertIn("uv_sync_failed", str(status["detail"]))

    def test_a_malformed_request_fails_without_touching_anything(self) -> None:
        world = World(self)
        directory = world.paths.update_dir(UPDATE_ID)
        directory.mkdir(parents=True)
        (directory / "request.json").write_text('{"id": "u000007", "kind": "both"}', encoding="utf-8")
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("failed", "bad_request"))

    def test_with_no_bot_loaded_nothing_is_switched_blind(self) -> None:
        world = World(self)
        world.request(Behaviour())
        del world.bots.launchctl.jobs["uz.yordamchi.dev"]
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("failed", "bots_not_loaded"))
        self.assertEqual(world.current(), world.previous.name)

    def test_a_drain_that_never_ends_fails_and_lifts_the_drain(self) -> None:
        world = World(self)
        world.request(Behaviour())
        world.run_until(Step.DRAINING)
        world.tick()  # the drain flag goes out
        world.bots.launchctl.jobs["uz.yordamchi.dev"].pid = None  # a bot that cannot drain (it is gone)
        world.bots.processes.clear()
        world.paths.drained_marker("dev").unlink(missing_ok=True)
        world.clock.advance(minutes=61)
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("failed", "drain_timeout"))
        self.assertFalse(world.paths.drain_flag.exists())
        self.assertEqual(world.current(), world.previous.name)


# --- kind=supervisor ------------------------------------------------------------------------------------------------
class SupervisorSelfSwitch(unittest.TestCase):
    def test_the_new_supervisor_confirms_itself(self) -> None:
        world = World(self)
        world.request(Behaviour(), kind="supervisor", change=SUPERVISOR_CHANGE)
        for _ in range(20):
            world.tick()
            if world.updater.exit_requested:
                break
        self.assertTrue(world.updater.exit_requested)
        new = version_of(NEW_SHA)
        self.assertEqual(link_target(world.paths.supervisor_current), new)
        self.assertEqual(world.current(), world.previous.name)  # the app is untouched
        self.assertEqual(world.status()["status"], "switching")
        self.assertEqual(world.kickstarts(), 0)
        successor = world.new_updater(release=new)
        status = world.run(updater=successor)
        self.assertEqual((status["status"], status["final"]), ("merged", True))
        self.assertEqual(successor.last_good("supervisor")["version"], new)

    def test_a_supervisor_switched_back_by_the_dev_bot_reports_the_rollback(self) -> None:
        world = World(self)
        world.request(Behaviour(), kind="supervisor", change=SUPERVISOR_CHANGE)
        for _ in range(20):
            world.tick()
            if world.updater.exit_requested:
                break
        old = version_of(BASE_SHA)
        install_release(
            world.paths.supervisor_releases,
            world.paths.supervisor_current,
            BASE_SHA,
            Behaviour(),
            treeish=f"{BASE_SHA}:supervisor",
        )  # what the dev bot's watcher does after 60 s without a heartbeat of the new release
        status = world.run(updater=world.new_updater(release=old))
        self.assertEqual((status["status"], status["result_code"]), ("rolled_back", "supervisor_not_started"))
        self.assertIn(version_of(NEW_SHA), world.opener.texts()[0])

    def test_a_supervisor_whose_trial_run_fails_is_refused(self) -> None:
        world = World(self)
        world.request(Behaviour(trial="fail"), kind="supervisor", change=SUPERVISOR_CHANGE)
        status = world.run()
        self.assertEqual((status["status"], status["result_code"]), ("refused", "trial_failed"))
        self.assertEqual(link_target(world.paths.supervisor_current), version_of(BASE_SHA))


# --- the small parts ------------------------------------------------------------------------------------------------
class Parsing(unittest.TestCase):
    def test_test_counts(self) -> None:
        self.assertEqual(collected_count("a\nb\n\n1234 tests collected in 2.10s\n"), 1234)
        self.assertEqual(collected_count("1200/1234 tests collected (34 deselected) in 3.0s"), 1200)
        self.assertIsNone(collected_count("no tests ran"))
        self.assertEqual(unittest_count("....\n------\nRan 99 tests in 0.311s\n\nOK\n"), 99)

    def test_failure_summary_names_tests_then_the_stage(self) -> None:
        output = "FAILED tests/a.py::t1 - x\nFAILED tests/a.py::t2 - y\nmake: *** [test] Error 1"
        self.assertEqual(failure_summary(output), "tests/a.py::t1, tests/a.py::t2")
        self.assertEqual(failure_summary("src/x.py:3: error: bad\nmake: *** [typecheck] Error 1"), "make typecheck")

    def test_parse_request_bounds_the_timeouts_and_checks_the_shape(self) -> None:
        world = World(self)
        world.request(Behaviour(), health_timeout_s=10_000)
        request = parse_request(world.paths.update_dir(UPDATE_ID))
        assert not isinstance(request, str)
        self.assertEqual(request.health_timeout_s, 600)
        bad = world.paths.update_dir("x")
        bad.mkdir()
        (bad / "request.json").write_text(json.dumps({"id": "y", "kind": "app"}), encoding="utf-8")
        self.assertEqual(parse_request(bad), "bad_request")

    def test_status_files_are_private(self) -> None:
        world = World(self)
        world.request(Behaviour())
        world.run()
        mode = stat.S_IMODE((world.paths.update_dir(UPDATE_ID) / "status.json").stat().st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
