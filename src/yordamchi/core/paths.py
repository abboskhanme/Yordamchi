"""Where Yordamchi keeps things on disk (SPEC §2.2, decided).

Every path derives from four roots: the home directory, the data dir, the config dir (the legacy master-key file;
on Windows also the DPAPI secret store) and the log dir.
Production uses the defaults below; tests and the supervisor's `make check` run (`YORDAMCHI_DATA_DIR=<tmp>`) point the
roots elsewhere through `core.config.load_paths()`. Nothing here touches the disk.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal


class BotName(StrEnum):
    """The two bot processes. Defined here because each one's files (env, DB, socket, heartbeat) carry its name."""

    DEV = "dev"
    PERSONAL = "personal"


type ProcessName = Literal["dev", "personal", "supervisor"]
type SessionDir = Literal["routing", "report", "settings", "assistant", "smoke"]

APP_DIR_NAME = "Yordamchi"
LAUNCHD_PREFIX = "uz.yordamchi"
# In `run/`, next to the control sockets: the pre-push hook finds it there.
SECRET_HASHES_FILE = "secret-hashes.json"  # noqa: S105  # a file name, not a secret


@dataclass(frozen=True, slots=True)
class Paths:
    """The resolved roots. Build it with `Paths.defaults(home)` or `core.config.load_paths()`."""

    home: Path
    data: Path
    config: Path
    logs: Path

    @classmethod
    def defaults(
        cls,
        home: Path,
        *,
        data: Path | None = None,
        config: Path | None = None,
        logs: Path | None = None,
    ) -> "Paths":
        """SPEC §2.2 defaults under `home`; any root may be overridden."""
        return cls(
            home=home,
            data=data or home / "Library" / "Application Support" / APP_DIR_NAME,
            config=config or home / ".config" / "yordamchi",
            logs=logs or home / "Library" / "Logs" / APP_DIR_NAME,
        )

    @classmethod
    def windows_defaults(
        cls,
        home: Path,
        *,
        local_app_data: Path | None = None,
        app_data: Path | None = None,
        data: Path | None = None,
        config: Path | None = None,
        logs: Path | None = None,
    ) -> "Paths":
        """Windows (the personal bot run from a terminal, owner request 2026-10-31): data in
        `%LOCALAPPDATA%/Yordamchi`, logs in `data/logs`, config (the DPAPI secret store) in `%APPDATA%/Yordamchi`,
        so the sealed secrets live outside DATA. Without the variables, the `AppData` folders under `home`."""
        local = local_app_data or home / "AppData" / "Local"
        roaming = app_data or home / "AppData" / "Roaming"
        data = data or local / APP_DIR_NAME
        return cls(home=home, data=data, config=config or roaming / APP_DIR_NAME, logs=logs or data / "logs")

    # --- bootstrap and databases ---------------------------------------------------------------------------------
    def env_file(self, bot: BotName) -> Path:
        """`dev.env` / `personal.env` (0600): token, owner id, chat ids."""
        return self.data / f"{bot}.env"

    @property
    def db_dir(self) -> Path:
        return self.data / "db"

    def db_file(self, bot: BotName) -> Path:
        """`db/dev.db` is written only by the dev bot, `db/personal.db` only by the personal bot (SPEC §2.3)."""
        return self.db_dir / f"{bot}.db"

    @property
    def master_key(self) -> Path:
        """The legacy master-key file (before 2026-10-24). The key now lives in the Keychain (`core.keychain`
        `MASTER_KEY`): `KeychainKeystore` and the installer import this file, verify the copy and delete it, and the
        check script is red while it exists."""
        return self.config / "master.key"

    @property
    def secret_store(self) -> Path:
        """Windows only: the DPAPI blobs of the token and the master key (`core.keychain.DpapiStore`)."""
        return self.config / "secrets"

    # --- releases ------------------------------------------------------------------------------------------------
    @property
    def releases(self) -> Path:
        return self.data / "releases"

    @property
    def current_release(self) -> Path:
        """Symlink to the live release; the bots run from here, never from the working copy."""
        return self.releases / "current"

    @property
    def shims(self) -> Path:
        """`ssh`/`scp`/`sftp`/`rsync` gate shims; first on every child's PATH (SPEC §2.5 step 4)."""
        return self.current_release / "shims"

    @property
    def githooks(self) -> Path:
        return self.current_release / "githooks"

    @property
    def supervisor(self) -> Path:
        return self.data / "supervisor"

    # --- run/: heartbeats, markers, sockets, flags ---------------------------------------------------------------
    @property
    def run(self) -> Path:
        return self.data / "run"

    def socket(self, bot: BotName) -> Path:
        return self.run / f"{bot}.sock"

    def heartbeat(self, bot: BotName) -> Path:
        return self.run / f"{bot}.heartbeat.json"

    def clean_marker(self, bot: BotName) -> Path:
        """Written on a clean exit; its absence at start means the previous exit was unclean (SPEC §2.5 step 8)."""
        return self.run / f"{bot}.clean"

    def last_error(self, bot: BotName) -> Path:
        return self.run / f"{bot}.lasterror"

    def drained_marker(self, bot: BotName) -> Path:
        return self.run / f"{bot}.drained"

    @property
    def flags(self) -> Path:
        return self.run / "flags"

    @property
    def drain_flag(self) -> Path:
        return self.flags / "drain.json"

    @property
    def probation_flag(self) -> Path:
        return self.flags / "probation.json"

    @property
    def secret_hashes(self) -> Path:
        """sha256 of every vault value, read by the pre-push scan (SPEC §4.5)."""
        return self.run / SECRET_HASHES_FILE

    # --- updates, backups, caches --------------------------------------------------------------------------------
    @property
    def updates(self) -> Path:
        return self.data / "updates"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def media(self) -> Path:
        """Media cache, purged after `media.retention_days`."""
        return self.data / "media"

    def session_dir(self, kind: SessionDir) -> Path:
        """Empty private cwd for an assistant or smoke session (SPEC §5.3)."""
        return self.data / "sessions" / kind

    @property
    def bin(self) -> Path:
        """Static ffmpeg/ffprobe (SPEC §9.3 step 6)."""
        return self.data / "bin"

    # --- logs ----------------------------------------------------------------------------------------------------
    def log_file(self, process: ProcessName) -> Path:
        return self.logs / f"{process}.jsonl"

    def launchd_log(self, process: ProcessName) -> Path:
        return self.logs / f"{LAUNCHD_PREFIX}.{process}.launchd.log"

    # --- outside Yordamchi's own dirs ----------------------------------------------------------------------------
    @property
    def local_bin(self) -> Path:
        """`~/.local/bin`: claude, gh, node, uv (STACK §5)."""
        return self.home / ".local" / "bin"

    @property
    def claude_user_settings(self) -> Path:
        """The owner's `~/.claude/settings.json`: read for the credential check, never written (SPEC §2.5 step 3)."""
        return self.home / ".claude" / "settings.json"
