"""`launchctl` in the owner's GUI domain (`gui/<uid>`), through an injectable `Runner` (STACK §4.6, SPEC §9.1).

A label counts as installed when `launchctl print gui/<uid>/<label>` succeeds, that is when the job is loaded. A plist
on disk that is not loaded means the job was stopped on purpose (`launchctl bootout`, SPEC §9.1) or not installed
yet, and the watchdog leaves it alone. `kickstart -k` works only on a loaded job anyway.
"""

import re
from dataclasses import dataclass
from typing import Final

from yordamchi_supervisor.proc import Runner

LAUNCHCTL: Final = "/bin/launchctl"
PRINT_TIMEOUT_S: Final = 10.0
KICKSTART_TIMEOUT_S: Final = 45.0  # -k waits for the old process to exit; the bots' ExitTimeOut is 20 s

_PID = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)
_RUNS = re.compile(r"^\s*runs = (\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class JobStatus:
    loaded: bool
    pid: int | None = None
    runs: int | None = None  # how many times launchd has started the job since it was loaded


class LaunchCtl:
    def __init__(self, runner: Runner, uid: int) -> None:
        self._runner = runner
        self._uid = uid

    def target(self, label: str) -> str:
        return f"gui/{self._uid}/{label}"

    def status(self, label: str) -> JobStatus | None:
        """None when launchctl itself did not answer (timeout): the caller must not act on a guess."""
        result = self._runner.run([LAUNCHCTL, "print", self.target(label)], timeout=PRINT_TIMEOUT_S)
        if result.returncode is None:
            return None
        if not result.ok:
            return JobStatus(loaded=False)
        pid = _PID.search(result.stdout)
        runs = _RUNS.search(result.stdout)
        return JobStatus(
            loaded=True,
            pid=int(pid.group(1)) if pid else None,
            runs=int(runs.group(1)) if runs else None,
        )

    def kickstart(self, label: str) -> bool:
        result = self._runner.run([LAUNCHCTL, "kickstart", "-k", self.target(label)], timeout=KICKSTART_TIMEOUT_S)
        return result.ok

    def bootout(self, label: str) -> bool:
        """Unload the job and stop its process (the updater, before it restores a DB under it). True when it is not
        loaded afterwards, including when it was not loaded to begin with."""
        result = self._runner.run([LAUNCHCTL, "bootout", self.target(label)], timeout=KICKSTART_TIMEOUT_S)
        if result.ok:
            return True
        status = self.status(label)
        return status is not None and not status.loaded

    def bootstrap(self, plist: str) -> bool:
        """Load the job from its plist again (it starts at once: `RunAtLoad`)."""
        result = self._runner.run([LAUNCHCTL, "bootstrap", f"gui/{self._uid}", plist], timeout=KICKSTART_TIMEOUT_S)
        return result.ok
