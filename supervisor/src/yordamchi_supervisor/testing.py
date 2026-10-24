"""Test doubles for the supervisor's seams: virtual clock, scripted `launchctl`, a recording Telegram opener, a fake
`caffeinate` and a temp data dir. Nothing in the supervisor imports this module; only `supervisor/tests` does.

It lives in the package, not in `supervisor/tests/`, because the root `mypy` run resolves `tests.*` to the app's
`tests` package, so a `supervisor/tests/fakes.py` could not be imported under both runners. T57 (the updater) reuses it.
"""

import io
import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

from yordamchi_supervisor.log import JsonLog, Scrubber
from yordamchi_supervisor.paths import SupervisorPaths
from yordamchi_supervisor.proc import Child, RunResult

# Shaped like a real token (digits:35 chars) so the pattern scrubber and the exact-value scrubber are both exercised.
TOKEN = "8123456789:AAFakeTokenForSupervisorTests_abcdefgh"  # noqa: S105 - a fake
GROUP_ID = "-1001234567890"
UID = 501


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


@dataclass
class FakeJob:
    pid: int | None = 4242
    runs: int = 1


class FakeLaunchctl:
    """A `Runner` that answers `launchctl print` and `kickstart -k` from an in-memory job table.

    `kickstart` restarts a loaded job with a new pid and bumps `runs`, like launchd does. Set `kick_fails` to make it
    return an error, and `hang` to make every call time out.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.unloaded: dict[str, FakeJob] = {}  # booted out, remembered so a bootstrap continues `runs`
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.kick_fails = False
        self.hang = False
        self._next_pid = 5000

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:  # noqa: PLR0911 - one return per verb
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        if self.hang:
            return RunResult(returncode=None, stderr="timeout")
        verb, target = argv[1], argv[-1]
        if verb == "bootstrap":
            return self._bootstrap(target)
        label = target.rsplit("/", 1)[-1]
        job = self.jobs.get(label)
        if job is None:
            return RunResult(returncode=113, stderr=f'Could not find service "{label}" in domain for user gui: {UID}')
        if verb == "print":
            pid_line = f"\tpid = {job.pid}\n" if job.pid is not None else ""
            state = "running" if job.pid is not None else "not running"
            out = f"{target} = {{\n\tactive count = 1\n\tstate = {state}\n\truns = {job.runs}\n{pid_line}}}\n"
            return RunResult(returncode=0, stdout=out)
        if verb == "kickstart":
            if self.kick_fails:
                return RunResult(returncode=37, stderr="Operation already in progress")
            self._next_pid += 1
            job.pid = self._next_pid
            job.runs += 1
            return RunResult(returncode=0)
        if verb == "bootout":
            self.unloaded[label] = self.jobs.pop(label)
            return RunResult(returncode=0)
        return RunResult(returncode=64)

    def _bootstrap(self, plist: str) -> RunResult:
        label = Path(plist).stem
        if label in self.jobs:
            return RunResult(returncode=37, stderr="Bootstrap failed: 5: Input/output error")
        before = self.unloaded.pop(label, None)
        self._next_pid += 1
        self.jobs[label] = FakeJob(pid=self._next_pid, runs=1 if before is None else before.runs + 1)
        return RunResult(returncode=0)

    def kicks(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "kickstart"]

    def crash(self, label: str, *, respawn: bool) -> None:
        """The bot died; with `respawn`, launchd's KeepAlive already started a new process."""
        job = self.jobs[label]
        if respawn:
            self._next_pid += 1
            job.pid = self._next_pid
            job.runs += 1
        else:
            job.pid = None


@dataclass
class Sent:
    url: str
    body: dict[str, object]
    timeout: float


@dataclass
class FakeOpener:
    """Records every Telegram request. `fail` raises it (or returns it, when it is an int status) instead."""

    sent: list[Sent] = field(default_factory=list)
    fail: Callable[[urllib.request.Request], BaseException | int] | None = None

    def __call__(self, request: urllib.request.Request, timeout: float) -> int:
        if self.fail is not None:
            outcome = self.fail(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        data = request.data
        if not isinstance(data, bytes):
            raise TypeError("the alerter always sends a bytes body")
        self.sent.append(Sent(url=request.full_url, body=json.loads(data), timeout=timeout))
        return 200

    def texts(self) -> list[str]:
        return [str(item.body["text"]) for item in self.sent]


def http_error(request: urllib.request.Request, code: int) -> urllib.error.HTTPError:
    """An HTTPError the way urllib raises it: the URL (with the token) is inside the exception."""
    return urllib.error.HTTPError(request.full_url, code, "Unauthorized", Message(), io.BytesIO(b"{}"))


class FakeChild:
    _pids = 9000

    def __init__(self) -> None:
        FakeChild._pids += 1
        self._pid = FakeChild._pids
        self.returncode: int | None = None
        self.terminated = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0


@dataclass
class FakeSpawner:
    spawned: list[list[str]] = field(default_factory=list)
    children: list[FakeChild] = field(default_factory=list)
    fail: bool = False

    def spawn(self, argv: Sequence[str]) -> Child:
        if self.fail:
            raise FileNotFoundError(argv[0])
        self.spawned.append(list(argv))
        child = FakeChild()
        self.children.append(child)
        return child


class TempData:
    """A temp HOME with the SPEC §2.2 data dir and log dir, and a 0600 `dev.env`."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="yd-sup-")
        root = Path(self._dir.name)
        self.paths = SupervisorPaths(home=root, data=root / "data", logs=root / "logs")
        for directory in (self.paths.data, self.paths.run, self.paths.data / "db", self.paths.logs):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.write_env(f"DEV_BOT_TOKEN={TOKEN}\nOWNER_ID=11111\nDEV_GROUP_ID={GROUP_ID}\n")
        self.scrubber = Scrubber()
        self.log = JsonLog(self.paths.log_file, self.scrubber)

    def write_env(self, text: str, mode: int = 0o600) -> None:
        path = self.paths.env_file("dev")
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)

    def write_heartbeat(self, bot: str, *, at: datetime, pid: int, started_at: str) -> None:
        """A heartbeat in the T32 shape, from before T32b (no poll attempt fields), written at `at`."""
        body: dict[str, object] = {
            "pid": pid,
            "version": "2026.10.24-abcdef0",
            "started_at": started_at,
            "last_poll_ok_at": at.isoformat(),
            "mode": "normal",
            "loop_lag_ms": 3,
        }
        self._write_beat(bot, body, at)

    def write_poll_heartbeat(
        self,
        bot: str,
        *,
        at: datetime,
        pid: int,
        started_at: str,
        attempt_at: datetime | None,
        kind: str = "none",
        failures: int = 0,
        ok_at: datetime | None = None,
        mode: str = "normal",
    ) -> None:
        """A heartbeat in the T32b shape, written at `at`: the poll attempt fields as `shared/health` writes them."""
        body: dict[str, object] = {
            "pid": pid,
            "version": "2026.10.24-abcdef0",
            "started_at": started_at,
            "last_poll_ok_at": None if ok_at is None else ok_at.isoformat(),
            "mode": mode,
            "loop_lag_ms": 3,
            "last_poll_attempt_at": None if attempt_at is None else attempt_at.isoformat(),
            "last_poll_error_kind": kind,
            "consecutive_poll_failures": failures,
        }
        self._write_beat(bot, body, at)

    def _write_beat(self, bot: str, body: dict[str, object], at: datetime) -> None:
        path = self.paths.run / f"{bot}.heartbeat.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        os.utime(path, (at.timestamp(), at.timestamp()))

    def log_text(self) -> str:
        path = self.paths.log_file
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def log_events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log_text().splitlines() if line]

    def cleanup(self) -> None:
        self.log.close()
        self._dir.cleanup()


START = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)  # 15:00 in Tashkent, a Saturday
