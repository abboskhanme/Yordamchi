"""Keeps `/usr/bin/caffeinate -s -w <supervisor pid>` alive (SPEC §9.2): no system sleep while on AC power.

`-w` ties caffeinate's life to the supervisor, so a `kill -9` of the supervisor cannot leave an orphan holding the
power assertion. If caffeinate exits, `ensure()` starts a new one on the next tick. A spawn failure is logged once per
`SPAWN_FAIL_LOG_EVERY`, not every tick.
"""

import subprocess
from datetime import datetime, timedelta
from typing import Final

from yordamchi_supervisor.log import JsonLog
from yordamchi_supervisor.proc import Child, Spawner

CAFFEINATE: Final = "/usr/bin/caffeinate"
STOP_TIMEOUT_S: Final = 5.0
SPAWN_FAIL_LOG_EVERY: Final = timedelta(hours=1)


class Caffeinate:
    def __init__(self, spawner: Spawner, log: JsonLog, *, owner_pid: int) -> None:
        self._spawner = spawner
        self._log = log
        self.argv: Final = (CAFFEINATE, "-s", "-w", str(owner_pid))
        self._child: Child | None = None
        self._fail_logged_at: datetime | None = None
        self.restarts = 0

    @property
    def child(self) -> Child | None:
        return self._child

    def ensure(self, now: datetime) -> None:
        if self._child is not None:
            code = self._child.poll()
            if code is None:
                return
            self.restarts += 1
            self._log.warning("caffeinate_exited", code=code, restarts=self.restarts)
            self._child = None
        try:
            self._child = self._spawner.spawn(self.argv)
        except OSError as exc:
            if self._fail_logged_at is None or now - self._fail_logged_at >= SPAWN_FAIL_LOG_EVERY:
                self._log.error("caffeinate_spawn_failed", error=type(exc).__name__)
                self._fail_logged_at = now
            return
        self._fail_logged_at = None
        self._log.info("caffeinate_started", pid=self._child.pid)

    def stop(self) -> None:
        child, self._child = self._child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=STOP_TIMEOUT_S)
