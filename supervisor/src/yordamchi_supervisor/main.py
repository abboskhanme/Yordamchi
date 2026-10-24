"""Composition and the main loop of `uz.yordamchi.supervisor` (SPEC §2.1, §9.1): `python -I -m yordamchi_supervisor`.

The loop wakes every `LOOP_STEP` and runs each due `Job`:

| job | every | does |
|---|---|---|
| `heartbeat` | 15 s | writes `run/supervisor.heartbeat.json` (`pid`, `version`, `started_at`, `last_tick_at`) |
| `caffeinate` | 15 s | keeps `caffeinate -s -w <pid>` running |
| `watchdog` | 15 s | dead process, stale heartbeat, crash loop (`watchdog.py`) |
| `alerts` | 30 s | retries alerts queued during a network drop |
| `backups` | 60 s | the daily 04:00 backup, when due (`backups.py`) |
| `log_trim` | 60 s | copy-truncates the launchd logs over 5 MB (`logtrim.py`) |

A job that raises is logged (`job_failed`) and alerted at most once per `JOB_ALERT_EVERY`; the other jobs keep
running. SIGTERM (launchd, `kickstart -k`) or SIGINT stops the loop within one step, then caffeinate is stopped.
A second supervisor (a manual run next to the launchd one) finds `run/supervisor.lock` held and exits with 75.

Seam for the self-updater (T57): build an `Updater` with these same parts and `supervisor.add_job(Job("updater",
…))`. It journals in the reserved `update` section of `StateFile`, writes `updates/<id>/status.json`, and the watchdog
already goes quiet for it (`updates.scan`).
"""

import contextlib
import fcntl
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
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
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.log import JsonLog, Scrubber, install_excepthook
from yordamchi_supervisor.logtrim import trim_launchd_logs
from yordamchi_supervisor.paths import SupervisorPaths, resolve
from yordamchi_supervisor.proc import PopenSpawner, Runner, Spawner, SubprocessRunner
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter, Opener, urllib_opener
from yordamchi_supervisor.watchdog import Watchdog

LOOP_STEP: Final = 5.0
JOB_ALERT_EVERY: Final = timedelta(hours=6)
EXIT_ALREADY_RUNNING: Final = 75  # EX_TEMPFAIL


@dataclass(slots=True)
class Job:
    name: str
    every: timedelta
    run: Callable[[datetime], None]
    last_run: datetime | None = None
    last_alert: datetime | None = None

    def due(self, now: datetime) -> bool:
        # A clock that went backwards (manual change) must not stall a job until it catches up.
        return self.last_run is None or now - self.last_run >= self.every or now < self.last_run


def write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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
        uid: int,
        pid: int,
    ) -> None:
        self.paths = paths
        self.clock = clock
        self.log = log
        self.pid = pid
        self.started_at = clock.now()
        self.state = StateFile(paths.state_file)
        self.alerter = Alerter(paths.env_file("dev"), log, scrubber, clock, opener=opener)
        self.caffeinate = Caffeinate(spawner, log, owner_pid=pid)
        self.watchdog = Watchdog(paths, LaunchCtl(runner, uid), self.alerter, log, scrubber, self.state)
        self.backups = Backups(paths, self.alerter, log, self.state)
        self.jobs: list[Job] = [
            Job("heartbeat", timedelta(seconds=15), self.write_heartbeat),
            Job("caffeinate", timedelta(seconds=15), self.caffeinate.ensure),
            Job("watchdog", timedelta(seconds=15), self.watchdog.tick),
            Job("alerts", timedelta(seconds=30), lambda _now: self.alerter.flush()),
            Job("backups", timedelta(seconds=60), self.backups.tick),
            Job("log_trim", timedelta(seconds=60), lambda _now: trim_launchd_logs(paths, log)),
        ]

    def add_job(self, job: Job) -> None:
        self.jobs.append(job)

    def write_heartbeat(self, now: datetime) -> None:
        write_json_atomic(
            self.paths.heartbeat("supervisor"),
            {
                "pid": self.pid,
                "version": __version__,
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
                self.log.error("job_failed", job=job.name, error=type(exc).__name__)
                if job.last_alert is None or now - job.last_alert >= JOB_ALERT_EVERY:
                    job.last_alert = now
                    self.alerter.send("supervisor_error", S.SUPERVISOR_ERROR(job=job.name))

    def run_forever(self, stop: threading.Event) -> None:
        self.log.info("supervisor_started", version=__version__, pid=self.pid, data=str(self.paths.data))
        try:
            while not stop.is_set():
                self.tick()
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


def main(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    stream: TextIO | None = None,
    stop: threading.Event | None = None,
) -> int:
    os.umask(0o077)
    scrubber = Scrubber()
    install_excepthook(scrubber)
    paths, ignored = resolve(os.environ if environ is None else environ, home or Path.home())
    log = JsonLog(paths.log_file, scrubber, stream=stream)
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
                uid=os.getuid(),
                pid=os.getpid(),
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
    sys.exit(main())
