"""Where the supervisor finds things (SPEC §2.2, §9.1). A copy of the app's layout, because A6 forbids importing it.

The data dir comes from `YORDAMCHI_DATA_DIR` (the same variable the app reads, SPEC §3.2.12) and defaults to
`~/Library/Application Support/Yordamchi`. SPEC §9.1 names the plist variable `YORDAMCHI_DATA`. That spelling is
not read; it is only reported in the log, so a plist rendered from the old table cannot point the supervisor at a
different directory without anyone noticing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

DATA_DIR_VAR: Final = "YORDAMCHI_DATA_DIR"
LOG_DIR_VAR: Final = "YORDAMCHI_LOG_DIR"
LEGACY_DATA_VAR: Final = "YORDAMCHI_DATA"
APP_DIR_NAME: Final = "Yordamchi"
LAUNCHD_PREFIX: Final = "uz.yordamchi"

type Bot = Literal["dev", "personal"]
type Process = Literal["dev", "personal", "supervisor"]

BOTS: Final[tuple[Bot, ...]] = ("dev", "personal")
PROCESSES: Final[tuple[Process, ...]] = ("dev", "personal", "supervisor")


def label(process: Process) -> str:
    """`uz.yordamchi.dev`, `uz.yordamchi.personal`, `uz.yordamchi.supervisor`."""
    return f"{LAUNCHD_PREFIX}.{process}"


@dataclass(frozen=True, slots=True)
class SupervisorPaths:
    home: Path
    data: Path
    logs: Path

    # --- data dir ------------------------------------------------------------------------------------------------
    def env_file(self, bot: Bot) -> Path:
        return self.data / f"{bot}.env"

    def db_file(self, bot: Bot) -> Path:
        return self.data / "db" / f"{bot}.db"

    @property
    def run(self) -> Path:
        return self.data / "run"

    def heartbeat(self, process: Process) -> Path:
        return self.run / f"{process}.heartbeat.json"

    def last_error(self, bot: Bot) -> Path:
        return self.run / f"{bot}.lasterror"

    @property
    def lock_file(self) -> Path:
        return self.run / "supervisor.lock"

    @property
    def updates(self) -> Path:
        return self.data / "updates"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def state_file(self) -> Path:
        """`supervisor/state.json`: watchdog and backup memory now, and the updater's step journal in T57."""
        return self.data / "supervisor" / "state.json"

    # --- logs ----------------------------------------------------------------------------------------------------
    @property
    def log_file(self) -> Path:
        return self.logs / "supervisor.jsonl"

    def launchd_log(self, process: Process) -> Path:
        return self.logs / f"{label(process)}.launchd.log"


def resolve(environ: Mapping[str, str], home: Path) -> tuple[SupervisorPaths, list[str]]:
    """The paths, plus the names of variables that were set but are deliberately not read (for the log)."""

    def override(var: str) -> Path | None:
        value = environ.get(var)
        return Path(value).expanduser().absolute() if value else None

    ignored = [LEGACY_DATA_VAR] if environ.get(LEGACY_DATA_VAR) and not environ.get(DATA_DIR_VAR) else []
    paths = SupervisorPaths(
        home=home,
        data=override(DATA_DIR_VAR) or home / "Library" / "Application Support" / APP_DIR_NAME,
        logs=override(LOG_DIR_VAR) or home / "Library" / "Logs" / APP_DIR_NAME,
    )
    return paths, ignored
