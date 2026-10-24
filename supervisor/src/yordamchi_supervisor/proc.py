"""Child processes: always an argv list, never a shell, always a timeout (CONVENTIONS §6, §12).

`Runner` runs short commands (`launchctl`). `Spawner` starts the one long-lived child, `caffeinate -s -w <pid>`.
Its lifetime is bounded by `-w`, so it exits when the supervisor does, and by `Child.wait(timeout)` on stop.
Children get a minimal environment. The supervisor holds no secret in its environment and passes none on.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
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
