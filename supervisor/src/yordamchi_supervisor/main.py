"""Composition and the main loop of `uz.yordamchi.supervisor` (SPEC §2.1, §9.1): `python -I -m yordamchi_supervisor`.

The loop wakes every `LOOP_STEP` and runs each due `Job`:

| job | every | does |
|---|---|---|
| `heartbeat` | 15 s | `run/supervisor.heartbeat.json`: `pid`, `version`, `release`, `started_at`, `last_tick_at` |
| `caffeinate` | 15 s | keeps `caffeinate -s -w <pid>` running |
| `watchdog` | 15 s | dead process, stale heartbeat, crash loop, a Mac banner after 10 min offline (`watchdog.py`) |
| `alerts` | 30 s | retries alerts queued during a network drop |
| `backups` | 60 s | the daily 04:00 backup, when due (`backups.py`) |
| `log_trim` | 60 s | copy-truncates the launchd logs over 5 MB (`logtrim.py`) |
| `updater` | 5 s | the self-update journal: build, test, drain, switch, probation, rollback (`updater.py`) |
| `upstream` | 60 s | every 3 days: fetch `origin main`, queue a self-update when behind it (`upstream.py`) |

A job that raises is logged (`job_failed`) and alerted at most once per `JOB_ALERT_EVERY`; the other jobs keep
running. SIGTERM (launchd, `kickstart -k`) or SIGINT stops the loop within one step, then caffeinate is stopped.
A second supervisor (a manual run next to the launchd one) finds `run/supervisor.lock` held and exits with 75.

`release` is the name of the release folder this process runs from (`supervisor/releases/<release>/.venv`): a
supervisor self-switch is confirmed by the new process's first heartbeat carrying the new name. After the switch the
old process stops its loop (`Updater.exit_requested`) and exits 0; launchd's KeepAlive starts the new one.

`--selftest` (the updater's trial run of a freshly built supervisor release): build every part against a scratch data
dir with inert launchctl, caffeinate and Telegram, run one tick of every job, exit 0. An import error, a broken
constructor or a job that raises exits non-zero, so the switch never happens.
"""

import argparse
import contextlib
import fcntl
import json
import os
import signal
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Final, TextIO

from yordamchi_supervisor import __version__
from yordamchi_supervisor import strings as S
from yordamchi_supervisor.backups import Backups
from yordamchi_supervisor.caffeinate import Caffeinate
from yordamchi_supervisor.clock import Clock, SystemClock
from yordamchi_supervisor.control import SelfTester, SocketSelfTester
from yordamchi_supervisor.keychain import KeychainError, SecurityKeychain, TokenSource
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.log import JsonLog, Scrubber, install_excepthook
from yordamchi_supervisor.logtrim import trim_launchd_logs
from yordamchi_supervisor.notify import Notifier
from yordamchi_supervisor.paths import SupervisorPaths, resolve
from yordamchi_supervisor.proc import (
    Child,
    CommandResult,
    CommandRunner,
    PopenSpawner,
    Runner,
    RunResult,
    Spawner,
    SubprocessCommandRunner,
    SubprocessRunner,
)
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter, Opener, urllib_opener
from yordamchi_supervisor.updater import INTERVAL as UPDATER_INTERVAL
from yordamchi_supervisor.updater import ThreadWorker, Updater, Worker, default_network_guard
from yordamchi_supervisor.upstream import INTERVAL as UPSTREAM_INTERVAL
from yordamchi_supervisor.upstream import Upstream
from yordamchi_supervisor.watchdog import Watchdog

LOOP_STEP: Final = 5.0
JOB_ALERT_EVERY: Final = timedelta(hours=6)
EXIT_ALREADY_RUNNING: Final = 75  # EX_TEMPFAIL
EXIT_SELFTEST_FAILED: Final = 70  # EX_SOFTWARE


@dataclass(slots=True)
class Job:
    name: str
    every: timedelta
    run: Callable[[datetime], None]
    last_run: datetime | None = None
    last_alert: datetime | None = None
    failures: int = 0

    def due(self, now: datetime) -> bool:
        # A clock that went backwards (manual change) must not stall a job until it catches up.
        return self.last_run is None or now - self.last_run >= self.every or now < self.last_run


def write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def own_release(prefix: str = sys.prefix) -> str:
    """The release folder this interpreter's venv lives in (`supervisor/releases/<name>/.venv`), or ''."""
    venv = Path(prefix).resolve()
    return venv.parent.name if venv.name == ".venv" else ""


class Supervisor:
    def __init__(
        self,
        paths: SupervisorPaths,
        *,
        clock: Clock,
        log: JsonLog,
        scrubber: Scrubber,
        runner: Runner,
        spawner: Spawner,
        opener: Opener,
        keychain: TokenSource,
        uid: int,
        pid: int,
        command_runner: CommandRunner | None = None,
        selftester: SelfTester | None = None,
        worker: Worker | None = None,
        release: str = "",
        network_guard: Sequence[str] = (),
    ) -> None:
        self.paths = paths
        self.clock = clock
        self.log = log
        self.pid = pid
        self.release = release
        self.started_at = clock.now()
        self.state = StateFile(paths.state_file)
        self.alerter = Alerter(paths.env_file("dev"), log, scrubber, clock, keychain=keychain, opener=opener)
        self.caffeinate = Caffeinate(spawner, log, owner_pid=pid)
        launchctl = LaunchCtl(runner, uid)
        self.watchdog = Watchdog(
            paths, launchctl, self.alerter, log, scrubber, self.state, notifier=Notifier(runner, log)
        )
        self.backups = Backups(paths, self.alerter, log, self.state)
        commands = command_runner or SubprocessCommandRunner()
        self.updater = Updater(
            paths,
            runner=commands,
            launchctl=launchctl,
            selftester=selftester or SocketSelfTester(paths),
            alerter=self.alerter,
            log=log,
            scrubber=scrubber,
            state=self.state,
            worker=worker or ThreadWorker(),
            release=release,
            network_guard=network_guard,
        )
        self.upstream = Upstream(paths, runner=commands, alerter=self.alerter, log=log, state=self.state)
        self.jobs: list[Job] = [
            Job("heartbeat", timedelta(seconds=15), self.write_heartbeat),
            Job("caffeinate", timedelta(seconds=15), self.caffeinate.ensure),
            Job("watchdog", timedelta(seconds=15), self.watchdog.tick),
            Job("alerts", timedelta(seconds=30), lambda _now: self.alerter.flush()),
            Job("backups", timedelta(seconds=60), self.backups.tick),
            Job("log_trim", timedelta(seconds=60), lambda _now: trim_launchd_logs(paths, log)),
            Job("updater", UPDATER_INTERVAL, self.updater.tick),
            Job("upstream", UPSTREAM_INTERVAL, self.upstream.tick),
        ]

    def add_job(self, job: Job) -> None:
        self.jobs.append(job)

    def write_heartbeat(self, now: datetime) -> None:
        write_json_atomic(
            self.paths.heartbeat("supervisor"),
            {
                "pid": self.pid,
                "version": __version__,
                "release": self.release,
                "started_at": self.started_at.isoformat(),
                "last_tick_at": now.isoformat(),
            },
        )

    def tick(self) -> None:
        now = self.clock.now()
        for job in self.jobs:
            if not job.due(now):
                continue
            job.last_run = now
            try:
                job.run(now)
            except Exception as exc:  # noqa: BLE001 - one failing job must not stop the watchdog or the backups
                job.failures += 1
                self.log.error("job_failed", job=job.name, error=type(exc).__name__)
                if job.last_alert is None or now - job.last_alert >= JOB_ALERT_EVERY:
                    job.last_alert = now
                    self.alerter.send("supervisor_error", S.SUPERVISOR_ERROR(job=job.name))

    def switching_out(self) -> bool:
        """A supervisor self-switch swapped `supervisor/current`: this process steps aside for the new one."""
        return self.updater.exit_requested

    def run_forever(self, stop: threading.Event) -> None:
        self.log.info("supervisor_started", version=__version__, release=self.release, pid=self.pid)
        try:
            while not stop.is_set():
                self.tick()
                if self.switching_out():
                    self.log.info("supervisor_exit_for_switch")
                    break
                stop.wait(LOOP_STEP)
        finally:
            self.caffeinate.stop()
            self.log.info("supervisor_stopped", pending_alerts=self.alerter.pending)


@contextlib.contextmanager
def single_instance(lock_file: Path) -> Iterator[bool]:
    """True while this process holds `run/supervisor.lock`; False when another supervisor does."""
    lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# --- --selftest: one inert tick of everything -----------------------------------------------------------------------
class _InertRunner:
    """Answers every `launchctl` call as "no such service" (and `osascript` as failed): the trial never touches a real
    job and shows no banner."""

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        return RunResult(returncode=113)


class _InertCommands:
    def run(
        self, argv: Sequence[str], *, timeout: float, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        return CommandResult(returncode=1)


class _InertChild:
    pid = 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _InertSpawner:
    def spawn(self, argv: Sequence[str]) -> Child:
        return _InertChild()


def _inert_opener(request: urllib.request.Request, timeout: float) -> int:
    return 200


class _InertKeychain:
    def dev_token(self) -> str:
        raise KeychainError("keychain_unavailable")  # the trial never reads the real Keychain


def run_selftest(log: JsonLog, scrubber: Scrubber) -> int:
    with tempfile.TemporaryDirectory(prefix="yd-sup-selftest-") as scratch:
        root = Path(scratch)
        paths = SupervisorPaths(home=root, data=root / "data", logs=root / "logs", work_copy=root / "repo")
        supervisor = Supervisor(
            paths,
            clock=SystemClock(),
            log=log,
            scrubber=scrubber,
            runner=_InertRunner(),
            spawner=_InertSpawner(),
            opener=_inert_opener,
            keychain=_InertKeychain(),
            uid=os.getuid(),
            pid=os.getpid(),
            command_runner=_InertCommands(),
            release=own_release(),
        )
        supervisor.tick()
        supervisor.caffeinate.stop()
        failed = [job.name for job in supervisor.jobs if job.failures]
    if failed:
        log.error("selftest_failed", jobs=failed)
        return EXIT_SELFTEST_FAILED
    log.info("selftest_ok", version=__version__)
    return 0


def main(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    stream: TextIO | None = None,
    stop: threading.Event | None = None,
    argv: Sequence[str] | None = None,
) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="yordamchi_supervisor")
    parser.add_argument("--selftest", action="store_true", help="one inert tick of every job, then exit")
    options = parser.parse_args(list(argv or []))
    scrubber = Scrubber()
    install_excepthook(scrubber)
    paths, ignored = resolve(os.environ if environ is None else environ, home or Path.home())
    log = JsonLog(paths.log_file, scrubber, stream=stream)
    if options.selftest:
        try:
            return run_selftest(log, scrubber)
        finally:
            log.close()
    for var in ignored:
        log.warning("env_var_ignored", var=var, use="YORDAMCHI_DATA_DIR")
    stop = stop or threading.Event()
    try:
        with single_instance(paths.lock_file) as held:
            if not held:
                log.error("already_running", lock=str(paths.lock_file))
                return EXIT_ALREADY_RUNNING
            supervisor = Supervisor(
                paths,
                clock=SystemClock(),
                log=log,
                scrubber=scrubber,
                runner=SubprocessRunner(),
                spawner=PopenSpawner(),
                opener=urllib_opener,
                keychain=SecurityKeychain(SubprocessRunner()),
                uid=os.getuid(),
                pid=os.getpid(),
                release=own_release(),
                network_guard=default_network_guard(),
            )

            def on_signal(signum: int, _frame: FrameType | None) -> None:
                log.info("signal_received", signal=signal.Signals(signum).name)
                stop.set()

            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, on_signal)
                signal.signal(signal.SIGINT, on_signal)
            supervisor.run_forever(stop)
            return 0
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv[1:]))
