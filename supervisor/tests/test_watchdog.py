"""Watchdog (SPEC §2.8, NFR-REL-01): dead process, stale heartbeat, crash loop, update suppression, sleep and wake.

Time is virtual. `run_for` ticks every 15 s, like production, because a longer gap between ticks means the Mac slept.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.launchd import LaunchCtl
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
from yordamchi_supervisor.watchdog import Watchdog

DEV = "uz.yordamchi.dev"
PERSONAL = "uz.yordamchi.personal"
KICK_DEV = ["/bin/launchctl", "kickstart", "-k", f"gui/{UID}/{DEV}"]


class WatchdogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.clock = FakeClock(START)
        self.launchctl = FakeLaunchctl()
        self.opener = FakeOpener()
        self.timeline: list[tuple[datetime, str]] = []
        self.watchdog = self.build()

    def build(self) -> Watchdog:
        alerter = Alerter(
            self.data.paths.env_file("dev"), self.data.log, self.data.scrubber, self.clock, opener=self.opener
        )
        return Watchdog(
            self.data.paths,
            LaunchCtl(self.launchctl, UID),
            alerter,
            self.data.log,
            self.data.scrubber,
            StateFile(self.data.paths.state_file),
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

    def loop_alerts(self) -> list[tuple[datetime, str]]:
        return [(at, text) for at, text in self.timeline if "crash loop" in text]

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


if __name__ == "__main__":
    unittest.main()
