"""Composition, the job loop, paths, the single-instance lock, state and the string catalog."""

import ast
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import yordamchi_supervisor
from yordamchi_supervisor import strings as S
from yordamchi_supervisor.main import EXIT_ALREADY_RUNNING, Job, Supervisor, main, single_instance
from yordamchi_supervisor.paths import resolve
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.testing import (
    START,
    UID,
    FakeClock,
    FakeJob,
    FakeLaunchctl,
    FakeOpener,
    FakeSpawner,
    TempData,
)

SRC = Path(yordamchi_supervisor.__file__).resolve().parent


class SupervisorLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.clock = FakeClock(START)
        self.launchctl = FakeLaunchctl()
        self.spawner = FakeSpawner()
        self.opener = FakeOpener()
        self.supervisor = Supervisor(
            self.data.paths,
            clock=self.clock,
            log=self.data.log,
            scrubber=self.data.scrubber,
            runner=self.launchctl,
            spawner=self.spawner,
            opener=self.opener,
            uid=UID,
            pid=4321,
        )

    def test_one_tick_runs_every_job(self) -> None:
        self.supervisor.tick()
        heartbeat = json.loads(self.data.paths.heartbeat("supervisor").read_text(encoding="utf-8"))
        self.assertEqual(heartbeat["pid"], 4321)
        self.assertEqual(heartbeat["version"], yordamchi_supervisor.__version__)
        self.assertEqual(heartbeat["last_tick_at"], START.isoformat())
        self.assertEqual(self.spawner.spawned, [["/usr/bin/caffeinate", "-s", "-w", "4321"]])
        self.assertEqual(len(self.launchctl.calls), 2)  # print dev, print personal

    def test_jobs_run_on_their_own_intervals(self) -> None:
        for _ in range(12):  # one minute of 5 s loop steps
            self.supervisor.tick()
            self.clock.advance(seconds=5)
        self.assertEqual(len(self.launchctl.calls), 2 * 4)  # watchdog every 15 s

    def test_a_failing_job_is_contained_and_alerted_once_per_6_h(self) -> None:
        self.launchctl.jobs["uz.yordamchi.dev"] = FakeJob(pid=4242)
        calls: list[datetime] = []

        def broken(now: datetime) -> None:
            calls.append(now)
            raise OSError("disk went away")

        self.supervisor.jobs.insert(0, Job("broken", timedelta(seconds=15), broken))
        self.supervisor.tick()
        self.clock.advance(seconds=15)
        self.supervisor.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.data.log_text().count('"job_failed"'), 2)
        self.assertEqual(len(self.opener.sent), 1)
        self.assertIn(S.SUPERVISOR_ERROR(job="broken"), self.opener.texts()[0])
        self.assertTrue(self.data.paths.heartbeat("supervisor").exists())  # the other jobs still ran
        self.assertEqual(len(self.launchctl.calls), 4)

    def test_the_updater_seam_is_just_another_job(self) -> None:
        seen: list[datetime] = []
        self.supervisor.add_job(Job("updater", timedelta(seconds=5), seen.append))
        self.supervisor.tick()
        self.assertEqual(seen, [START])

    def test_run_forever_stops_on_the_event_and_stops_caffeinate(self) -> None:
        stop = threading.Event()
        self.supervisor.jobs.append(Job("stopper", timedelta(seconds=1), lambda now: stop.set()))
        self.supervisor.run_forever(stop)
        self.assertTrue(self.spawner.children[0].terminated)
        self.assertIn("supervisor_stopped", self.data.log_text())


class Paths(unittest.TestCase):
    def test_the_data_dir_defaults_to_application_support(self) -> None:
        paths, ignored = resolve({}, Path("/Users/owner"))
        self.assertEqual(paths.data, Path("/Users/owner/Library/Application Support/Yordamchi"))
        self.assertEqual(paths.log_file, Path("/Users/owner/Library/Logs/Yordamchi/supervisor.jsonl"))
        self.assertEqual(paths.heartbeat("dev"), paths.data / "run" / "dev.heartbeat.json")
        self.assertEqual(paths.launchd_log("personal").name, "uz.yordamchi.personal.launchd.log")
        self.assertEqual(ignored, [])

    def test_yordamchi_data_dir_overrides_it(self) -> None:
        paths, _ = resolve(
            {"YORDAMCHI_DATA_DIR": "/Volumes/x/yd", "YORDAMCHI_LOG_DIR": "/Volumes/x/yl"}, Path("/Users/owner")
        )
        self.assertEqual(paths.data, Path("/Volumes/x/yd"))
        self.assertEqual(paths.logs, Path("/Volumes/x/yl"))

    def test_the_old_spelling_is_reported_not_read(self) -> None:
        paths, ignored = resolve({"YORDAMCHI_DATA": "/Volumes/x/elsewhere"}, Path("/Users/owner"))
        self.assertEqual(paths.data, Path("/Users/owner/Library/Application Support/Yordamchi"))
        self.assertEqual(ignored, ["YORDAMCHI_DATA"])


class SingleInstance(unittest.TestCase):
    def test_a_second_supervisor_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            environ = {"YORDAMCHI_DATA_DIR": str(home / "data"), "YORDAMCHI_LOG_DIR": str(home / "logs")}
            previous = sys.excepthook
            self.addCleanup(setattr, sys, "excepthook", previous)
            with single_instance(home / "data" / "run" / "supervisor.lock") as held:
                self.assertTrue(held)
                code = main(environ, home=home, stream=io.StringIO())
            self.assertEqual(code, EXIT_ALREADY_RUNNING)
            self.assertIn("already_running", (home / "logs" / "supervisor.jsonl").read_text(encoding="utf-8"))
            with single_instance(home / "data" / "run" / "supervisor.lock") as held:
                self.assertTrue(held)  # released again


class State(unittest.TestCase):
    def test_sections_survive_a_reload_and_a_corrupt_file_is_moved_aside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "supervisor" / "state.json"
            StateFile(path).save_section("backups", {"last_ok_date": "2026-10-24"})
            self.assertEqual(StateFile(path).section("backups"), {"last_ok_date": "2026-10-24"})
            path.write_text("{half", encoding="utf-8")
            self.assertEqual(StateFile(path).section("backups"), {})
            self.assertTrue(path.with_name("state.json.corrupt").exists())


class Catalog(unittest.TestCase):
    def test_an_entry_refuses_missing_or_unknown_placeholders(self) -> None:
        with self.assertRaises(KeyError):
            S.BOT_RESTARTED(bot="Dev bot")
        with self.assertRaises(KeyError):
            S.NO_LAST_ERROR(extra="x")

    def test_every_entry_renders(self) -> None:
        for name in dir(S):
            entry = getattr(S, name)
            if isinstance(entry, S.Msg):
                text = entry(**dict.fromkeys(entry.params, "1"))
                self.assertNotIn("{", text, name)

    def test_no_inline_buttons_anywhere_in_the_supervisor(self) -> None:
        for path in sorted(SRC.rglob("*.py")):
            self.assertNotIn('reply_markup":', path.read_text(encoding="utf-8"), path.name)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "reply_markup":
                    self.fail(f"{path.name} builds a reply_markup")


if __name__ == "__main__":
    unittest.main()
