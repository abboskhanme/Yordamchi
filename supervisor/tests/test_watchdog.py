"""Watchdog (SPEC §2.8, NFR-REL-01): dead process, stale heartbeat, crash loop, update suppression, sleep and wake,
and offline versus dead (T32b): poll attempts, network outages, other poll failures, and files from before T32b; and
the banner on the Mac after 10 min offline (2026-10-27).

Time is virtual. `run_for` ticks every 15 s, like production, because a longer gap between ticks means the Mac slept.
"""

import json
import os
import unittest
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.notify import OSASCRIPT, SCRIPT, Notifier
from yordamchi_supervisor.proc import RunResult
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.testing import (
    GROUP_ID,
    START,
    TOKEN,
    UID,
    FakeClock,
    FakeJob,
    FakeLaunchctl,
    FakeOpener,
    TempData,
)
from yordamchi_supervisor.watchdog import Heartbeat, Watchdog, read_heartbeat

DEV = "uz.yordamchi.dev"
PERSONAL = "uz.yordamchi.personal"
KICK_DEV = ["/bin/launchctl", "kickstart", "-k", f"gui/{UID}/{DEV}"]


class FakeOsascript:
    """A `Runner` for `osascript`: records each banner's argv and answers with `returncode`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.returncode = 0

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return RunResult(returncode=self.returncode)

    def banners(self) -> list[str]:
        return [call[-1] for call in self.calls]


class WatchdogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.clock = FakeClock(START)
        self.launchctl = FakeLaunchctl()
        self.opener = FakeOpener()
        self.osascript = FakeOsascript()
        self.timeline: list[tuple[datetime, str]] = []
        self.watchdog = self.build()

    def build(self) -> Watchdog:
        alerter = Alerter(
            self.data.paths.env_file("dev"),
            self.data.log,
            self.data.scrubber,
            self.clock,
            keychain=self.data.keychain,
            opener=self.opener,
        )
        return Watchdog(
            self.data.paths,
            LaunchCtl(self.launchctl, UID),
            alerter,
            self.data.log,
            self.data.scrubber,
            StateFile(self.data.paths.state_file),
            notifier=Notifier(self.osascript, self.data.log),
        )

    def tick(self, **advance: float) -> datetime:
        now = self.clock.advance(**advance) if advance else self.clock.now()
        before = len(self.opener.sent)
        self.watchdog.tick(now)
        self.timeline.extend((now, text) for text in self.opener.texts()[before:])
        return now

    def run_for(self, seconds: int, *, beat: str | None = None) -> None:
        """One tick every 15 s. With `beat`, the bot writes a fresh heartbeat (that `started_at`) before each tick."""
        for _ in range(seconds // 15):
            self.clock.advance(seconds=15)
            if beat is not None:
                self.heartbeat(started_at=beat, pid=self.launchctl.jobs[DEV].pid or 0)
            self.tick()

    def heartbeat(self, *, started_at: str = "2026-10-24T09:00:00+00:00", pid: int = 4242) -> None:
        self.data.write_heartbeat("dev", at=self.clock.now(), pid=pid, started_at=started_at)

    def touch(self, path: Path) -> None:
        stamp = self.clock.now().timestamp()
        os.utime(path, (stamp, stamp))

    def write_update(self, update_id: str, status: str | None) -> None:
        directory = self.data.paths.updates / update_id
        directory.mkdir(parents=True)
        (directory / "request.json").write_text(json.dumps({"id": update_id, "kind": "app"}), encoding="utf-8")
        self.touch(directory / "request.json")
        if status is not None:
            self.set_update_status(update_id, status)

    def set_update_status(self, update_id: str, status: str) -> None:
        path = self.data.paths.updates / update_id / "status.json"
        path.write_text(json.dumps({"status": status}), encoding="utf-8")
        self.touch(path)

    def events(self, name: str) -> list[dict[str, object]]:
        return [event for event in self.data.log_events() if event["event"] == name]

    def loop_alerts(self) -> list[tuple[datetime, str]]:
        return [(at, text) for at, text in self.timeline if "crash loop" in text]


class InstalledLabels(WatchdogCase):
    def test_labels_that_are_not_loaded_are_left_alone(self) -> None:
        self.tick()
        self.run_for(300)
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])
        printed = {call[-1] for call in self.launchctl.calls}
        self.assertEqual(printed, {f"gui/{UID}/{DEV}", f"gui/{UID}/{PERSONAL}"})

    def test_only_the_loaded_bot_is_acted_on(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.tick()
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])

    def test_a_launchctl_that_does_not_answer_causes_no_action(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.launchctl.hang = True
        self.tick()
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])
        self.assertIn("launchctl_no_answer", self.data.log_text())

    def test_every_launchctl_call_has_a_timeout(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.tick()
        self.assertTrue(self.launchctl.timeouts)
        self.assertTrue(all(0 < timeout <= 60 for timeout in self.launchctl.timeouts))


class DeadProcess(WatchdogCase):
    def test_a_missing_process_is_kickstarted_and_the_owner_is_told(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.tick()
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertEqual(len(self.opener.sent), 1)
        sent = self.opener.sent[0]
        self.assertEqual(sent.body["chat_id"], GROUP_ID)
        self.assertEqual(set(sent.body), {"chat_id", "text"})  # plain text: no parse_mode, no reply_markup
        self.assertIn(S.BOT_RESTARTED(bot="Dev bot", reason=S.REASON_PROCESS_MISSING()), self.opener.texts()[0])
        self.assertTrue(self.opener.texts()[0].startswith("[Supervisor 15:00] "))

    def test_no_second_kickstart_within_30_s(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.launchctl.kick_fails = True
        self.tick()
        self.run_for(15)
        self.assertEqual(len(self.launchctl.kicks()), 1)
        self.run_for(15)
        self.assertEqual(len(self.launchctl.kicks()), 2)

    def test_a_failed_kickstart_is_reported_as_such(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=None)
        self.launchctl.kick_fails = True
        self.tick()
        self.assertEqual(len(self.opener.sent), 1)
        self.assertIn("qayta ishga tushira olmadim", self.opener.texts()[0])
        self.assertIn(f"gui/$(id -u)/{DEV}", self.opener.texts()[0])
        self.assertIn("bot_kickstart_failed", self.data.log_text())


class StaleHeartbeat(WatchdogCase):
    def setUp(self) -> None:
        super().setUp()
        self.launchctl.jobs[DEV] = FakeJob(pid=4242)

    def test_a_fresh_heartbeat_needs_nothing(self) -> None:
        self.heartbeat()
        self.tick()
        self.run_for(600, beat="s1")
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])

    def test_a_heartbeat_older_than_120_s_is_kickstarted(self) -> None:
        self.heartbeat()
        self.tick()
        self.run_for(120)
        self.assertEqual(self.launchctl.kicks(), [])  # exactly 120 s: still fine
        self.run_for(15)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])  # 135 s
        self.assertIn(S.REASON_HEARTBEAT_STALE(seconds=135), self.opener.texts()[0])

    def test_a_process_seen_for_the_first_time_gets_120_s(self) -> None:
        # The heartbeat on disk is a day old (the Mac rebooted); the bot launchd just started has not polled yet.
        self.data.write_heartbeat("dev", at=START - timedelta(days=1), pid=1, started_at="old")
        self.tick()
        self.run_for(120)
        self.assertEqual(self.launchctl.kicks(), [])
        self.run_for(15)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])

    def test_after_a_kickstart_the_new_process_gets_120_s(self) -> None:
        self.heartbeat()
        self.tick()
        self.run_for(135)
        self.assertEqual(len(self.launchctl.kicks()), 1)
        self.run_for(105)  # the old heartbeat is still on disk; the new process has not polled yet
        self.assertEqual(len(self.launchctl.kicks()), 1)
        self.run_for(300, beat="2026-10-24T10:02:15+00:00")
        self.assertEqual(len(self.launchctl.kicks()), 1)

    def test_a_paused_bot_that_keeps_writing_is_not_restarted(self) -> None:
        # Paused (drain or probation): last_poll_ok_at is old, but the file is rewritten every 30 s (SPEC §2.8).
        path = self.data.paths.heartbeat("dev")
        body = {"pid": 4242, "started_at": "s1", "last_poll_ok_at": "2026-10-24T08:00:00+00:00", "mode": "paused"}
        self.tick()
        for _ in range(20):
            path.write_text(json.dumps(body), encoding="utf-8")
            self.touch(path)
            self.run_for(30)
        self.assertEqual(self.launchctl.kicks(), [])


class SleepAndWake(WatchdogCase):
    def test_waking_from_sleep_gives_the_bot_time_before_any_restart(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=4242)
        self.heartbeat()
        self.tick()
        self.tick(hours=3)  # the Mac slept; the heartbeat is 3 h old
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertIn("wake_detected", self.data.log_text())
        self.run_for(120)
        self.assertEqual(self.launchctl.kicks(), [])
        self.run_for(15)  # 135 s after waking and still no heartbeat: polling is dead
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])


class CrashLoop(WatchdogCase):
    def setUp(self) -> None:
        super().setUp()
        self.launchctl.jobs[DEV] = FakeJob(pid=4242, runs=1)
        self.data.paths.last_error("dev").write_text(
            f"first failure\nRuntimeError: polling failed at https://api.telegram.org/bot{TOKEN}/getUpdates\n\n",
            encoding="utf-8",
        )

    def crash_and_respawn(self, n: int, *, then_run: int) -> None:
        """launchd's KeepAlive restarts the bot; the new process polls and writes heartbeats with its own started_at."""
        self.launchctl.crash(DEV, respawn=True)
        self.run_for(then_run, beat=f"start-{n}")

    def test_three_starts_in_10_min_send_one_alert_with_the_last_error(self) -> None:
        self.heartbeat(started_at="start-0")
        self.tick()
        self.run_for(60, beat="start-0")
        for n in (1, 2):
            self.crash_and_respawn(n, then_run=120)
        self.assertEqual(self.loop_alerts(), [])
        self.crash_and_respawn(3, then_run=15)
        alerts = [text for _, text in self.loop_alerts()]
        self.assertEqual(len(alerts), 1)
        self.assertIn("3 marta", alerts[0])
        self.assertIn("RuntimeError: polling failed", alerts[0])  # the last non-empty line
        self.assertNotIn(TOKEN, alerts[0])
        self.assertNotIn("first failure", alerts[0])

    def test_a_loop_alerts_at_most_once_per_30_min(self) -> None:
        self.heartbeat(started_at="start-0")
        self.tick()
        for n in range(1, 31):  # a crash every 2 min for an hour
            self.crash_and_respawn(n, then_run=120)
        alert_times = [at for at, _ in self.loop_alerts()]
        self.assertEqual(len(alert_times), 2)
        self.assertGreaterEqual(alert_times[1] - alert_times[0], timedelta(minutes=30))
        self.assertLess(alert_times[1] - alert_times[0], timedelta(minutes=31))
        self.assertEqual(self.launchctl.kicks(), [])  # launchd restarted it every time; nothing to kick
        self.assertEqual(len(self.timeline), 2)  # and no other alert reached the owner

    def test_starts_that_never_wrote_a_heartbeat_count_too(self) -> None:
        # A refused start (exit 78) dies before its first heartbeat; only launchd's `runs` counter moves.
        self.tick()
        for _ in range(3):
            self.launchctl.crash(DEV, respawn=True)
            self.run_for(60)
        self.assertEqual(len(self.loop_alerts()), 1)

    def test_starts_spread_over_more_than_10_min_are_not_a_loop(self) -> None:
        self.heartbeat(started_at="start-0")
        self.tick()
        for n in range(1, 6):
            self.crash_and_respawn(n, then_run=360)
        self.assertEqual(self.loop_alerts(), [])
        self.assertEqual(self.timeline, [])

    def test_the_restart_alerts_stay_quiet_while_a_loop_alert_is_active(self) -> None:
        self.tick()
        for _ in range(3):
            self.launchctl.crash(DEV, respawn=True)
            self.run_for(60)
        self.assertEqual(len(self.loop_alerts()), 1)
        self.launchctl.crash(DEV, respawn=False)
        self.run_for(15)
        self.assertEqual(len(self.launchctl.kicks()), 1)  # it is still restarted ...
        self.assertEqual(len(self.timeline), 1)  # ... but the owner already knows

    def test_the_30_min_window_survives_a_supervisor_restart(self) -> None:
        self.tick()
        for _ in range(3):
            self.launchctl.crash(DEV, respawn=True)
            self.run_for(60)
        self.assertEqual(len(self.loop_alerts()), 1)
        self.watchdog = self.build()  # the supervisor itself was restarted
        self.run_for(15)
        for _ in range(3):
            self.launchctl.crash(DEV, respawn=True)
            self.run_for(60)
        self.assertEqual(len(self.loop_alerts()), 1)


class UpdateSuppression(WatchdogCase):
    def setUp(self) -> None:
        super().setUp()
        self.launchctl.jobs[DEV] = FakeJob(pid=None)

    def test_silent_while_an_update_is_switching(self) -> None:
        self.write_update("u1", "switching")
        self.tick()
        self.run_for(300)
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])
        self.assertIn("watchdog_suppressed", self.data.log_text())

    def test_silent_from_the_request_until_a_terminal_status(self) -> None:
        self.write_update("u1", None)  # requested: no status.json yet
        self.tick()
        for status in ("building", "draining", "switching", "probation"):
            self.set_update_status("u1", status)
            self.run_for(60)
        self.assertEqual(self.launchctl.kicks(), [])
        self.set_update_status("u1", "rolled_back")
        self.run_for(15)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])

    def test_restarts_during_an_update_never_count_as_a_crash_loop(self) -> None:
        self.launchctl.jobs[DEV] = FakeJob(pid=4242)
        self.heartbeat()
        self.tick()
        self.write_update("u1", "switching")
        for _ in range(4):
            self.launchctl.crash(DEV, respawn=True)
            self.run_for(30)
        self.set_update_status("u1", "healthy")
        self.run_for(300, beat="new-version")
        self.assertEqual(self.timeline, [])
        self.assertEqual(self.launchctl.kicks(), [])

    def test_an_update_untouched_for_2_h_does_not_mute_the_watchdog(self) -> None:
        self.write_update("u1", "draining")
        old = (self.clock.now() - timedelta(hours=3)).timestamp()
        for name in ("request.json", "status.json"):
            os.utime(self.data.paths.updates / "u1" / name, (old, old))
        self.tick()
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertIn("update_stale_ignored", self.data.log_text())


# --- T32b: offline is not dead ---------------------------------------------------------------------------------------
class PollCase(WatchdogCase):
    """A bot from T32b on: after every `getUpdates` attempt it writes when it tried and how that went."""

    def setUp(self) -> None:
        super().setUp()
        self.launchctl.jobs[DEV] = FakeJob(pid=4242)
        self.started = "s1"
        self.kind = "none"
        self.failures = 0
        self.attempt_at: datetime | None = None
        self.ok_at: datetime | None = None

    def attempt(self, kind: str = "none") -> None:
        """One attempt now, recorded the way the app's polling hook records it."""
        now = self.clock.now()
        self.kind, self.attempt_at = kind, now
        if kind == "none":
            self.ok_at, self.failures = now, 0
        else:
            self.failures += 1
        self.rewrite()

    def rewrite(self) -> None:
        """The file is written again now with no new attempt in it (the app's 30 s timer)."""
        self.data.write_poll_heartbeat(
            "dev",
            at=self.clock.now(),
            pid=self.launchctl.jobs[DEV].pid or 0,
            started_at=self.started,
            attempt_at=self.attempt_at,
            kind=self.kind,
            failures=self.failures,
            ok_at=self.ok_at,
        )

    def poll_for(self, seconds: int, kind: str, *, every: int = 30) -> None:
        """Attempts of `kind` every `every` s (30 s is the longest backoff), and a watchdog tick every 15 s."""
        for step in range(1, seconds // 15 + 1):
            self.clock.advance(seconds=15)
            if step * 15 % every == 0:
                self.attempt(kind)
            self.tick()

    def respawned(self, n: int) -> None:
        """launchd's KeepAlive started a new process; it shows itself before its first attempt."""
        self.launchctl.crash(DEV, respawn=True)
        self.started, self.kind, self.failures, self.attempt_at, self.ok_at = f"start-{n}", "none", 0, None, None
        self.rewrite()


class Offline(PollCase):
    def test_a_30_minute_outage_with_fresh_attempts_restarts_nothing_and_alerts_nobody_on_telegram(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(1800, "network")
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])
        [offline] = self.events("bot_offline")
        self.assertEqual(offline["failures"], 1)

        self.poll_for(30, "none")
        [back] = self.events("bot_back_online")
        self.assertEqual(back["offline_s"], 1800)
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])

    def test_attempts_as_sparse_as_the_runner_makes_them_are_still_a_live_loop(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(1800, "network", every=45)  # a 30 s backoff, then a probe that hangs for its 10 s budget
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])

    def test_a_stuck_loop_is_kickstarted_even_while_its_file_stays_fresh(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(60, "network")
        for _ in range(8):  # no attempt any more, but the file is still written
            self.clock.advance(seconds=15)
            self.rewrite()
            self.tick()
        self.assertEqual(self.launchctl.kicks(), [])  # exactly 120 s after the last attempt
        self.clock.advance(seconds=15)
        self.rewrite()
        self.tick()
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertIn(S.REASON_HEARTBEAT_STALE(seconds=135), self.opener.texts()[0])
        self.assertEqual(self.events("bot_kickstarted")[0]["reason"], "poll_loop_stuck")
        self.assertEqual(self.events("bot_offline_ended")[0]["cause"], "poll_loop_stuck")

    def test_a_missing_process_is_kickstarted_at_once_while_offline(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(300, "network")
        self.launchctl.crash(DEV, respawn=False)
        self.tick(seconds=15)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertIn(S.REASON_PROCESS_MISSING(), self.opener.texts()[0])
        self.assertEqual(self.events("bot_offline_ended")[0]["cause"], "process_missing")

    def test_starts_while_offline_never_count_toward_a_crash_loop(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(60, "network")
        for n in range(1, 5):  # four restarts in 8 min, all during the outage
            self.respawned(n)
            self.tick(seconds=15)
            self.poll_for(105, "network")
        self.assertEqual(self.timeline, [])
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(len(self.events("bot_offline")), 1)  # a restarted process is still offline, not back

        self.poll_for(30, "none")
        for n in range(5, 7):  # back online: two more starts inside the same 10 min are not a loop ...
            self.respawned(n)
            self.poll_for(60, "none")
        self.assertEqual(self.loop_alerts(), [])
        self.respawned(7)  # ... and a third is: counting resumed
        self.poll_for(60, "none")
        self.assertEqual(len(self.loop_alerts()), 1)


class OfflineBanner(PollCase):
    """Telegram cannot carry the news of its own outage: after 10 min offline, one banner on the Mac per outage."""

    def go_offline(self) -> None:
        """A healthy poll, then the first network failure: the watchdog sees the bot offline from now."""
        self.attempt()
        self.tick()
        self.poll_for(30, "network")

    def test_one_banner_after_10_min_offline_and_no_second_one_in_the_same_outage(self) -> None:
        self.go_offline()
        self.poll_for(585, "network")
        self.assertEqual(self.osascript.calls, [])
        self.poll_for(15, "network")
        self.assertEqual(self.osascript.banners(), [S.BOTS_OFFLINE(bots="Dev bot", minutes=10)])
        self.poll_for(3600, "network")
        self.assertEqual(len(self.osascript.calls), 1)
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])

    def test_an_outage_shorter_than_10_min_shows_nothing(self) -> None:
        self.go_offline()
        self.poll_for(570, "network")
        self.poll_for(30, "none")
        self.poll_for(1800, "none")
        self.assertEqual(self.osascript.calls, [])

    def test_the_next_outage_gets_its_own_banner(self) -> None:
        self.go_offline()
        self.poll_for(600, "network")
        self.poll_for(30, "none")
        self.poll_for(30, "network")
        self.poll_for(600, "network")
        self.assertEqual(len(self.osascript.calls), 2)

    def test_a_restart_during_the_outage_does_not_start_it_again(self) -> None:
        self.go_offline()
        self.poll_for(300, "network")
        self.respawned(1)
        self.tick(seconds=15)
        self.poll_for(300, "network")
        self.assertEqual(len(self.osascript.calls), 1)

    def test_the_texts_reach_osascript_as_arguments_never_as_script(self) -> None:
        self.go_offline()
        self.poll_for(600, "network")
        [call] = self.osascript.calls
        script = [call[i + 1] for i, item in enumerate(call) if item == "-e"]
        self.assertEqual(call[0], OSASCRIPT)
        self.assertEqual(script, list(SCRIPT))
        self.assertEqual(call[-3:], ["--", S.MAC_TITLE(), S.BOTS_OFFLINE(bots="Dev bot", minutes=10)])
        self.assertTrue(all(0 < timeout <= 60 for timeout in self.osascript.timeouts))
        [shown] = self.events("mac_notified")
        self.assertEqual(shown["kind"], "bots_offline")
        self.assertNotIn("Telegram'ga", self.data.log_text())

    def test_a_banner_that_fails_is_logged_and_not_retried(self) -> None:
        self.osascript.returncode = 1
        self.go_offline()
        self.poll_for(1200, "network")
        self.assertEqual(len(self.osascript.calls), 1)
        [failed] = self.events("mac_notify_failed")
        self.assertEqual(failed["returncode"], 1)


class OfflineBannerBothBots(PollCase):
    """Both bots lose Telegram together: one banner names both."""

    def setUp(self) -> None:
        super().setUp()
        self.launchctl.jobs[PERSONAL] = FakeJob(pid=4343)

    def rewrite(self) -> None:
        super().rewrite()
        self.data.write_poll_heartbeat(
            "personal",
            at=self.clock.now(),
            pid=self.launchctl.jobs[PERSONAL].pid or 0,
            started_at="p1",
            attempt_at=self.attempt_at,
            kind=self.kind,
            failures=self.failures,
            ok_at=self.ok_at,
        )

    def test_one_banner_names_both_bots(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(30, "network")
        self.poll_for(1800, "network")
        self.assertEqual(self.osascript.banners(), [S.BOTS_OFFLINE(bots="Dev bot va Shaxsiy bot", minutes=10)])


class OtherPollFailures(PollCase):
    def check_restarted_120_s_after_the_failures_began(self, kind: str) -> None:
        self.attempt()
        self.tick()
        self.poll_for(150, kind)  # the first failure was seen 30 s in
        self.assertEqual(self.launchctl.kicks(), [])
        self.poll_for(15, kind)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertIn(S.REASON_HEARTBEAT_STALE(seconds=135), self.opener.texts()[0])
        self.assertEqual(self.events("bot_kickstarted")[0]["reason"], "polls_failing")
        self.assertEqual(self.events("bot_offline"), [])

    def test_a_revoked_token_or_a_conflict_is_restarted_as_before(self) -> None:
        self.check_restarted_120_s_after_the_failures_began("other")

    def test_a_kind_this_watchdog_does_not_know_is_judged_the_same_way(self) -> None:
        self.check_restarted_120_s_after_the_failures_began("dns_v2")

    def test_another_failure_right_after_an_outage_gets_its_own_120_s(self) -> None:
        self.attempt()
        self.tick()
        self.poll_for(1800, "network")
        self.poll_for(90, "other")  # a 409 while Telegram still holds the long poll from before the outage
        self.poll_for(30, "none")
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.opener.sent, [])
        self.assertEqual(self.events("bot_offline_ended")[0]["cause"], "other")


class OlderFormats(PollCase):
    def test_a_file_without_attempt_fields_is_judged_by_its_mtime_alone(self) -> None:
        # A bot from before T32b: whatever its content says, a rewritten file is fresh and a silent one is stale.
        self.heartbeat(started_at="s1")
        self.tick()
        for _ in range(8):
            self.run_for(30)
            self.heartbeat(started_at="s1")
        self.assertEqual(self.launchctl.kicks(), [])
        self.run_for(135)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertEqual(self.events("bot_kickstarted")[0]["reason"], "heartbeat_stale")
        self.assertEqual(self.events("bot_offline"), [])

    def test_a_paused_bot_is_judged_by_its_mtime_even_in_the_new_shape(self) -> None:
        def paused() -> None:  # the last attempt failed on the network, then polling was paused on purpose
            self.data.write_poll_heartbeat(
                "dev", at=self.clock.now(), pid=4242, started_at="s1", attempt_at=START, kind="network", mode="paused"
            )

        paused()
        self.tick()
        for _ in range(20):  # 10 min paused: no attempt, the timer rewrites the file every 30 s
            self.run_for(30)
            paused()
        self.assertEqual(self.launchctl.kicks(), [])
        self.assertEqual(self.events("bot_offline"), [])

    def test_a_process_that_has_not_polled_yet_is_judged_by_its_mtime(self) -> None:
        self.rewrite()  # the start's first write: no attempt yet
        self.tick()
        self.run_for(120)
        self.assertEqual(self.launchctl.kicks(), [])
        self.run_for(15)
        self.assertEqual(self.launchctl.kicks(), [KICK_DEV])
        self.assertEqual(self.events("bot_kickstarted")[0]["reason"], "heartbeat_stale")

    def test_damaged_poll_fields_fall_back_to_the_mtime_rule(self) -> None:
        path = self.data.paths.heartbeat("dev")
        body = {"pid": 4242, "mode": "normal", "last_poll_attempt_at": "soon", "last_poll_error_kind": "network"}
        path.write_text(json.dumps(body), encoding="utf-8")
        self.touch(path)
        beat = read_heartbeat(path)
        assert beat is not None
        self.assertTrue(beat.not_polled_yet)
        self.assertFalse(beat.offline(self.clock.now()))
        self.assertEqual(beat.alive_at(), (START, "heartbeat_stale"))

    def test_an_attempt_is_never_taken_as_later_than_the_file_write(self) -> None:
        beat = Heartbeat(
            written_at=START,
            pid=4242,
            started_at="s1",
            mode="normal",
            last_poll_attempt_at=START + timedelta(hours=1),
            last_poll_error_kind="network",
        )
        self.assertEqual(beat.alive_at(), (START, "poll_loop_stuck"))


if __name__ == "__main__":
    unittest.main()
