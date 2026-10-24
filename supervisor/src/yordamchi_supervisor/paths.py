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
REPO_DIR_VAR: Final = "YORDAMCHI_REPO_DIR"
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
    work_copy: Path | None = None  # `YORDAMCHI_REPO_DIR`; None = the SPEC §2.2 default

    @property
    def repo(self) -> Path:
        """The working copy the executor edits (SPEC §2.2). The updater only reads commits from it (`git archive`),
        merges a healthy update's branch and switches back to `main`; nothing ever runs from it."""
        return self.work_copy or self.home / "Documents" / "Projects" / "Yordamchi"

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

    # --- releases and the self-updater (SPEC §2.2, §3.2.12) -------------------------------------------------------
    @property
    def releases(self) -> Path:
        return self.data / "releases"

    @property
    def current_release(self) -> Path:
        """`releases/current`, the symlink the bots run from."""
        return self.releases / "current"

    @property
    def supervisor_releases(self) -> Path:
        return self.data / "supervisor" / "releases"

    @property
    def supervisor_current(self) -> Path:
        return self.data / "supervisor" / "current"

    @property
    def flags(self) -> Path:
        return self.run / "flags"

    @property
    def drain_flag(self) -> Path:
        return self.flags / "drain.json"

    @property
    def probation_flag(self) -> Path:
        return self.flags / "probation.json"

    def drained_marker(self, bot: Bot) -> Path:
        return self.run / f"{bot}.drained"

    def socket(self, bot: Bot) -> Path:
        return self.run / f"{bot}.sock"

    def update_dir(self, update_id: str) -> Path:
        return self.updates / update_id

    @property
    def latest_update(self) -> Path:
        return self.updates / "latest.json"

    def pre_update_backup(self, update_id: str) -> Path:
        return self.backups / "pre-update" / update_id

    def plist(self, process: Process) -> Path:
        """`~/Library/LaunchAgents/uz.yordamchi.<process>.plist`, what `launchctl bootstrap` loads again."""
        return self.home / "Library" / "LaunchAgents" / f"{label(process)}.plist"

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
        work_copy=override(REPO_DIR_VAR),
    )
    return paths, ignored
