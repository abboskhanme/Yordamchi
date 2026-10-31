"""Install Yordamchi on this Mac (SPEC §9.3, §2.2, §9.1; TASKS T28). See docs/DEPLOY.md before running it.

Run it with the uv-managed Python, never Homebrew's:

    ~/.local/bin/python3.12 -I scripts/install.py --dry-run                # temp HOME; launchctl is never called
    ~/.local/bin/python3.12 -I scripts/install.py --release <commit>       # the real install (owner's go-ahead)
    ~/.local/bin/python3.12 -I scripts/install.py --env-only --rotate-tokens

Steps, in order. Each one is idempotent, so a failed run is fixed and simply run again:
 1. preflight: arm64 macOS; `~/.local/bin/{uv,claude,gh,node}` and `/usr/bin/git`; no refused credential source
    (`core.proc`'s refuse-list); the ffmpeg SHA-256 pins (or `--skip-ffmpeg`).
 2. toolchain: uv 0.13.5 (`uv self update`) and Python 3.12.15 (`uv python install`).
 3. the DATA tree (0700), `~/.config/yordamchi` (0700), the log dir (0700).
 4. the vault master key in the macOS login Keychain (`uz.yordamchi/vault-master-key`, base64 of 32 random bytes),
    created only if absent, never overwritten. A legacy `~/.config/yordamchi/master.key` is imported, read back,
    compared and only then deleted; a different key already in the Keychain stops the install with nothing changed.
    `tmutil addexclusion` on `~/.config/yordamchi`.
 5. env files `dev.env` / `personal.env` (0600): a value already there is never overwritten. Only missing values are
    asked for, tokens with hidden input (`getpass`). **Tokens go into the Keychain** (`uz.yordamchi/dev-bot-token`,
    `…/personal-bot-token`) and the env file gets only the reference (`@keychain:uz.yordamchi/dev-bot-token`); a
    plaintext token still in a file is moved into the Keychain (and should then be rotated). `--rotate-tokens` asks
    for new tokens and updates the Keychain items. `DEV_GROUP_ID` must be a `-100…` supergroup id.
 6. the app release `releases/<YYYY.MM.DD-sha7>/` from `git archive <commit>` (never the working copy), its `.venv`
    from `uv sync --frozen` with Python 3.12.15, then `chmod -R a-w`.
 7. the supervisor release `supervisor/releases/<version>/` from `git archive <commit>:supervisor`, same build.
 8. ffmpeg + ffprobe 9.0.2 arm64 into `DATA/bin`, SHA-256 verified, `-encoders` must list libopus.
 9. a running bot is stopped (`launchctl bootout`), each existing DB is copied to `backups/pre-update/install-…`
    (sqlite3 backup API + `integrity_check`), then `alembic upgrade head` runs on `db/dev.db` and `db/personal.db`.
10. `releases/current` and `supervisor/current` are switched atomically (the previous target is printed: rollback).
11. the plists are rendered from `ops/launchd/*.plist.tmpl` of the release into `DATA/launchd/`; dev and supervisor
    are copied to `~/Library/LaunchAgents` (personal only with `--enable-personal`, P3). The supervisor's
    `YORDAMCHI_REPO_DIR` is this checkout (or `--repo`), wherever it was cloned.
12. `launchctl bootstrap` / `kickstart -k`, then `python -m yordamchi.check` from the new release.

`--dry-run` builds everything under a temporary HOME (the release, its venv with `uv sync --offline`, the DBs, the
plists) and skips whatever would change this Mac outside it: `uv self update`, `uv python install`, the ffmpeg
download, `tmutil`, every `launchctl` call, **every Keychain read and write** (the Keychain is not under HOME) and the
final check. It never reads the owner's real env files.

Standard library only, plus `yordamchi.core` and `yordamchi.bots.cli.check` from this checkout (both stdlib-only,
A9), so the layout, the env-file grammar, the Keychain item names and the refuse-list have one definition. Secrets
never reach a file, argv or the output: the Keychain is written through `security -i` stdin and read back to verify,
values are registered with `core.redact` the moment they are known, and every printed line is scrubbed.
"""

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import subprocess  # an installer, run before any venv exists: core.proc is async-only
import sys
import tarfile
import tempfile
import time
import urllib.request
import xml.sax.saxutils
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, TextIO

REPO: Final = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from yordamchi.bots.cli import check as yd_check  # needs the sys.path entry above
from yordamchi.core import keychain as yd_keychain
from yordamchi.core import proc as yd_proc
from yordamchi.core import redact
from yordamchi.core.errors import ConfigInvalid, KeychainItemMissing, KeychainUnavailable
from yordamchi.core.paths import BotName, Paths, ProcessName

UV_VERSION: Final = "0.13.5"
PYTHON_VERSION: Final = "3.12.15"
FFMPEG_VERSION: Final = "9.0.2"
SESSION_KINDS: Final = ("routing", "report", "settings", "assistant", "smoke")
RELEASE_MARKER: Final = ".yordamchi-release.json"
FFMPEG_MARKER: Final = f".ffmpeg-{FFMPEG_VERSION}.json"
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
PLIST_MODE: Final = 0o644
TOKEN_PROMPT_TRIES: Final = 3
MASTER_KEY_BYTES: Final = 32
SYSTEM_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
# The same grammar `core.config` enforces at bot start (tests/unit/ops/test_install.py keeps them in step).
BOT_TOKEN: Final = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
GROUP_ID: Final = re.compile(r"-\d+")
# Inherited variables that would point uv or Python somewhere else than the release being built.
UNINHERITED_ENV: Final = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONPATH", "PYTHONHOME")

TIMEOUT_QUICK_S: Final = 30.0
TIMEOUT_GIT_S: Final = 120.0
TIMEOUT_UV_S: Final = 1800.0
TIMEOUT_DOWNLOAD_S: Final = 300.0
TIMEOUT_MIGRATE_S: Final = 300.0
TIMEOUT_LAUNCHCTL_S: Final = 45.0
UNLOAD_WAIT_S: Final = 20.0  # launchd lets a booted-out job go within a few seconds
UNLOAD_POLL_S: Final = 0.5
TIMEOUT_CHECK_S: Final = 120.0
TIMEOUT_KEYCHAIN_S: Final = 15.0


@dataclass(frozen=True, slots=True)
class Pin:
    """One pinned download: an https URL and the SHA-256 of the zip it serves."""

    url: str
    sha256: str


# ffmpeg + ffprobe 9.0.2 static arm64 (martin-riedl.de, signed zips, opus enabled; STACK §2, §5 item 4).
# Copied from docs/STACK.md §5, which names the publisher's `<zip>.sha256` files they were checked against; keep the
# two in step (tests/unit/ops/test_install.py compares them). A version bump records new pins there first; a pin set
# to None makes the preflight refuse instead of guessing.
FFMPEG_RELEASE: Final = f"https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_{FFMPEG_VERSION}"
FFMPEG_PINS: Final[Mapping[str, Pin | None]] = {
    "ffmpeg": Pin(f"{FFMPEG_RELEASE}/ffmpeg.zip", "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
    "ffprobe": Pin(f"{FFMPEG_RELEASE}/ffprobe.zip", "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"),
}
FFMPEG_NOT_PINNED: Final = (
    f"ffmpeg {FFMPEG_VERSION}: the download URL and SHA-256 of ffmpeg.zip / ffprobe.zip are not pinned in "
    "docs/STACK.md §5, and the installer does not guess them. Record both pins there, copy them into FFMPEG_PINS in "
    "scripts/install.py, and run it again; or pass --skip-ffmpeg to install without voice encoding for now."
)


# --- seams the tests replace -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> Completed: ...


class Downloader(Protocol):
    def __call__(self, url: str, dest: Path, *, timeout: float) -> None: ...


class Prompter(Protocol):
    @property
    def interactive(self) -> bool: ...

    def secret(self, prompt: str) -> str: ...

    def text(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Tools:
    uv: Path
    git: Path
    claude: Path
    gh: Path
    node: Path
    launchctl: Path = Path("/bin/launchctl")
    tmutil: Path = Path("/usr/bin/tmutil")

    @classmethod
    def under(cls, tools_home: Path) -> "Tools":
        local = tools_home / ".local" / "bin"
        return cls(
            uv=local / "uv", git=Path("/usr/bin/git"), claude=local / "claude", gh=local / "gh", node=local / "node"
        )


def child_env(environ: Mapping[str, str], tools_home: Path) -> dict[str, str]:
    """`core.proc.clean_env` (no refused, nested-session or Yordamchi-private names) with `~/.local/bin` first."""
    env = yd_proc.clean_env(path=f"{tools_home / '.local' / 'bin'}:{SYSTEM_PATH}", base=environ)
    for name in UNINHERITED_ENV:
        env.pop(name, None)
    env["HOME"] = str(tools_home)  # uv's cache and managed Pythons live in the owner's real home
    return env


def subprocess_runner(env: Mapping[str, str]) -> Runner:
    def run(argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> Completed:
        try:
            done = subprocess.run(  # argv lists of absolute tool paths, never a shell
                list(argv), cwd=cwd, env=dict(env), capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return Completed(returncode=124, stderr=b"timed out")
        except OSError as exc:
            return Completed(returncode=127, stderr=type(exc).__name__.encode())
        return Completed(done.returncode, done.stdout, done.stderr)

    return run


DOWNLOAD_USER_AGENT: Final = "Yordamchi-installer/1.0"


def urllib_download(url: str, dest: Path, *, timeout: float) -> None:
    if not url.startswith("https://"):
        msg = "only https downloads are allowed"
        raise ValueError(msg)
    # Cloudflare (ffmpeg.martin-riedl.de) answers urllib's default `Python-urllib/3.x` agent with 403.
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})  # noqa: S310  # https only
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as handle:  # noqa: S310
        shutil.copyfileobj(response, handle)


class KeychainStore(Protocol):
    """The installer's view of the login Keychain. Unlike a bot's `core.keychain.Keychain` it is not scoped: the
    installer is the owner's own tool and writes every `uz.yordamchi` item."""

    def read(self, item: yd_keychain.KeychainItem) -> str | None:
        """The value, or None when there is no such item. Raises `KeychainUnavailable`."""
        ...

    def write(self, item: yd_keychain.KeychainItem, value: str) -> None:
        """Create or replace it, then read it back. Raises `KeychainUnavailable` unless the copy is exact."""
        ...


class SecurityKeychain:
    """`/usr/bin/security` through `core.keychain`'s command lines: the value goes in on stdin and comes back on
    stdout, never on argv. `runner` is the test seam (a fake `security`)."""

    def __init__(self, runner: yd_keychain.Runner = yd_keychain.security_runner) -> None:
        self._run = runner

    def _call(self, argv: Sequence[str], item: yd_keychain.KeychainItem, stdin: bytes | None) -> yd_proc.ProcResult:
        try:
            return self._run(argv, stdin=stdin, timeout=TIMEOUT_KEYCHAIN_S)
        except (TimeoutError, OSError):
            raise KeychainUnavailable(item=str(item)) from None

    def read(self, item: yd_keychain.KeychainItem) -> str | None:
        result = self._call(yd_keychain.find_argv(item, reveal=True), item, None)
        try:
            value = yd_keychain.password_from(result, item)
        except KeychainItemMissing:
            return None
        redact.add_known_secrets([value])
        return value

    def write(self, item: yd_keychain.KeychainItem, value: str) -> None:
        redact.add_known_secrets([value])
        result = self._call(yd_keychain.write_argv(), item, yd_keychain.add_script(item, value))
        if result.returncode != 0 or self.read(item) != value:
            raise KeychainUnavailable(item=str(item))


class TerminalPrompter:
    @property
    def interactive(self) -> bool:
        return sys.stdin.isatty()

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt).strip()

    def text(self, prompt: str) -> str:
        return input(prompt).strip()


# --- options, steps, summary -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Options:
    home: Path
    repo: Path = REPO
    commit: str = "HEAD"
    dry_run: bool = False
    rotate_tokens: bool = False
    env_only: bool = False
    enable_personal: bool = False
    skip_ffmpeg: bool = False
    uid: int = field(default_factory=os.getuid)


class Status(StrEnum):
    DONE = "done"
    UNCHANGED = "unchanged"
    DRY_RUN = "dry-run"  # skipped because it would change this Mac outside the temp HOME
    WARN = "warn"
    FAILED = "failed"
    BLOCKED = "blocked"  # not attempted: an earlier step it needs did not complete


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    status: Status
    detail: str = ""


class InstallAborted(Exception):
    """A real install stops at the first failure, so nothing after a broken step changes this Mac."""


@dataclass(slots=True)
class Releases:
    sha: str = ""
    version: str = ""
    app: Path | None = None
    supervisor: Path | None = None
    previous_app: str | None = None
    previous_supervisor: str | None = None


# --- small file helpers ----------------------------------------------------------------------------------------------
def ensure_dir(path: Path) -> bool:
    """Create with 0700 or fix the mode; True when something changed."""
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    changed = created or (path.stat().st_mode & 0o777) != DIR_MODE
    path.chmod(DIR_MODE)
    return changed


def write_private(path: Path, data: bytes, *, mode: int = FILE_MODE) -> None:
    """Atomic write; the temp file has its final mode before the first byte."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            yield base / name


def make_read_only(root: Path) -> None:
    """`chmod -R a-w`, without following symlinks (`.venv/bin/python` points at uv's interpreter)."""
    for path in [*_walk(root), root]:
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode & ~0o222)


def make_writable(root: Path) -> None:
    for path in [root, *_walk(root)]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)


def swap_symlink(link: Path, target: Path) -> str | None:
    """Point `link` at `target` atomically, as a link relative to `link`'s directory (so DATA can move); returns
    the previous link text."""
    previous = str(link.readlink()) if link.is_symlink() else None
    tmp = link.with_name(f".{link.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(os.path.relpath(target, link.parent))
    tmp.replace(link)
    return previous


def render_plist(template: str, values: Mapping[str, str]) -> str:
    """`{{NAME}}` placeholders, XML-escaped; a missing value fails, and the result must parse as a plist."""
    missing = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", template)) - set(values))
    if missing:
        msg = f"plist template needs {missing}"
        raise ValueError(msg)
    rendered = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: xml.sax.saxutils.escape(values[m.group(1)]), template)
    plistlib.loads(rendered.encode("utf-8"))
    return rendered


def plist_values(paths: Paths, repo: Path) -> dict[str, str]:
    """`REPO` is the checkout the installer runs from (or `--repo`): the supervisor's `YORDAMCHI_REPO_DIR`, so a clone
    in any folder is the working copy upstream updates fetch into (SPEC §3.2.12 "Upstream updates")."""
    return {"DATA": str(paths.data), "HOME": str(paths.home), "LOGS": str(paths.logs), "REPO": str(repo)}


def data_dirs(paths: Paths) -> list[Path]:
    """SPEC §2.2, every directory the installer owns (all 0700)."""
    backups = paths.backups
    return [
        paths.data,
        paths.db_dir,
        paths.releases,
        paths.supervisor,
        paths.supervisor / "releases",
        paths.run,
        paths.flags,
        paths.updates,
        backups,
        backups / "daily",
        backups / "weekly",
        backups / "pre-update",
        paths.media,
        paths.data / "sessions",
        *(paths.session_dir(kind) for kind in SESSION_KINDS),
        paths.bin,
        staging_dir(paths),
        paths.config,
        paths.logs,
    ]


def staging_dir(paths: Paths) -> Path:
    """Rendered plists live here first; launchd never reads this directory."""
    return paths.data / "launchd"


def _sqlite_backup(source: Path, target: Path) -> str:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            row = dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    return str(row[0]) if row else "no result"


# --- env files -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EnvKey:
    name: str
    secret: bool
    required: bool
    prompt: str


ENV_SPECS: Final[Mapping[BotName, tuple[EnvKey, ...]]] = {
    BotName.DEV: (
        EnvKey("DEV_BOT_TOKEN", secret=True, required=True, prompt="Dev bot token from BotFather (hidden): "),
        EnvKey("OWNER_ID", secret=False, required=False, prompt="Your Telegram user id (empty: find it later): "),
        EnvKey("DEV_GROUP_ID", secret=False, required=False, prompt="Dev group id, -100… (empty: find it later): "),
    ),
    BotName.PERSONAL: (
        EnvKey("PERSONAL_BOT_TOKEN", secret=True, required=False, prompt="Personal bot token (hidden, P3): "),
        EnvKey("OWNER_ID", secret=False, required=False, prompt="Your Telegram user id: "),
        EnvKey("PERSONAL_GROUP_ID", secret=False, required=False, prompt="Personal group id (optional, empty = DM): "),
    ),
}
ENV_HEADER: Final = (
    "# Written by scripts/install.py (SPEC §2.2). Mode 0600. Never commit this file and never paste it into chat.\n"
    "# Tokens are not here: each *_BOT_TOKEN names its macOS Keychain item (@keychain:<service>/<account>).\n"
)
ROTATE_ADVICE: Final = (
    "they sat in a file in plaintext, so revoke them in BotFather and run: "
    "scripts/install.py --env-only --rotate-tokens"
)


def env_problem(key: str, value: str) -> str | None:
    """Why `value` is not acceptable for `key`, or None. A token is checked as typed (plaintext): what reaches the
    Keychain, not the file."""
    if key.endswith("_BOT_TOKEN"):
        return None if BOT_TOKEN.fullmatch(value) else "not a BotFather token (digits:35 characters)"
    if key == "OWNER_ID":
        return None if value.isdigit() and int(value) > 0 else "not a Telegram user id (a positive number)"
    if key == "DEV_GROUP_ID":
        if yd_check.SUPERGROUP_ID.fullmatch(value):
            return None
        return (
            "not a -100… supergroup id (a basic group changes its id when Telegram upgrades it, and the bot would "
            "then leave it as foreign)"
        )
    if key == "PERSONAL_GROUP_ID":
        return None if GROUP_ID.fullmatch(value) else "not a group id (a negative number)"
    return None


def file_value_problem(key: str, value: str) -> str | None:
    """Why a value already in an env file is not acceptable. A token line holds its own Keychain reference, or a
    legacy plaintext token that this run moves into the Keychain."""
    item = yd_keychain.TOKEN_ITEMS.get(key)
    if item is not None and yd_keychain.is_reference(value):
        return None if value == item.reference else f"must reference the Keychain item {item}"
    return env_problem(key, value)


def _master_key_bytes(encoded: str) -> bytes | None:
    """The raw key from standard base64 of exactly 32 bytes, else None."""
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except ValueError:
        return None
    return raw if len(raw) == MASTER_KEY_BYTES else None


@dataclass(slots=True)
class EnvPlan:
    """What one env file needs: values to write, keys whose value is replaced, keys left empty, tokens that were in
    the file in plaintext and are now only in the Keychain, and (dry run) tokens a rotation would ask for."""

    updates: dict[str, str] = field(default_factory=dict)
    replace: set[str] = field(default_factory=set)
    missing: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    rotatable: list[str] = field(default_factory=list)


# --- the installer ---------------------------------------------------------------------------------------------------
class Installer:
    def __init__(
        self,
        options: Options,
        *,
        tools: Tools,
        runner: Runner,
        download: Downloader,
        prompter: Prompter,
        keychain: KeychainStore,
        environ: Mapping[str, str],
        out: TextIO,
        pins: Mapping[str, Pin | None] = FFMPEG_PINS,
        machine: str = "",
        stamp: Callable[[], str] = lambda: time.strftime("%Y%m%dT%H%M%S"),
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.options = options
        self.paths = Paths.defaults(options.home)
        self.tools = tools
        self.run_cmd = runner
        self.download = download
        self.prompter = prompter
        self.keychain = keychain
        self.environ = environ
        self.out = out
        self.pins = pins
        self.machine = machine or f"{sys.platform}/{platform.machine()}"
        self.stamp = stamp
        self.pause = pause
        self.steps: list[Step] = []
        self.releases = Releases()
        self.stopped: list[ProcessName] = []
        self.changed_plists: set[ProcessName] = set()
        self.switched = False

    # --- bookkeeping ---
    @property
    def dry(self) -> bool:
        return self.options.dry_run

    def record(self, name: str, status: Status, detail: str = "") -> None:
        detail = detail.replace(str(self.paths.data), "DATA").replace(str(self.paths.home), "~")
        self.steps.append(Step(name, status, detail))
        self.say(f"  [{status}] {name}{': ' + detail if detail else ''}\n")

    def problem(self, name: str, detail: str) -> None:
        """A failed step: a real install stops here; a dry run notes it and goes on to show the rest."""
        self.record(name, Status.FAILED, detail)
        if not self.dry:
            raise InstallAborted(detail)

    def say(self, text: str) -> None:
        self.out.write(redact.scrubbed(text))
        self.out.flush()

    def would(self, name: str, argv: Sequence[object]) -> None:
        self.record(name, Status.DRY_RUN, "would run: " + " ".join(str(part) for part in argv))

    def run(self, argv: Sequence[object], *, cwd: Path | None = None, timeout: float) -> Completed:
        """`argv` items are `str` or `Path`; each is passed as `str(item)`."""
        return self.run_cmd([str(part) for part in argv], cwd=cwd, timeout=timeout)

    # --- entry ---
    def install(self) -> int:
        old_umask = os.umask(0o077)
        mode = "DRY RUN (temp HOME, no launchctl, no downloads)" if self.dry else "INSTALL"
        self.say(f"Yordamchi installer, {mode}\n  HOME = {self.paths.home}\n  DATA = {self.paths.data}\n")
        try:
            if self.options.env_only:
                self.data_tree()
                self.env_files()
                self.restart_after_env()
            else:
                self.preflight()
                self.toolchain()
                self.data_tree()
                self.master_key()
                self.env_files()
                self.resolve_release()
                self.build_releases()
                self.ffmpeg()
                self.stop_bots()
                self.backup_dbs()
                self.migrate()
                self.switch_current()
                self.plists()
                self.launchd()
                self.final_check()
        except InstallAborted:
            self.say("\nStopped at the first failure; nothing after it was changed.\n")
            self.restart_stopped()
        finally:
            os.umask(old_umask)
        self.summary()
        return 1 if any(step.status is Status.FAILED for step in self.steps) else 0

    # --- 1. preflight ---
    def preflight(self) -> None:
        if self.machine != "darwin/arm64":
            self.problem("preflight: platform", f"needs macOS on Apple silicon, found {self.machine}")
        tools = {"uv": self.tools.uv, "claude": self.tools.claude, "gh": self.tools.gh, "node": self.tools.node}
        missing = [f"{name} ({path})" for name, path in tools.items() if not os.access(path, os.X_OK)]
        if not os.access(self.tools.git, os.X_OK):
            missing.append(f"git ({self.tools.git}; xcode-select --install)")
        if missing:
            self.problem("preflight: tools", "missing " + ", ".join(missing))
        refused = yd_proc.check_credentials([self.paths.claude_user_settings], environ=self.environ)
        if refused:
            self.problem("preflight: credentials", "refused credential sources present: " + ", ".join(refused))
        if not self.options.skip_ffmpeg and any(pin is None for pin in self.pins.values()):
            self.problem("preflight: ffmpeg pins", FFMPEG_NOT_PINNED)
        if not any(step.name.startswith("preflight") for step in self.steps):
            self.record("preflight", Status.DONE, f"{self.machine}; tools found; no refused credential source")

    # --- 2. toolchain ---
    def toolchain(self) -> None:
        version = self.run([self.tools.uv, "--version"], timeout=TIMEOUT_QUICK_S)
        found = version.text().split()[1] if version.ok and len(version.text().split()) > 1 else "?"
        if found == UV_VERSION:
            self.record("uv", Status.UNCHANGED, f"uv {UV_VERSION}")
        elif self.dry:
            self.would(f"uv {found} -> {UV_VERSION}", [self.tools.uv, "self", "update", UV_VERSION])
        elif not self.run([self.tools.uv, "self", "update", UV_VERSION], timeout=TIMEOUT_UV_S).ok:
            self.problem("uv", f"uv self update {UV_VERSION} failed (found {found})")
        else:
            self.record("uv", Status.DONE, f"{found} -> {UV_VERSION}")
        find = [self.tools.uv, "python", "find", "--managed-python", "--no-python-downloads", PYTHON_VERSION]
        if self.run(find, timeout=TIMEOUT_QUICK_S).ok:
            self.record("python", Status.UNCHANGED, f"Python {PYTHON_VERSION} (uv-managed)")
        elif self.dry:
            self.would("python", [self.tools.uv, "python", "install", PYTHON_VERSION])
        elif not self.run([self.tools.uv, "python", "install", PYTHON_VERSION], timeout=TIMEOUT_UV_S).ok:
            self.problem("python", f"uv python install {PYTHON_VERSION} failed")
        else:
            self.record("python", Status.DONE, f"installed {PYTHON_VERSION}")

    # --- 3. data tree ---
    def data_tree(self) -> None:
        changed = sum(ensure_dir(path) for path in data_dirs(self.paths))
        fixed: list[str] = []
        for bot in BotName:
            db = self.paths.db_file(bot)
            for path in (self.paths.env_file(bot), db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
                if path.exists() and (path.stat().st_mode & 0o777) != FILE_MODE:
                    path.chmod(FILE_MODE)
                    fixed.append(path.name)
        launch_agents = yd_check.launch_agents_dir(self.paths)
        launch_agents.mkdir(parents=True, exist_ok=True)  # the owner's own directory: its mode is not ours to change
        detail = f"{len(data_dirs(self.paths))} dirs at 0700 ({changed} created or fixed)"
        self.record("data tree", Status.DONE if changed or fixed else Status.UNCHANGED, detail + _fixed(fixed))

    # --- 4. master key (in the Keychain) ---
    def master_key(self) -> None:
        self._master_key_item()
        exclusion = [self.tools.tmutil, "addexclusion", self.paths.config]
        if self.dry:
            self.would("time machine exclusion", exclusion)
        elif not self.run(exclusion, timeout=TIMEOUT_QUICK_S).ok:
            self.record(
                "time machine exclusion", Status.WARN, f"tmutil addexclusion failed; run it by hand: {exclusion}"
            )
        else:
            self.record("time machine exclusion", Status.DONE, str(self.paths.config))

    def _master_key_item(self) -> None:
        item, legacy = yd_keychain.MASTER_KEY, self.paths.master_key
        if self.dry:
            what = "import the legacy master.key into" if legacy.exists() else "keep or create it in"
            self.record("master key", Status.DRY_RUN, f"would {what} the Keychain as {item} (never in a dry run)")
            return
        try:
            stored = self.keychain.read(item)
            if legacy.exists():
                self._import_master_key(stored)
            elif stored is None:
                self.keychain.write(item, base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode("ascii"))
                self.record("master key", Status.DONE, f"created in the Keychain as {item}: 32 random bytes, base64")
            elif _master_key_bytes(stored) is None:
                self.problem(
                    "master key", f"the Keychain item {item} is not base64 of 32 bytes; it is never overwritten"
                )
            else:
                self.record("master key", Status.UNCHANGED, f"kept in the Keychain as {item}, never overwritten")
        except KeychainUnavailable:
            self.problem("master key", f"the Keychain did not answer for {item}: log in at the Mac and run again")

    def _import_master_key(self, stored: str | None) -> None:
        """A legacy `master.key` file: into the Keychain, read back, compared, and only then deleted."""
        item, legacy = yd_keychain.MASTER_KEY, self.paths.master_key
        raw = _master_key_bytes(legacy.read_bytes().decode("ascii", errors="replace"))
        if raw is None:
            self.problem("master key", f"{legacy} is not base64 of 32 bytes; nothing imported, the file is kept")
            return
        if stored is None:
            self.keychain.write(item, base64.b64encode(raw).decode("ascii"))  # verified by reading it back
            detail = f"imported {legacy.name} into the Keychain as {item}, verified, deleted the file"
        elif _master_key_bytes(stored) == raw:
            detail = f"the Keychain item {item} already holds this key; deleted the leftover {legacy.name}"
        else:
            self.problem(
                "master key",
                f"the Keychain item {item} holds a different key than {legacy}; nothing was changed "
                "(docs/DEPLOY.md, master key conflict)",
            )
            return
        legacy.unlink()
        self.record("master key", Status.DONE, detail)

    # --- 5. env files ---
    def env_files(self) -> None:
        read = {bot: self._read_env(bot) for bot in BotName}
        owners = [values.get("OWNER_ID", "") for values in read.values() if values is not None]
        known_owner = next((owner for owner in owners if env_problem("OWNER_ID", owner) is None), "")
        for bot, values in read.items():
            if values is not None:
                self._env_file(bot, values, known_owner)

    def _read_env(self, bot: BotName) -> dict[str, str] | None:
        path = self.paths.env_file(bot)
        if not path.exists():
            return {}
        try:
            return yd_check.read_env_values(path)
        except ConfigInvalid as exc:
            self.problem(f"{path.name}", f"malformed ({exc.params.get('key')}: {exc.params.get('problem')}); fix it")
            return None

    def _env_file(self, bot: BotName, values: Mapping[str, str], known_owner: str) -> None:
        path = self.paths.env_file(bot)
        if bot is BotName.PERSONAL and not path.exists() and not self.options.enable_personal:
            self.record(path.name, Status.UNCHANGED, "not created before P3 (--enable-personal)")
            return
        specs = ENV_SPECS[bot]
        invalid = [
            f"{spec.name} ({problem})"
            for spec in specs
            if values.get(spec.name) and (problem := file_value_problem(spec.name, values[spec.name])) is not None
        ]
        if invalid:
            self.problem(path.name, "invalid value, fix it by hand (it is never overwritten): " + "; ".join(invalid))
            return
        try:
            plan = self._collect(specs, values, known_owner)
        except KeychainUnavailable as exc:
            self.problem(path.name, f"the Keychain did not answer for {exc.params['key']}: log in at the Mac, rerun")
            return
        text = (
            path.read_text(encoding="utf-8") if path.exists() else ENV_HEADER + "".join(f"{s.name}=\n" for s in specs)
        )
        new_text, changed = yd_check.update_env_text(text, plan.updates, replace=plan.replace)
        if new_text != text or not path.exists():
            yd_check.write_env_file(path, new_text)
        path.chmod(FILE_MODE)
        self._report_env(bot, specs, values, plan, changed)

    def _report_env(
        self, bot: BotName, specs: Sequence[EnvKey], values: Mapping[str, str], plan: EnvPlan, changed: Sequence[str]
    ) -> None:
        name = self.paths.env_file(bot).name
        kept = [spec.name for spec in specs if values.get(spec.name) and spec.name not in plan.replace]
        detail = f"kept {kept or '-'}, set {list(changed) or '-'}, empty {plan.missing or '-'}; mode 0600"
        required = bot is BotName.DEV or self.options.enable_personal
        required_missing = [spec.name for spec in specs if required and spec.required and spec.name in plan.missing]
        if self.dry and (plan.missing or plan.rotatable or plan.moved):
            asked = [*plan.missing, *plan.rotatable]
            moved = f"; would move plaintext {plan.moved} into the Keychain" if plan.moved else ""
            self.record(name, Status.DRY_RUN, f"{detail}. Would ask at the terminal for {asked} (tokens hidden){moved}")
        elif required_missing:
            self.problem(name, f"{detail}. Required and still empty: {required_missing}")
        elif plan.moved:
            self.record(name, Status.WARN, f"{detail}. Moved plaintext {plan.moved} into the Keychain: {ROTATE_ADVICE}")
        elif plan.missing and any(key in {"OWNER_ID", "DEV_GROUP_ID"} for key in plan.missing):
            self.record(name, Status.WARN, detail + ". Find the ids: python -m yordamchi.check --discover-ids")
        else:
            self.record(name, Status.DONE if changed else Status.UNCHANGED, detail)

    def _collect(self, specs: Sequence[EnvKey], values: Mapping[str, str], known_owner: str) -> EnvPlan:
        plan = EnvPlan()
        for spec in specs:
            current = values.get(spec.name, "")
            if spec.secret:
                self._plan_token(spec, current, plan)
            elif current:
                continue  # an existing value is never overwritten
            elif spec.name == "OWNER_ID" and known_owner:
                plan.updates[spec.name] = known_owner  # the same owner in both files
            elif new := self._ask(spec):
                plan.updates[spec.name] = new
            else:
                plan.missing.append(spec.name)
        return plan

    def _plan_token(self, spec: EnvKey, current: str, plan: EnvPlan) -> None:
        """The token goes to its Keychain item (written through stdin, read back); the env file only ever gets the
        item's reference. A dry run never reads or writes the Keychain and never asks."""
        item = yd_keychain.TOKEN_ITEMS[spec.name]
        plaintext = current if current and not yd_keychain.is_reference(current) else ""
        if self.dry:
            if plaintext:
                plan.moved.append(spec.name)
            elif not current:
                plan.missing.append(spec.name)
            elif self.options.rotate_tokens:
                plan.rotatable.append(spec.name)
            return
        stored = self.keychain.read(item)
        if self.options.rotate_tokens and (stored or plaintext):
            new = self._ask(spec, rotate_from=stored or plaintext)
            if new:
                self.keychain.write(item, new)
                _point_at(item, current, spec.name, plan)
                return
        if plaintext:
            if stored is None:
                self.keychain.write(item, plaintext)
            plan.moved.append(spec.name)
            _point_at(item, current, spec.name, plan)
        elif stored is not None:
            _point_at(item, current, spec.name, plan)  # an empty line is filled; a reference stays as it is
        elif new := self._ask(spec):
            self.keychain.write(item, new)
            _point_at(item, current, spec.name, plan)
        else:
            plan.missing.append(spec.name)

    def _ask(self, spec: EnvKey, *, rotate_from: str = "") -> str:
        # A dry run never asks: a real token typed into a throwaway HOME would be a copy nobody cleans up.
        if self.dry or not self.prompter.interactive:
            return ""
        prompt = f"New {spec.prompt}" if rotate_from else spec.prompt
        for _ in range(TOKEN_PROMPT_TRIES):
            value = self.prompter.secret(prompt) if spec.secret else self.prompter.text(prompt)
            if not value:
                return ""
            if spec.secret:
                redact.add_known_secrets([value])
            problem = env_problem(spec.name, value)
            if problem is None and value == rotate_from:
                problem = "that is the old token; revoke it in BotFather (/revoke) and paste the new one"
            if problem is None:
                return value
            self.say(f"    {spec.name}: {problem}. Try again (empty to skip).\n")
        return ""

    def restart_after_env(self) -> None:
        """`--env-only`: the bots read their env file at start, so a loaded bot is restarted to pick up new tokens."""
        for process in ("dev", "personal"):
            target = f"gui/{self.options.uid}/{yd_check.label(process)}"
            if self.dry:
                self.would(f"restart {process}", [self.tools.launchctl, "kickstart", "-k", target])
                continue
            if not self.run([self.tools.launchctl, "print", target], timeout=TIMEOUT_LAUNCHCTL_S).ok:
                self.record(f"restart {process}", Status.UNCHANGED, "not loaded")
            elif self.run([self.tools.launchctl, "kickstart", "-k", target], timeout=TIMEOUT_LAUNCHCTL_S).ok:
                self.record(f"restart {process}", Status.DONE, "kickstart -k")
            else:
                self.record(f"restart {process}", Status.WARN, f"kickstart failed: launchctl kickstart -k {target}")

    # --- 6, 7. releases ---
    def resolve_release(self) -> None:
        repo = self.options.repo
        sha = self.run(
            [self.tools.git, "-C", repo, "rev-parse", "--verify", "--quiet", f"{self.options.commit}^{{commit}}"],
            timeout=TIMEOUT_GIT_S,
        )
        if not sha.ok or not re.fullmatch(r"[0-9a-f]{40}", sha.text()):
            self.record("release", Status.FAILED, f"{self.options.commit!r} is not a commit in {repo}")
            raise InstallAborted(self.options.commit)
        day = self.run(
            [self.tools.git, "-C", repo, "log", "-1", "--format=%cd", "--date=format:%Y.%m.%d", sha.text()],
            timeout=TIMEOUT_GIT_S,
        )
        if not day.ok or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", day.text()):
            self.record("release", Status.FAILED, "could not read the commit date")
            raise InstallAborted(sha.text())
        self.releases.sha = sha.text()
        self.releases.version = f"{day.text()}-{sha.text()[:7]}"
        self.record("release", Status.DONE, f"{self.releases.version} (commit {self.releases.sha})")

    def build_releases(self) -> None:
        version, sha = self.releases.version, self.releases.sha
        self.releases.app = self._build("app release", self.paths.releases / version, sha)
        self.releases.supervisor = self._build(
            "supervisor release", self.paths.supervisor / "releases" / version, f"{sha}:supervisor"
        )

    def _build(self, name: str, target: Path, treeish: str) -> Path | None:
        marker = target / RELEASE_MARKER
        if marker.exists():
            make_read_only(target)
            self.record(name, Status.UNCHANGED, f"{target} (already built, read-only)")
            return target
        if target.exists():  # a build that stopped half way: start it again
            make_writable(target)
            shutil.rmtree(target)
        archive = self.run(
            [self.tools.git, "-C", self.options.repo, "archive", "--format=tar", treeish], timeout=TIMEOUT_GIT_S
        )
        if not archive.ok:
            self.problem(name, f"git archive {treeish} failed")
            return None
        target.mkdir(mode=DIR_MODE)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            tar.extractall(target, filter="data")
        sync = [
            self.tools.uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--compile-bytecode",
            "--link-mode",
            "clone",  # never hardlink: chmod -R a-w must not reach into uv's cache
            "--managed-python",
            "--python",
            PYTHON_VERSION,
        ]
        if self.dry:
            sync += ["--offline", "--no-python-downloads"]
        result = self.run(sync, cwd=target, timeout=TIMEOUT_UV_S)
        if not result.ok:
            tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["?"]
            self.problem(name, f"uv sync failed in {target}: {tail[0]}")
            return None
        info = {"version": target.name, "treeish": treeish, "python": PYTHON_VERSION, "built_by": "scripts/install.py"}
        marker.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        make_read_only(target)
        self.record(name, Status.DONE, f"{target} (git archive {treeish}, uv sync --frozen, chmod -R a-w)")
        return target

    # --- 8. ffmpeg ---
    def ffmpeg(self) -> None:
        if self.options.skip_ffmpeg:
            self.record("ffmpeg", Status.WARN, "skipped (--skip-ffmpeg): voice replies are degraded until installed")
            return
        pins = {name: pin for name, pin in self.pins.items() if pin is not None}
        if len(pins) != len(self.pins):
            self.record("ffmpeg", Status.BLOCKED, "not pinned (see preflight)")
            return
        marker = self.paths.bin / FFMPEG_MARKER
        wanted = {name: pin.sha256 for name, pin in pins.items()}
        if marker.exists() and all((self.paths.bin / name).exists() for name in pins):
            try:
                recorded = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                recorded = None
            if recorded == wanted:
                self.record("ffmpeg", Status.UNCHANGED, f"{FFMPEG_VERSION} in {self.paths.bin}")
                return
        if self.dry:
            for name, pin in pins.items():
                self.record(f"ffmpeg: {name}", Status.DRY_RUN, f"would download {pin.url} (sha256 {pin.sha256})")
            return
        for name, pin in pins.items():
            self._install_binary(name, pin)
        encoders = self.run([self.paths.bin / "ffmpeg", "-hide_banner", "-encoders"], timeout=TIMEOUT_QUICK_S)
        if not encoders.ok or "libopus" not in encoders.stdout.decode("utf-8", errors="replace"):
            self.problem("ffmpeg", "ffmpeg -encoders does not list libopus")
            return
        write_private(marker, json.dumps(wanted).encode())
        self.record("ffmpeg", Status.DONE, f"{FFMPEG_VERSION} arm64, SHA-256 verified, libopus present")

    def _install_binary(self, name: str, pin: Pin) -> None:
        zipped = self.paths.bin / f".{name}.download.zip"
        zipped.unlink(missing_ok=True)
        try:
            self.download(pin.url, zipped, timeout=TIMEOUT_DOWNLOAD_S)
            digest = hashlib.sha256(zipped.read_bytes()).hexdigest()
            if digest != pin.sha256.lower():
                self.problem(f"ffmpeg: {name}", f"SHA-256 mismatch (got {digest}); nothing installed")
                return
            with zipfile.ZipFile(zipped) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and Path(m.filename).name == name]
                if len(members) != 1:
                    self.problem(f"ffmpeg: {name}", f"the zip has no single '{name}' binary")
                    return
                data = archive.read(members[0])
            write_private(self.paths.bin / name, data, mode=DIR_MODE)  # 0700: executable by the owner only
        except OSError as exc:
            self.problem(f"ffmpeg: {name}", f"download failed ({type(exc).__name__})")
        finally:
            zipped.unlink(missing_ok=True)

    # --- 9. stop, back up, migrate ---
    def _target(self, process: ProcessName) -> str:
        return f"gui/{self.options.uid}/{yd_check.label(process)}"

    def _loaded(self, process: ProcessName) -> bool:
        return self.run([self.tools.launchctl, "print", self._target(process)], timeout=TIMEOUT_LAUNCHCTL_S).ok

    def _stop(self, process: ProcessName) -> None:
        """`bootout`, then wait until launchd has really let the job go: bootout returns before the job is gone, and a
        `bootstrap` or `kickstart` meanwhile fails (2026-10-25: the dev bot stayed down after two updates)."""
        self.run([self.tools.launchctl, "bootout", self._target(process)], timeout=TIMEOUT_LAUNCHCTL_S)
        waited = 0.0
        while self._loaded(process) and waited < UNLOAD_WAIT_S:
            self.pause(UNLOAD_POLL_S)
            waited += UNLOAD_POLL_S

    def stop_bots(self) -> None:
        if self.dry:
            self.record("stop bots", Status.DRY_RUN, "launchctl is never called in a dry run")
            return
        for process in ("dev", "personal"):
            if self._loaded(process):
                self._stop(process)
                self.stopped.append(process)
        self.record("stop bots", Status.DONE if self.stopped else Status.UNCHANGED, f"stopped {self.stopped or '-'}")

    def restart_stopped(self) -> None:
        """After an abort before the switch, the bots this run stopped go back up on the release they ran before."""
        if self.switched or not self.stopped:
            return  # nothing stopped, or `current` already moved (the launchd step then decides)
        for process in self.stopped:
            plist = yd_check.plist_path(self.paths, process)
            ok = self.run(
                [self.tools.launchctl, "bootstrap", f"gui/{self.options.uid}", plist], timeout=TIMEOUT_LAUNCHCTL_S
            ).ok
            self.record(f"restart {process}", Status.DONE if ok else Status.WARN, "back on the previous release")

    def backup_dbs(self) -> None:
        existing = [self.paths.db_file(bot) for bot in BotName if self.paths.db_file(bot).exists()]
        if not existing:
            self.record("db backup", Status.UNCHANGED, "no database yet (first install)")
            return
        target = self.paths.backups / "pre-update" / f"install-{self.releases.version}-{self.stamp()}"
        ensure_dir(target)
        for db in existing:
            copy = target / db.name
            verdict = _sqlite_backup(db, copy)
            copy.chmod(FILE_MODE)
            if verdict != "ok":
                self.problem("db backup", f"{copy} failed integrity_check ({verdict}); migrations not run")
                return
        self.record("db backup", Status.DONE, f"{[db.name for db in existing]} -> {target} (integrity ok)")

    def migrate(self) -> None:
        app = self.releases.app
        python = app / ".venv" / "bin" / "python" if app else None
        if app is None or python is None or not python.exists():
            self.record("migrations", Status.BLOCKED, "no built app release")
            return
        for bot in BotName:
            db = self.paths.db_file(bot)
            argv = [python, "-I", "-m", "alembic", "-c", app / "alembic.ini", "-x", f"db={db}", "upgrade", "head"]
            result = self.run(argv, cwd=app, timeout=TIMEOUT_MIGRATE_S)
            if not result.ok:
                tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["?"]
                self.problem("migrations", f"alembic upgrade head on {db.name} failed: {tail[0]}")
                return
            for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
                if path.exists():
                    path.chmod(FILE_MODE)
        self.record("migrations", Status.DONE, "alembic upgrade head on dev.db and personal.db")

    # --- 10. current ---
    def switch_current(self) -> None:
        app, supervisor = self.releases.app, self.releases.supervisor
        if app is None or supervisor is None:
            self.record("switch current", Status.BLOCKED, "a release did not build")
            return
        self.switched = True
        self.releases.previous_app = swap_symlink(self.paths.current_release, app)
        self.releases.previous_supervisor = swap_symlink(self.paths.supervisor / "current", supervisor)
        self.record(
            "switch current",
            Status.DONE,
            f"releases/current -> {app.name} (was {self.releases.previous_app or '-'}), "
            f"supervisor/current -> releases/{supervisor.name} (was {self.releases.previous_supervisor or '-'})",
        )

    # --- 11. plists ---
    def installed_processes(self) -> list[ProcessName]:
        return ["dev", "personal", "supervisor"] if self.options.enable_personal else ["dev", "supervisor"]

    def plists(self) -> None:
        source = (self.releases.app or self.options.repo) / "ops" / "launchd"
        values = plist_values(self.paths, self.options.repo)
        rendered: list[str] = []
        for process in ("dev", "personal", "supervisor"):
            name = yd_check.label(process)
            try:
                text = render_plist((source / f"{name}.plist.tmpl").read_text(encoding="utf-8"), values)
            except (OSError, ValueError) as exc:
                self.problem("plists", f"{name}: {type(exc).__name__}")
                return
            write_private(staging_dir(self.paths) / f"{name}.plist", text.encode())
            if process not in self.installed_processes():
                continue
            target = yd_check.plist_path(self.paths, process)
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                write_private(target, text.encode(), mode=PLIST_MODE)
                self.changed_plists.add(process)
            rendered.append(name)
        held = "" if self.options.enable_personal else "; uz.yordamchi.personal rendered to DATA/launchd only (P3)"
        self.record("plists", Status.DONE, f"installed {rendered} in ~/Library/LaunchAgents{held}")

    # --- 12. launchd and the check ---
    def launchd(self) -> None:
        if self.dry:
            for process in self.installed_processes():
                self.would(
                    f"launchd {process}",
                    [
                        self.tools.launchctl,
                        "bootstrap",
                        f"gui/{self.options.uid}",
                        yd_check.plist_path(self.paths, process),
                    ],
                )
            return
        if self.releases.app is None or self.releases.supervisor is None:
            self.record("launchd", Status.BLOCKED, "no release to run")
            return
        for process in self.installed_processes():
            target, plist = self._target(process), yd_check.plist_path(self.paths, process)
            loaded = self._loaded(process)
            if loaded and process not in self.changed_plists:
                ok = self.run([self.tools.launchctl, "kickstart", "-k", target], timeout=TIMEOUT_LAUNCHCTL_S).ok
                how = "kickstart -k"
                if not ok:
                    # The job was in transition (still unloading, or brought back meanwhile): start it afresh.
                    self._stop(process)
                    ok = self.run(
                        [self.tools.launchctl, "bootstrap", f"gui/{self.options.uid}", plist],
                        timeout=TIMEOUT_LAUNCHCTL_S,
                    ).ok
                    how = "kickstart -k failed; bootout + bootstrap"
            else:
                if loaded:
                    self._stop(process)
                self.run([self.tools.launchctl, "enable", target], timeout=TIMEOUT_LAUNCHCTL_S)
                ok = self.run(
                    [self.tools.launchctl, "bootstrap", f"gui/{self.options.uid}", plist], timeout=TIMEOUT_LAUNCHCTL_S
                ).ok
                how = "bootstrap"
            if not ok:
                self.problem(f"launchd {process}", f"launchctl {how} failed for {target}")
                return
            self.record(f"launchd {process}", Status.DONE, how)

    def final_check(self) -> None:
        python = self.paths.current_release / ".venv" / "bin" / "python"
        argv = [python, "-I", "-m", "yordamchi.check", "--no-colour"]
        if self.dry:
            self.would("check", argv)
            return
        result = self.run(argv, cwd=self.paths.run, timeout=TIMEOUT_CHECK_S)
        self.say(redact.scrubbed(result.stdout.decode("utf-8", errors="replace")))
        if result.ok:
            self.record("check", Status.DONE, "all green")
        else:
            self.record("check", Status.WARN, "red lines above: fix them, then run python -m yordamchi.check")

    # --- summary ---
    def summary(self) -> None:
        width = max((len(step.name) for step in self.steps), default=10)
        lines = ["", "Summary", "-------"]
        lines += [f"  {step.status.value:<10} {step.name:<{width}}  {step.detail}" for step in self.steps]
        counts = {status: sum(1 for step in self.steps if step.status is status) for status in Status}
        lines.append("  " + ", ".join(f"{count} {status.value}" for status, count in counts.items() if count))
        if self.releases.previous_app or self.releases.previous_supervisor:
            lines += [
                "",
                "Rollback (docs/DEPLOY.md):",
                f"  point releases/current back at {self.releases.previous_app or '(none)'}",
                f"  point supervisor/current back at {self.releases.previous_supervisor or '(none)'}",
                "  then launchctl kickstart -k each job; DBs: backups/pre-update/install-*",
            ]
        if self.dry:
            lines += ["", f"Dry run only. Everything above was written under {self.paths.home}."]
        self.say("\n".join(lines) + "\n")


def _point_at(item: yd_keychain.KeychainItem, current: str, key: str, plan: EnvPlan) -> None:
    """The env line becomes the item's reference; a plaintext token on it is replaced."""
    if current == item.reference:
        return
    plan.updates[key] = item.reference
    if current:
        plan.replace.add(key)


def _fixed(names: list[str]) -> str:
    return f"; fixed modes of {names}" if names else ""


# --- command line ----------------------------------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Yordamchi on this Mac (docs/DEPLOY.md).")
    parser.add_argument(
        "--release", default="HEAD", help="the commit to install (default HEAD); never the working copy"
    )
    parser.add_argument("--repo", type=Path, default=REPO, help="the git repository (default: this checkout)")
    parser.add_argument("--dry-run", action="store_true", help="build everything under a temp HOME; no launchctl")
    parser.add_argument("--home", type=Path, help="the HOME a dry run builds into (default: a new temp dir)")
    parser.add_argument("--rotate-tokens", action="store_true", help="ask for new bot tokens and replace them")
    parser.add_argument("--env-only", action="store_true", help="only the env files (and restart loaded bots)")
    parser.add_argument("--enable-personal", action="store_true", help="P3: install and load the personal bot")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="install without ffmpeg (voice degraded)")
    parser.add_argument("--yes", action="store_true", help="do not ask before a real install")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    environ = dict(os.environ)  # judged by the refuse-list and cleaned for children, never logged
    real_home = Path.home()
    if args.dry_run:
        home = (args.home or Path(tempfile.mkdtemp(prefix="yordamchi-dry-run-"))).resolve()
        if home == real_home.resolve():
            sys.stderr.write("--dry-run must not use the real HOME\n")
            return 2
    else:
        if args.home is not None:
            sys.stderr.write("--home is for --dry-run only; a real install uses your HOME\n")
            return 2
        home = real_home
        if not args.yes:
            if not sys.stdin.isatty():
                sys.stderr.write("a real install needs a terminal (or --yes); try --dry-run first\n")
                return 2
            answer = input(f"Install Yordamchi {args.release} into {home} and load it into launchd? Type 'install': ")
            if answer.strip() != "install":
                sys.stderr.write("cancelled; nothing changed\n")
                return 1
    options = Options(
        home=home,
        repo=args.repo.resolve(),
        commit=args.release,
        dry_run=args.dry_run,
        rotate_tokens=args.rotate_tokens,
        env_only=args.env_only,
        enable_personal=args.enable_personal,
        skip_ffmpeg=args.skip_ffmpeg,
    )
    installer = Installer(
        options,
        tools=Tools.under(real_home),
        runner=subprocess_runner(child_env(environ, real_home)),
        download=urllib_download,
        prompter=TerminalPrompter(),
        keychain=SecurityKeychain(),
        environ=environ,
        out=sys.stdout,
    )
    return installer.install()


if __name__ == "__main__":
    raise SystemExit(main())
