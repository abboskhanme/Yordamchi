"""A notification on the Mac's screen, for news that cannot travel through Telegram (owner request 2026-10-27).

The supervisor's alerts and the bots' own messages all go through Telegram, so an outage of that path is told
nowhere else. `osascript` shows a banner in the owner's GUI session (the supervisor is a LaunchAgent there). The
texts reach the script as `argv` items, never spliced into its source, so nothing in them needs escaping. A failure
is logged and changes nothing else: the banner is a courtesy, the log stays the record.
"""

from typing import Final

from yordamchi_supervisor.log import JsonLog
from yordamchi_supervisor.proc import Runner

OSASCRIPT: Final = "/usr/bin/osascript"
TIMEOUT_S: Final = 10.0
SCRIPT: Final = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


class Notifier:
    def __init__(self, runner: Runner, log: JsonLog) -> None:
        self._runner = runner
        self._log = log

    def notify(self, kind: str, *, title: str, text: str) -> bool:
        """Show one banner. `kind` is a code, logged; the texts are never logged."""
        argv = [OSASCRIPT]
        for line in SCRIPT:
            argv += ["-e", line]
        result = self._runner.run([*argv, "--", title, text], timeout=TIMEOUT_S)
        if result.ok:
            self._log.info("mac_notified", kind=kind)
        else:
            self._log.warning("mac_notify_failed", kind=kind, returncode=result.returncode)
        return result.ok
