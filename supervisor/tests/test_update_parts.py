"""The self-updater's parts: release folders, the selftest socket client, the worker thread, `--selftest`, the
heartbeat's `release`, the command runner's timeout and `launchctl bootout`/`bootstrap`."""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from yordamchi_supervisor.control import SocketSelfTester, read_selftest_answer
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.main import Supervisor, main, own_release
from yordamchi_supervisor.paths import SupervisorPaths
from yordamchi_supervisor.proc import SubprocessCommandRunner
from yordamchi_supervisor.releases import (
    RELEASE_MARKER,
    UV_SYNC_RELEASE,
    build_release,
    link_target,
    marker_commit,
    prune,
    swap_symlink,
)
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
from yordamchi_supervisor.testing_update import BASE_SHA, Behaviour, FakeTools
from yordamchi_supervisor.updater import StepResult, ThreadWorker


class Releases(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tools = FakeTools()
        self.tools.add(BASE_SHA, Behaviour(), parent=None, branch="main")

    def build(self, target: Path) -> bool:
        built = build_release(
            self.tools, repo=self.root / "repo", treeish=BASE_SHA, target=target, home=self.root, env={}
        )
        return built.ok

    def test_a_release_uses_the_installer_flags_and_ends_read_only(self) -> None:
        target = self.root / "releases" / "2026.10.24-aaaaaaa"
        self.assertTrue(self.build(target))
        sync = next(call for call in self.tools.calls if Path(call[0]).name == "uv")
        self.assertEqual(sync[1:], list(UV_SYNC_RELEASE))
        self.assertIn("--no-dev", sync)
        self.assertEqual(sync[sync.index("--link-mode") + 1], "clone")
        self.assertEqual(marker_commit(target), BASE_SHA)
        self.assertFalse(os.access(target / RELEASE_MARKER, os.W_OK))
        self.assertFalse(os.access(target, os.W_OK))

    def test_a_finished_release_is_reused_and_a_half_one_rebuilt(self) -> None:
        target = self.root / "releases" / "2026.10.24-aaaaaaa"
        self.assertTrue(self.build(target))
        calls = len(self.tools.calls)
        self.assertTrue(self.build(target))
        self.assertEqual(len(self.tools.calls), calls)  # nothing ran again
        other = self.root / "releases" / "half"
        other.mkdir()
        (other / "junk").write_text("x", encoding="utf-8")
        self.assertTrue(self.build(other))
        self.assertFalse((other / "junk").exists())

    def test_symlinks_are_relative_and_swapped_atomically(self) -> None:
        a, b = self.root / "releases" / "a", self.root / "releases" / "b"
        a.mkdir(parents=True)
        b.mkdir()
        link = self.root / "releases" / "current"
        swap_symlink(link, a)
        swap_symlink(link, b)
        self.assertEqual(str(link.readlink()), "b")
        self.assertEqual(link_target(link), "b")

    def test_prune_keeps_the_newest_and_the_protected(self) -> None:
        directory = self.root / "releases"
        for index, name in enumerate(["r1", "r2", "r3", "r4", "r5"]):
            (directory / name).mkdir(parents=True)
            os.utime(directory / name, (1000 + index, 1000 + index))
        (directory / "r1").chmod(0o500)
        removed = prune(directory, keep=2, protect={"r1"})
        self.assertEqual(sorted(removed), ["r2", "r3"])
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["r1", "r4", "r5"])


class SelftestSocket(unittest.TestCase):
    def test_answers_are_read_strictly(self) -> None:
        self.assertFalse(read_selftest_answer(None, "n").answered)
        self.assertTrue(read_selftest_answer(b'{"ok": true, "nonce": "n"}', "n").ok)
        wrong = read_selftest_answer(b'{"ok": true, "nonce": "other"}', "n")
        self.assertEqual((wrong.answered, wrong.ok, wrong.failed_step), (True, False, "nonce"))
        failed = read_selftest_answer(b'{"ok": false, "nonce": "n", "steps": {"access": true, "outbox": false}}', "n")
        self.assertEqual((failed.ok, failed.failed_step), (False, "outbox"))
        self.assertEqual(read_selftest_answer(b"[1]", "n").failed_step, "malformed")

    def test_a_real_unix_socket_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:  # short path: unix socket names are limited
            root = Path(tmp)
            paths = SupervisorPaths(home=root, data=root / "d", logs=root / "l")
            paths.run.mkdir(parents=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(paths.socket("dev")))
            server.listen(1)
            seen: list[dict[str, object]] = []

            def answer() -> None:
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.recv(4096).decode())
                    seen.append(request)
                    reply = {"ok": True, "nonce": request["nonce"], "steps": {"access": True}}
                    connection.sendall(json.dumps(reply).encode() + b"\n")

            thread = threading.Thread(target=answer)
            thread.start()
            result = SocketSelfTester(paths, timeout=5).selftest("dev", "abc")
            thread.join()
            server.close()
            self.assertTrue(result.ok)
            self.assertEqual(seen, [{"method": "selftest", "nonce": "abc"}])
            self.assertFalse(SocketSelfTester(paths, timeout=1).selftest("personal", "abc").answered)


class Worker(unittest.TestCase):
    def test_a_job_runs_once_on_a_thread_and_a_crash_is_a_failed_step(self) -> None:
        worker = ThreadWorker()
        gate = threading.Event()
        runs: list[int] = []

        def job() -> StepResult:
            runs.append(1)
            gate.wait(5)
            return StepResult(ok=True, counts={"app": 3})

        self.assertIsNone(worker.run("u1:test", job))
        self.assertIsNone(worker.run("u1:test", job))  # still running: not started twice
        gate.set()
        result = None
        for _ in range(200):
            result = worker.run("u1:test", job)
            if result is not None:
                break
            time.sleep(0.01)
        self.assertEqual((result, len(runs)), (StepResult(ok=True, counts={"app": 3}), 1))

        def broken() -> StepResult:
            raise OSError("disk")

        self.assertIsNone(worker.run("u1:build", broken))
        for _ in range(200):
            result = worker.run("u1:build", broken)
            if result is not None:
                break
            time.sleep(0.01)
        self.assertEqual(result, StepResult(ok=False, code="internal_error", detail="OSError"))


class SupervisorWiring(unittest.TestCase):
    def test_the_heartbeat_names_the_release_and_the_updater_is_a_job(self) -> None:
        data = TempData()
        self.addCleanup(data.cleanup)
        supervisor = Supervisor(
            data.paths,
            clock=FakeClock(START),
            log=data.log,
            scrubber=data.scrubber,
            runner=FakeLaunchctl(),
            spawner=FakeSpawner(),
            opener=FakeOpener(),
            keychain=data.keychain,
            uid=UID,
            pid=4321,
            release="2026.10.24-abcdef0",
        )
        self.assertIn("updater", [job.name for job in supervisor.jobs])
        supervisor.tick()
        beat = json.loads(data.paths.heartbeat("supervisor").read_text(encoding="utf-8"))
        self.assertEqual(beat["release"], "2026.10.24-abcdef0")
        self.assertFalse(supervisor.switching_out())

    def test_own_release_reads_the_venv_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "releases" / "2026.10.24-abcdef0" / ".venv"
            venv.mkdir(parents=True)
            link = Path(tmp) / "current"
            link.symlink_to(venv.parent)
            self.assertEqual(own_release(str(link / ".venv")), "2026.10.24-abcdef0")
            self.assertEqual(own_release(tmp), "")

    def test_selftest_runs_one_inert_tick_and_exits_0(self) -> None:
        previous = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", previous)
        with tempfile.TemporaryDirectory() as tmp:
            environ = {"YORDAMCHI_DATA_DIR": str(Path(tmp) / "d"), "YORDAMCHI_LOG_DIR": str(Path(tmp) / "l")}
            out = io.StringIO()
            self.assertEqual(main(environ, home=Path(tmp), stream=out, argv=["--selftest"]), 0)
            self.assertIn("selftest_ok", out.getvalue())
            self.assertFalse((Path(tmp) / "d" / "run" / "supervisor.lock").exists())  # never the real lock


class Tools(unittest.TestCase):
    def test_a_command_over_its_timeout_is_killed_with_its_group(self) -> None:
        started = time.monotonic()
        result = SubprocessCommandRunner().run(["/bin/sleep", "5"], timeout=0.3)
        self.assertIsNone(result.returncode)
        self.assertLess(time.monotonic() - started, 4)
        ok = SubprocessCommandRunner().run(["/bin/echo", "hi"], timeout=5, env={"PATH": "/bin"})
        self.assertEqual((ok.ok, ok.text()), (True, "hi"))

    def test_bootout_and_bootstrap(self) -> None:
        fake = FakeLaunchctl()
        fake.jobs["uz.yordamchi.dev"] = FakeJob(pid=10, runs=4)
        launchctl = LaunchCtl(fake, UID)
        self.assertTrue(launchctl.bootout("uz.yordamchi.dev"))
        status = launchctl.status("uz.yordamchi.dev")
        self.assertIsNotNone(status)
        assert status is not None
        self.assertFalse(status.loaded)
        self.assertTrue(launchctl.bootout("uz.yordamchi.dev"))  # not loaded counts as done
        self.assertTrue(launchctl.bootstrap("/Users/o/Library/LaunchAgents/uz.yordamchi.dev.plist"))
        status = launchctl.status("uz.yordamchi.dev")
        assert status is not None
        self.assertEqual((status.loaded, status.runs), (True, 5))
        self.assertFalse(launchctl.bootstrap("/Users/o/Library/LaunchAgents/uz.yordamchi.dev.plist"))


if __name__ == "__main__":
    unittest.main()
