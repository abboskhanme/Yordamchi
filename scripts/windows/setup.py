"""`uv run python scripts\\windows\\setup.py`: the one-time setup of the personal bot on Windows (owner request
2026-10-31: other people run only the personal bot, from a terminal, straight on their Claude login).

What `scripts/install.py` does for the personal bot on the Mac, minus launchd, releases and the supervisor:
1. the data tree under `%LOCALAPPDATA%\\Yordamchi` (`core.config.load_paths(platform="win32")`, the same resolution
   the bot uses; `YORDAMCHI_DATA_DIR` / `YORDAMCHI_CONFIG_DIR` / `YORDAMCHI_LOG_DIR` still override);
2. the vault master key (`uz.yordamchi/vault-master-key`, base64 of 32 random bytes) in the DPAPI store, created only
   if absent and never overwritten, so a re-run keeps every sealed vault row readable;
3. `DATA\\personal.env`: the bot token goes into the DPAPI store and the file gets only its reference
   (`@keychain:uz.yordamchi/personal-bot-token`), plus `OWNER_ID` and the optional `PERSONAL_GROUP_ID`. A value
   already in the file is kept; a flag given on the command line replaces it;
4. `alembic upgrade head` on `DATA\\db\\personal.db` with this checkout's interpreter (`python -I -m alembic`);
5. the config is read back exactly as the bot reads it (`core.config.load_personal_config`), so a typo fails here.

The token comes from a hidden prompt (`getpass`) on a terminal, or from one line of stdin (`--token-stdin`, or no
terminal: CI). It never reaches argv, a file other than its DPAPI blob, or the output (`core.redact` scrubs every
printed line). Standard library plus `yordamchi` from this checkout's venv; the seams (`runner`, `prompter`, `store`,
`platform`) let the tests run it on macOS with a fake DPAPI.
"""

import argparse
import getpass
import os
import re
import subprocess  # a setup script, sync by nature: core.proc is async-only (as in scripts/install.py)
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, Protocol, TextIO

REPO: Final = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from yordamchi.adapters.keystore.aead import decode_key, encode_key  # needs the sys.path entry above
from yordamchi.bots.cli import check as yd_check
from yordamchi.core import config as yd_config
from yordamchi.core import keychain as yd_keychain
from yordamchi.core import redact
from yordamchi.core.errors import ConfigInvalid, KeychainItemMissing, KeychainUnavailable
from yordamchi.core.paths import BotName, Paths

WINDOWS: Final = "win32"
BOT: Final = BotName.PERSONAL
TOKEN_KEY: Final = "PERSONAL_BOT_TOKEN"  # noqa: S105  # a variable name, not a token
OWNER_KEY: Final = "OWNER_ID"
GROUP_KEY: Final = "PERSONAL_GROUP_ID"
ENV_KEYS: Final = (TOKEN_KEY, OWNER_KEY, GROUP_KEY)
# The same grammar `core.config` and `scripts/install.py` enforce (tests/unit/ops/test_windows_setup.py keeps them in
# step).
BOT_TOKEN: Final = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
GROUP_ID: Final = re.compile(r"-\d+")
MASTER_KEY_BYTES: Final = 32
PROMPT_TRIES: Final = 3
TIMEOUT_MIGRATE_S: Final = 300.0
SESSION_KINDS: Final = ("assistant", "smoke")
ENV_HEADER: Final = (
    "# Written by scripts/windows/setup.py. Never commit this file and never paste it into chat.\n"
    "# The token is not here: PERSONAL_BOT_TOKEN names its DPAPI-sealed item (@keychain:<service>/<account>).\n"
)
PROMPTS: Final[Mapping[str, str]] = {
    TOKEN_KEY: "Personal bot token from BotFather (hidden): ",
    OWNER_KEY: "Your Telegram user id: ",
    GROUP_KEY: "Personal group id (optional, empty = DM): ",
}
# Client-facing (Uzbek): what to do once setup is green. ASCII apostrophes, so a cp1252 console prints them too.
NEXT_STEPS: Final = (
    "\nTayyor. Botni ishga tushirish:  scripts\\windows\\run.cmd\n"
    "Keyin botga Telegramda yozing. Ovozli xabarlar uchun Gemini API kalitingizni botga shu chatda yuboring:\n"
    "bot uni o'zining shifrlangan seyfiga saqlaydi.\n"
    "Bot har 3 kunda GitHub'dagi yangilanishni tekshiradi va o'zini o'zi yangilaydi.\n"
)


# --- seams the tests replace -------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> Completed: ...


class Prompter(Protocol):
    @property
    def interactive(self) -> bool: ...

    def secret(self, prompt: str) -> str: ...

    def text(self, prompt: str) -> str: ...

    def line(self) -> str:
        """One line of stdin, no prompt (`--token-stdin`, CI)."""
        ...


def subprocess_runner(argv: Sequence[str], *, cwd: Path, timeout: float) -> Completed:
    try:
        done = subprocess.run(list(argv), cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Completed(returncode=124, stderr=b"timed out")
    except OSError as exc:
        return Completed(returncode=127, stderr=type(exc).__name__.encode())
    return Completed(done.returncode, done.stdout, done.stderr)


class TerminalPrompter:
    def __init__(self, stdin: TextIO = sys.stdin) -> None:
        self._stdin = stdin

    @property
    def interactive(self) -> bool:
        return self._stdin.isatty()

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt).strip()

    def text(self, prompt: str) -> str:
        return input(prompt).strip()

    def line(self) -> str:
        return self._stdin.readline().strip()


# --- options and steps -------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Options:
    owner_id: str | None = None
    group_id: str | None = None
    token_stdin: bool = False


class Status(StrEnum):
    DONE = "done"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    status: Status
    detail: str = ""


class SetupAborted(Exception):
    """Setup stops at the first failure, so nothing after a broken step changes this machine."""


def value_problem(key: str, value: str) -> str | None:
    """Why `value` is not acceptable for `key`, or None (`scripts/install.py env_problem`, personal keys)."""
    if key == TOKEN_KEY:
        return None if BOT_TOKEN.fullmatch(value) else "not a BotFather token (digits:35 characters)"
    if key == OWNER_KEY:
        return None if value.isdigit() and int(value) > 0 else "not a Telegram user id (a positive number)"
    if key == GROUP_KEY:
        return None if GROUP_ID.fullmatch(value) else "not a group id (a negative number)"
    return None


def data_dirs(paths: Paths) -> list[Path]:
    """The part of `scripts/install.py data_dirs` the personal bot uses on its own (no releases, no supervisor)."""
    return [
        paths.data,
        paths.db_dir,
        paths.run,
        paths.flags,
        paths.backups,
        paths.backups / "pre-update",
        paths.media,
        paths.data / "sessions",
        *(paths.session_dir(kind) for kind in SESSION_KINDS),
        paths.bin,
        paths.logs,
        paths.config,
        paths.secret_store,
    ]


def write_env_file(path: Path, text: str) -> None:
    """Atomic replace through a temp file in the same directory (0600 where the file system has modes). Not
    `bots.cli.check.write_env_file`: it calls `os.fchmod`, which Python 3.12 lacks on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    path.chmod(0o600)


# --- setup -------------------------------------------------------------------------------------------------------
class Setup:
    def __init__(
        self,
        options: Options,
        *,
        environ: Mapping[str, str],
        out: TextIO,
        runner: Runner = subprocess_runner,
        prompter: Prompter | None = None,
        home: Path | None = None,
        repo: Path = REPO,
        python: Path | None = None,
        store: yd_keychain.Store | None = None,
        platform: str = WINDOWS,
    ) -> None:
        self.options = options
        self.environ = environ
        self.out = out
        self.runner = runner
        self.prompter: Prompter = prompter or TerminalPrompter()
        self.home = home
        self.repo = repo
        self.python = python or Path(sys.executable)
        self.platform = platform
        self.paths = yd_config.load_paths(environ, home=home, platform=platform)
        self.store = store or yd_keychain.default_store(self.paths, platform=platform)
        self.chain = yd_keychain.Keychain(BOT, store=self.store)
        self.steps: list[Step] = []

    # --- bookkeeping ---
    def say(self, text: str) -> None:
        self.out.write(redact.scrubbed(text))
        self.out.flush()

    def record(self, name: str, status: Status, detail: str = "") -> None:
        detail = detail.replace(str(self.paths.data), "DATA")
        self.steps.append(Step(name, status, detail))
        self.say(f"  [{status}] {name}{': ' + detail if detail else ''}\n")

    def problem(self, name: str, detail: str) -> NoReturn:
        self.record(name, Status.FAILED, detail)
        raise SetupAborted(detail)

    # --- entry ---
    def run(self) -> int:
        self.say(f"Yordamchi personal bot setup\n  DATA   = {self.paths.data}\n  CONFIG = {self.paths.config}\n")
        try:
            self.data_tree()
            self.master_key()
            self.env_file()
            self.migrate()
            self.verify()
        except SetupAborted:
            self.say("\nSetup stopped at the failed step above; fix it and run setup again (it is safe to repeat).\n")
            return 1
        self.say(NEXT_STEPS)
        return 0

    # --- 1. directories ---
    def data_tree(self) -> None:
        created = [path for path in data_dirs(self.paths) if not path.exists()]
        for path in data_dirs(self.paths):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.record("data tree", Status.DONE if created else Status.UNCHANGED, f"{len(created)} created")

    # --- 2. master key ---
    def master_key(self) -> None:
        item = yd_keychain.MASTER_KEY
        try:
            stored = self.chain.read(item)
        except KeychainItemMissing:
            stored = None
        except KeychainUnavailable:
            self.problem("master key", f"the secret store did not answer for {item}")
        if stored is None:
            self._write(item, encode_key(os.urandom(MASTER_KEY_BYTES)), "master key")
            self.record("master key", Status.DONE, f"created as {item}: 32 random bytes, base64, DPAPI-sealed")
            return
        try:
            decode_key(stored)
        except ValueError:
            self.problem("master key", f"{item} is not base64 of 32 bytes; it is never overwritten")
        self.record("master key", Status.UNCHANGED, f"kept {item}, never overwritten")

    def _write(self, item: yd_keychain.KeychainItem, value: str, step: str) -> None:
        try:
            self.chain.write(item, value)
            if self.chain.read(item) != value:
                self.problem(step, f"{item} did not read back as written")
        except (KeychainUnavailable, KeychainItemMissing):
            self.problem(step, f"the secret store could not write {item}")

    # --- 3. env file ---
    def env_file(self) -> None:
        path = self.paths.env_file(BOT)
        try:
            values = yd_check.read_env_values(path) if path.exists() else {}
        except ConfigInvalid as exc:
            self.problem(path.name, f"malformed ({exc.params.get('key')}: {exc.params.get('problem')}); fix it")
        updates: dict[str, str] = {}
        replace: set[str] = set()
        self._token(values.get(TOKEN_KEY, ""), updates, replace)
        self._plain(OWNER_KEY, values.get(OWNER_KEY, ""), self.options.owner_id, updates, replace, required=True)
        self._plain(GROUP_KEY, values.get(GROUP_KEY, ""), self.options.group_id, updates, replace, required=False)
        text = path.read_text(encoding="utf-8") if path.exists() else ENV_HEADER + "".join(f"{k}=\n" for k in ENV_KEYS)
        new_text, changed = yd_check.update_env_text(text, updates, replace=replace)
        if new_text != text or not path.exists():
            write_env_file(path, new_text)
        kept = [key for key in ENV_KEYS if values.get(key) and key not in changed]
        status = Status.DONE if changed else Status.UNCHANGED
        self.record(path.name, status, f"kept {kept or '-'}, set {list(changed) or '-'}")

    def _token(self, current: str, updates: dict[str, str], replace: set[str]) -> None:
        item = yd_keychain.TOKEN_ITEMS[TOKEN_KEY]
        try:
            stored: str | None = self.chain.read(item)
        except KeychainItemMissing:
            stored = None
        except KeychainUnavailable:
            self.problem(TOKEN_KEY, f"the secret store did not answer for {item}")
        if current and not yd_keychain.is_reference(current):
            redact.add_known_secrets([current])
            plaintext = current  # a token typed into the file by hand: into the store, the file gets the reference
        else:
            plaintext = ""
        if current and yd_keychain.is_reference(current) and current != item.reference:
            self.problem(TOKEN_KEY, f"must reference {item}")
        new = self._ask_token() if self.options.token_stdin or (stored is None and not plaintext) else plaintext
        if new:
            if (problem := value_problem(TOKEN_KEY, new)) is not None:
                self.problem(TOKEN_KEY, problem)
            if new != stored:
                self._write(item, new, TOKEN_KEY)
        elif stored is None:
            self.problem(TOKEN_KEY, "required: paste the token from BotFather (or pipe it in with --token-stdin)")
        if current != item.reference:
            updates[TOKEN_KEY] = item.reference
            if current:
                replace.add(TOKEN_KEY)

    def _ask_token(self) -> str:
        if self.options.token_stdin or not self.prompter.interactive:
            value = self.prompter.line()
            redact.add_known_secrets([value] if value else [])
            return value
        return self._ask(TOKEN_KEY, secret=True)

    def _ask(self, key: str, *, secret: bool) -> str:
        if not self.prompter.interactive:
            return ""
        for _ in range(PROMPT_TRIES):
            value = self.prompter.secret(PROMPTS[key]) if secret else self.prompter.text(PROMPTS[key])
            if not value:
                return ""
            if secret:
                redact.add_known_secrets([value])
            problem = value_problem(key, value)
            if problem is None:
                return value
            self.say(f"    {key}: {problem}. Try again (empty to skip).\n")
        return ""

    def _plain(
        self,
        key: str,
        current: str,
        flag: str | None,
        updates: dict[str, str],
        replace: set[str],
        *,
        required: bool,
    ) -> None:
        if flag is not None:
            if (problem := value_problem(key, flag)) is not None:
                self.problem(key, problem)
            if flag != current:
                updates[key] = flag
                replace.add(key)
            return
        if current:
            if (problem := value_problem(key, current)) is not None:
                self.problem(key, f"{problem}; fix it in the env file or pass the flag")
            return
        if new := self._ask(key, secret=False):
            updates[key] = new
        elif required:
            self.problem(key, "required: pass --owner-id (your numeric Telegram user id)")

    # --- 4. migrations ---
    def migrate(self) -> None:
        db = self.paths.db_file(BOT)
        argv = [
            str(self.python),
            "-I",
            "-m",
            "alembic",
            "-c",
            str(self.repo / "alembic.ini"),
            "-x",
            f"db={db}",
            "upgrade",
            "head",
        ]
        result = self.runner(argv, cwd=self.repo, timeout=TIMEOUT_MIGRATE_S)
        if not result.ok:
            tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["?"]
            self.problem("migrations", f"alembic upgrade head on {db.name} failed: {tail[0]}")
        self.record("migrations", Status.DONE, f"alembic upgrade head on {db.name}")

    # --- 5. read back ---
    def verify(self) -> None:
        try:
            loaded = yd_config.load_personal_config(
                self.environ, home=self.home, keychain_store=self.store, platform=self.platform
            )
        except ConfigInvalid as exc:
            self.problem("config", f"{exc.params.get('key')}: {exc.params.get('problem')}")
        except (KeychainUnavailable, KeychainItemMissing):
            self.problem("config", "the token did not read back from the secret store")
        where = f"group {loaded.personal_group_id}" if loaded.personal_group_id else "DM only"
        self.record("config", Status.DONE, f"the bot reads it back: owner {loaded.owner_id}, {where}")


# --- command line ------------------------------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts\\windows\\setup.py",
        description="Set up the Yordamchi personal bot on Windows (safe to run again).",
    )
    parser.add_argument("--owner-id", help="your numeric Telegram user id (replaces the one in personal.env)")
    parser.add_argument("--group-id", help="optional personal group id, a negative number (empty = DM only)")
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the bot token from one line of stdin (CI); replaces a stored token",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")  # a cp1252 console or pipe must not crash on a non-ASCII path
    redact.install_process_hooks()
    if sys.platform != WINDOWS:
        sys.stderr.write("this setup is for Windows; on a Mac use scripts/install.py --enable-personal\n")
        return 2
    options = Options(owner_id=args.owner_id, group_id=args.group_id, token_stdin=args.token_stdin)
    return Setup(options, environ=dict(os.environ), out=sys.stdout).run()


if __name__ == "__main__":
    raise SystemExit(main())
