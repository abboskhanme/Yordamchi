"""Child processes: always an argv list, never a shell, always a timeout (CONVENTIONS §6, §12).

`Runner` runs short commands (`launchctl`). `CommandRunner` runs the updater's tools (git, uv, make, a release's own
python) with a working directory, an environment and bytes output, each in its own process group. `Spawner` starts
the one long-lived child, `caffeinate -s -w <pid>`.
Its lifetime is bounded by `-w`, so it exits when the supervisor does, and by `Child.wait(timeout)` on stop.
Children get a minimal environment. The supervisor holds no secret in its environment and passes none on.
"""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

CHILD_ENV: Final = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int | None  # None: it timed out or could not start
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=CHILD_ENV,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return RunResult(returncode=None, stderr="timeout")
        except OSError as exc:
            return RunResult(returncode=None, stderr=type(exc).__name__)
        return RunResult(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


class Child(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Spawner(Protocol):
    def spawn(self, argv: Sequence[str]) -> Child: ...


class PopenSpawner:
    def spawn(self, argv: Sequence[str]) -> Child:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=CHILD_ENV,
        )


# --- the updater's tools: git, uv, make, a release's own python (T57) -----------------------------------------------
@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None  # None: it timed out or could not start
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()

    def output(self) -> str:
        return (self.stdout + b"\n" + self.stderr).decode("utf-8", errors="replace")

    def tail(self, lines: int = 20) -> list[str]:
        """The last non-empty lines of stdout and stderr together: what a failed build or test run reports."""
        return [line for line in self.output().splitlines() if line.strip()][-lines:]


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, timeout: float, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult: ...


def tool_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of git, uv and make, built from nothing (never the supervisor's own), so no `VIRTUAL_ENV`,
    `PYTHONPATH` or credential variable leaks into a build or a test run. `~/.local/bin` comes first for `uv`."""
    env = {"HOME": str(home), "PATH": f"{home / '.local' / 'bin'}:{CHILD_ENV['PATH']}", "LANG": CHILD_ENV["LANG"]}
    env.update(extra or {})
    return env


class SubprocessCommandRunner:
    """Each command in its own process group: a timeout kills the group (TERM, then KILL), so a hung test run or
    `uv sync` leaves no children behind."""

    def run(
        self, argv: Sequence[str], *, timeout: float, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        try:
            child = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(env) if env is not None else dict(CHILD_ENV),
                start_new_session=True,
            )
        except OSError as exc:
            return CommandResult(returncode=None, stderr=type(exc).__name__.encode())
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(child)
            stdout, stderr = child.communicate()
            return CommandResult(returncode=None, stdout=stdout or b"", stderr=(stderr or b"") + b"\ntimeout")
        return CommandResult(returncode=child.returncode, stdout=stdout or b"", stderr=stderr or b"")


def kill_group(child: "subprocess.Popen[bytes]", grace_s: float = 5.0) -> None:
    """TERM the child's process group, then KILL it after `grace_s`."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        child.wait()
