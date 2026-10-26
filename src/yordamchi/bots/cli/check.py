"""`python -m yordamchi.check`: is this Mac set up the way the bots need? (SPEC §9.3 step 8, §12; TASKS T28)

One line per check, in Uzbek, green (`OK`), red (`XATO`) or neutral (`ESLATMA`). The exit code is 1 when any line is
red. It runs at install time, at bot start and on "tekshir", ideally inside the launchd context, so the environment it
judges is the bots' own.

What it checks:
- modes: DATA and its `db/`, `run/` (0700), the env files and the DB files (0600), `~/.config/yordamchi` (0700);
- secrets (owner requirement 2026-10-24): no env file holds a plaintext secret (`core.config.plaintext_secrets`), every
  `@keychain:` reference names an item of that bot that exists, the vault master key is in the Keychain, and no
  legacy `master.key` file is left. Existence is checked without `-w`, so no value is read for it;
- `~/.local/bin/claude`, and `claude auth status` reporting `authMethod: "claude.ai"` (only that field is shown);
- no refused credential source in the environment or in `~/.claude/settings.json` (`core.proc`'s refuse-list);
- `claude`, `git`, `gh`, `ffmpeg`, `node` on the bots' search path (`core.proc.build_search_path`);
- the LaunchAgents are loaded. The personal bot counts once it is enabled (`scripts/install.py --enable-personal`
  put its plist in `~/Library/LaunchAgents`); before that, unloaded is not an error;
- heartbeats: the supervisor's must be fresh; a bot's must be fresh once the bot has written one (the personal
  bot's only while it is enabled: a file left from before it was switched off is not judged);
- `OWNER_ID` and a `-100…` supergroup `DEV_GROUP_ID` are set;
- Telegram setup (X01), through the Bot API with each bot's token read from the Keychain item its env file names
  (a plaintext token is not used): privacy off for the dev
  bot, join-groups off, an empty command menu, the dev group is a supergroup where the bot is an admin that can delete
  and pin, and the owner does not post there as an anonymous admin; whether the dev group has Topics on (`is_forum`:
  off is the flat group of before, so information only) and, when it does, whether the bot may open topics
  (`can_manage_topics`: without it no project gets its topic, so red) (D4 revised 2026-10-26);
- power (`pmset` AC sleep 0), free disk and FileVault (the last two for information).

`--discover-ids` calls `getUpdates` once without an offset (nothing is confirmed, so the bot still receives those
updates later), prints the candidate owner id and group ids, and offers to write the missing ones into the env files.

Tokens never reach the output or argv: `security` gets the item name only, the value is added to `core.redact`'s known
set when read, every printed line passes
`redact.scrubbed`, and no URL or exception text from a Telegram call is shown (only a short problem code). The network
is used only for the Telegram checks. Imports: `yordamchi.core` and the standard library only (A9).

The env-file editing helpers (`update_env_text`, `write_env_file`) live here so that `scripts/install.py` reuses the
same code: a key that already has a value is never overwritten unless the caller names it in `replace`.
"""

import argparse
import asyncio
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, TextIO

from yordamchi.core import config, keychain, proc, redact
from yordamchi.core.errors import ConfigInvalid
from yordamchi.core.paths import LAUNCHD_PREFIX, BotName, Paths, ProcessName

API_BASE: Final = "https://api.telegram.org"
TELEGRAM_TIMEOUT_S: Final = 10.0
CLAUDE_TIMEOUT_S: Final = 20.0
LAUNCHCTL_TIMEOUT_S: Final = 10.0
SYSTEM_TIMEOUT_S: Final = 10.0
LAUNCHCTL: Final = "/bin/launchctl"
PMSET: Final = "/usr/bin/pmset"
FDESETUP: Final = "/usr/bin/fdesetup"
EXPECTED_AUTH_METHOD: Final = "claude.ai"
KEYCHAIN_TIMEOUT_S: Final = 10.0
ROTATE_HINT: Final = "python scripts/install.py --env-only --rotate-tokens"
INSTALL_HINT: Final = "python scripts/install.py"
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
# The supervisor writes its heartbeat every 15 s; a bot's is judged like the watchdog judges it (SPEC §2.8).
SUPERVISOR_HEARTBEAT_MAX_AGE_S: Final = 60.0
BOT_HEARTBEAT_MAX_AGE_S: Final = 120.0
CHECKED_BINARIES: Final = ("claude", "git", "gh", "ffmpeg", "node")
LOADED_LABELS: Final[tuple[ProcessName, ...]] = ("dev", "supervisor")  # personal: once enabled
ENABLE_PERSONAL_HINT: Final = "python scripts/install.py --enable-personal"
SUPERGROUP_ID: Final = re.compile(r"-100\d{5,}")
_ENV_VALUE_FORBIDDEN: Final = re.compile(r"[\s\"'#]")
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1


def launch_agents_dir(paths: Paths) -> Path:
    return paths.home / "Library" / "LaunchAgents"


def label(process: ProcessName) -> str:
    return f"{LAUNCHD_PREFIX}.{process}"


def plist_path(paths: Paths, process: ProcessName) -> Path:
    return launch_agents_dir(paths) / f"{label(process)}.plist"


# --- report lines ----------------------------------------------------------------------------------------------------
class Level(StrEnum):
    OK = "ok"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Line:
    level: Level
    text: str
    hint: str = ""


def ok(text: str) -> Line:
    return Line(Level.OK, text)


def fail(text: str, hint: str = "") -> Line:
    return Line(Level.FAIL, text, hint)


def info(text: str, hint: str = "") -> Line:
    return Line(Level.INFO, text, hint)


@dataclass(slots=True)
class Report:
    sections: list[tuple[str, list[Line]]] = field(default_factory=list)

    def add(self, title: str, lines: list[Line]) -> None:
        self.sections.append((title, lines))

    @property
    def lines(self) -> list[Line]:
        return [line for _, lines in self.sections for line in lines]

    @property
    def failures(self) -> int:
        return sum(1 for line in self.lines if line.level is Level.FAIL)


_TAGS: Final = {Level.OK: "OK", Level.FAIL: "XATO", Level.INFO: "ESLATMA"}
_COLOURS: Final = {Level.OK: "\033[32m", Level.FAIL: "\033[31m", Level.INFO: "\033[33m"}
_RESET: Final = "\033[0m"


def render(report: Report, *, colour: bool) -> str:
    out: list[str] = ["Yordamchi tekshiruvi"]
    for title, lines in report.sections:
        out.append("")
        out.append(f"== {title} ==")
        for line in lines:
            tag = f"[{_TAGS[line.level]}]".ljust(10)
            if colour:
                tag = f"{_COLOURS[line.level]}{tag}{_RESET}"
            out.append(f"  {tag}{line.text}")
            if line.hint:
                out.append(f"            -> {line.hint}")
    passed = sum(1 for line in report.lines if line.level is Level.OK)
    out.append("")
    if report.failures:
        out.append(f"Natija: {report.failures} ta xato, {passed} ta OK. Xatolarni tuzatib, qayta tekshiring.")
    else:
        out.append(f"Natija: hammasi joyida ({passed} ta OK).")
    # Defence in depth: every known token is masked even if a line ever carried one by mistake.
    return redact.scrubbed("\n".join(out) + "\n")


# --- env files: read, and edit without overwriting ------------------------------------------------------------------
def _line_key(raw: str) -> tuple[str, str] | None:
    """(key, value) of a `KEY=VALUE` line in `core.config.parse_env_file`'s grammar, else None."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    line = line.removeprefix("export ").strip()
    key, sep, value = line.partition("=")
    if not sep:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key.strip(), value


def update_env_text(
    text: str, updates: Mapping[str, str], *, replace: Collection[str] = ()
) -> tuple[str, tuple[str, ...]]:
    """Set each key of `updates` whose current value is empty or absent; a key in `replace` is set even when it has a
    value. Every other line, comment and key stays byte for byte. Returns the new text and the keys that changed."""
    for key, value in updates.items():
        if _ENV_VALUE_FORBIDDEN.search(value):
            msg = f"refusing to write {key}: the value has whitespace, a quote or '#'"
            raise ValueError(msg)
    lines = text.splitlines()
    changed: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(lines):
        parsed = _line_key(raw)
        if parsed is None or parsed[0] not in updates:
            continue
        key, current = parsed
        seen.add(key)
        new = updates[key]
        if (current and key not in replace) or current == new:
            continue
        lines[index] = f"{key}={new}"
        changed.append(key)
    for key, value in updates.items():
        if key not in seen and value:
            lines.append(f"{key}={value}")
            changed.append(key)
    return ("\n".join(lines) + "\n" if lines else ""), tuple(changed)


def write_env_file(path: Path, text: str) -> None:
    """Atomic replace; the file is 0600 from its first byte (a temp file in the same 0700 dir, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)  # still there only when something failed before the rename
    path.chmod(FILE_MODE)


def read_env_values(path: Path) -> dict[str, str]:
    """Parsed values; every token-like value joins the redaction set before anything can print it."""
    values = config.read_env_file(path)
    redact.add_known_secrets(value for key, value in values.items() if key.endswith("_TOKEN") and value)
    return values


# --- Telegram Bot API (stdlib urllib) --------------------------------------------------------------------------------
type Params = Mapping[str, str | int]


class TelegramError(Exception):
    """A Bot API call failed. `problem` is a short code; nothing here ever holds the URL, the token or a response."""

    def __init__(self, problem: str, status: int | None = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.status = status


class BotApi(Protocol):
    def call(self, method: str, params: Params | None = None) -> object: ...


type BotApiFactory = Callable[[str], BotApi]


_PROBLEMS: Final = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    429: "rate_limited",
}


def _problem(status: int | None) -> str:
    return _PROBLEMS.get(status or 0, "refused")


class UrllibBotApi:
    """One bot's Bot API over `urllib`. The opener is injectable, so the tests answer from a fake handler."""

    def __init__(
        self, token: str, *, opener: urllib.request.OpenerDirector | None = None, timeout: float = TELEGRAM_TIMEOUT_S
    ) -> None:
        redact.add_known_secrets([token])
        self._token = token
        self._opener = opener or urllib.request.build_opener()
        self._timeout = timeout

    def __repr__(self) -> str:
        return "UrllibBotApi(token=••••)"

    def call(self, method: str, params: Params | None = None) -> object:
        body = urllib.parse.urlencode({key: str(value) for key, value in (params or {}).items()}).encode()
        request = urllib.request.Request(  # noqa: S310  # always API_BASE (https), never from input
            f"{API_BASE}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        status: int | None = None
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = b""
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            # `from None`: the context of a urllib error can carry the request, and with it the token.
            raise TelegramError("network") from None
        if status is not None:
            raise TelegramError(_problem(status), status)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise TelegramError("bad_response") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
            code = payload.get("error_code") if isinstance(payload, dict) else None
            raise TelegramError(
                _problem(code if isinstance(code, int) else None), code if isinstance(code, int) else None
            )
        result: object = payload["result"]
        return result


async def _tg(api: BotApi, method: str, params: Params | None = None) -> dict[str, object] | list[object]:
    result = await asyncio.to_thread(api.call, method, params)
    if isinstance(result, dict):
        return {str(key): value for key, value in result.items()}
    if isinstance(result, list):
        return list(result)
    raise TelegramError("bad_response")


def _as_dict(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


# --- subprocesses --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int | None  # None: it could not start, or it timed out
    stdout: str = ""


type Runner = Callable[[Sequence[str], float], Awaitable[RunResult]]


def process_runner(environ: Mapping[str, str], search_path: str) -> Runner:
    """Children run through `core.proc` with the environment a bot gives its own children."""
    env = proc.clean_env(path=search_path, base=environ)

    async def run(argv: Sequence[str], timeout: float) -> RunResult:
        try:
            result = await proc.run(argv, env=env, timeout=timeout)
        except (TimeoutError, OSError):
            return RunResult(None)
        return RunResult(result.returncode, result.stdout.decode("utf-8", errors="replace"))

    return run


# --- context -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Context:
    paths: Paths
    environ: Mapping[str, str]
    uid: int
    now: float  # epoch seconds, for heartbeat ages
    run: Runner
    bot_api: BotApiFactory
    search_path: str
    telegram: bool = True


def _home_relative(paths: Paths, path: Path) -> str:
    try:
        return "~/" + path.relative_to(paths.home).as_posix()
    except ValueError:
        return str(path)


# --- files and modes -----------------------------------------------------------------------------------------------
def _mode_line(paths: Paths, path: Path, expected: int, *, missing: Line | None = None, is_dir: bool) -> Line:
    shown = _home_relative(paths, path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return missing or fail(f"{shown}: topilmadi", "python scripts/install.py ni ishga tushiring")
    if stat.S_ISLNK(st.st_mode) or stat.S_ISDIR(st.st_mode) != is_dir:
        return fail(f"{shown}: {'papka' if is_dir else 'fayl'} emas")
    mode = stat.S_IMODE(st.st_mode)
    if mode != expected:
        return fail(f"{shown}: ruxsat {mode:04o}, kerak {expected:04o}", f'chmod {expected:o} "{path}"')
    return ok(f"{shown}: ruxsat {expected:04o}")


def check_files(paths: Paths) -> list[Line]:
    lines = [
        _mode_line(paths, paths.data, DIR_MODE, is_dir=True),
        _mode_line(paths, paths.db_dir, DIR_MODE, is_dir=True),
        _mode_line(paths, paths.run, DIR_MODE, is_dir=True),
        _mode_line(paths, paths.env_file(BotName.DEV), FILE_MODE, is_dir=False),
        _mode_line(
            paths,
            paths.env_file(BotName.PERSONAL),
            FILE_MODE,
            is_dir=False,
            missing=info("personal.env: hali yo'q (P3 gacha shart emas)"),
        ),
    ]
    db_files = [paths.db_file(bot) for bot in BotName]
    candidates = [db.with_name(db.name + suffix) for db in db_files for suffix in ("", "-wal", "-shm")]
    lines.extend(_mode_line(paths, path, FILE_MODE, is_dir=False) for path in candidates if path.exists())
    lines.append(_mode_line(paths, paths.config, DIR_MODE, is_dir=True))
    return lines


# --- secrets: env files hold references only, the values live in the Keychain ----------------------------------------
async def _keychain_value(run: Runner, item: keychain.KeychainItem, *, reveal: bool) -> tuple[str | None, Line | None]:
    """(the value when `reveal`, else "" when it exists; None and a red line when it cannot be had). Lines start with
    the item name: after "Keychain:" `core.redact`'s generic rule would take the name for a secret and mask it."""
    result = await run(keychain.find_argv(item, reveal=reveal), KEYCHAIN_TIMEOUT_S)
    if result.returncode == keychain.ITEM_NOT_FOUND_EXIT:
        return None, fail(f"{item}: Keychain'da topilmadi", ROTATE_HINT)
    value = result.stdout.removesuffix("\n") if reveal else ""
    if result.returncode != 0 or (reveal and not value):
        return None, fail(f"{item}: Keychain'dan o'qilmadi (qulflanganmi?)", "Mac'ga login qiling, qayta tekshiring")
    if value:
        redact.add_known_secrets([value])
    return value, None


def _reference_hint(item: keychain.KeychainItem) -> str:
    # The reference is never printed whole: `core.redact` would read "keychain:<long value>" as a secret and mask it.
    return f"havola shunday bo'lsin: @keychain:<service>/<account>, yozuv {item}"


def _reference(bot: BotName, key: str, value: str) -> tuple[keychain.KeychainItem | None, Line | None]:
    """The item a token key names, if it is a well-formed reference to one of this bot's items."""
    try:
        item = keychain.parse_reference(value)
    except ValueError:
        return None, fail(f"{bot}.env: {key} havolasi noto'g'ri", _reference_hint(keychain.TOKEN_ITEMS[key]))
    if item not in keychain.ALLOWED[bot]:
        return None, fail(f"{bot}.env: {key} boshqa botning yozuvini ko'rsatadi ({item})", "faqat o'z yozuvi (UC-36)")
    expected = keychain.TOKEN_ITEMS[key]
    if item != expected:  # e.g. the master key: it must never be read as a token, let alone sent to Telegram
        return None, fail(f"{bot}.env: {key} boshqa yozuvni ko'rsatadi ({item})", _reference_hint(expected))
    return item, None


async def _env_secret_lines(ctx: Context, bot: BotName, values: Mapping[str, str]) -> list[Line]:
    name = f"{bot}.env"
    plaintext = config.plaintext_secrets(values)
    lines = [fail(f"{name}: {key} ochiq holda yozilgan (Keychain havolasi emas)", ROTATE_HINT) for key in plaintext]
    if not plaintext:
        lines.append(ok(f"{name}: ochiq maxfiy qiymat yo'q (faqat Keychain havolalari)"))
    for key in keychain.TOKEN_ITEMS:
        value = values.get(key, "")
        if not keychain.is_reference(value):
            continue
        item, problem = _reference(bot, key, value)
        if item is None:
            lines.append(problem or fail(f"{name}: {key}"))
            continue
        _, problem = await _keychain_value(ctx.run, item, reveal=False)
        lines.append(problem or ok(f"{item}: Keychain'da bor ({name} {key})"))
    return lines


async def check_secrets(ctx: Context) -> list[Line]:
    lines: list[Line] = []
    for bot in BotName:
        path = ctx.paths.env_file(bot)
        if not path.exists():
            continue
        try:
            values = read_env_values(path)
        except ConfigInvalid:
            continue  # the Telegram section reports an unreadable file
        lines.extend(await _env_secret_lines(ctx, bot, values))
    _, problem = await _keychain_value(ctx.run, keychain.MASTER_KEY, reveal=False)
    if problem is not None:
        lines.append(fail(f"Ombor kaliti: Keychain'da {keychain.MASTER_KEY} yo'q yoki o'qilmadi", INSTALL_HINT))
    else:
        lines.append(ok(f"Ombor kaliti: Keychain'da ({keychain.MASTER_KEY})"))
    if ctx.paths.master_key.exists() or ctx.paths.master_key.is_symlink():
        lines.append(
            fail(
                f"{_home_relative(ctx.paths, ctx.paths.master_key)}: kalit hali fayl sifatida turibdi",
                "python scripts/install.py: kalit Keychain'ga ko'chiriladi, tekshiriladi va fayl o'chiriladi",
            )
        )
    return lines


async def _token(run: Runner, bot: BotName, values: Mapping[str, str], key: str) -> tuple[str | None, Line | None]:
    """The token to call the Bot API with: only ever from the Keychain. (None, None) when another line covers it."""
    value = values.get(key, "")
    if not value or not keychain.is_reference(value):
        return None, None  # empty: reported by the caller; plaintext: red in the secrets section, never used
    item, problem = _reference(bot, key, value)
    if item is None:
        return None, problem
    return await _keychain_value(run, item, reveal=True)


# --- claude --------------------------------------------------------------------------------------------------------
async def check_claude(ctx: Context) -> list[Line]:
    claude = ctx.paths.local_bin / "claude"
    if not os.access(claude, os.X_OK):
        return [fail("claude: ~/.local/bin/claude topilmadi", "Claude Code ni o'rnating (claude.ai/install)")]
    lines = [ok("claude: ~/.local/bin/claude bor")]
    result = await ctx.run([str(claude), "auth", "status", "--json"], CLAUDE_TIMEOUT_S)
    try:
        status = json.loads(result.stdout) if result.returncode is not None else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        lines.append(fail("claude auth status: javob o'qilmadi", "terminalda: claude auth status"))
        return lines
    # Only these two fields are read: the rest of the status (email, organisation) is never printed.
    method = status.get("authMethod")
    if status.get("loggedIn") is False:
        lines.append(fail("claude auth status: tizimga kirilmagan", "terminalda: claude, keyin /login"))
    elif method == EXPECTED_AUTH_METHOD:
        lines.append(ok('claude auth status: authMethod "claude.ai" (obuna)'))
    else:
        shown = method if isinstance(method, str) and re.fullmatch(r"[\w.-]{1,32}", method) else "?"
        lines.append(
            fail(
                f'claude auth status: authMethod "{shown}", kerak "claude.ai"',
                "API kalit yoki token emas, Claude obunasi bilan kiring (ClaudeManager)",
            )
        )
    return lines


def check_credentials(ctx: Context) -> list[Line]:
    found = proc.check_credentials([ctx.paths.claude_user_settings], environ=ctx.environ)
    if not found:
        return [ok("Taqiqlangan kirish manbalari yo'q (env va ~/.claude/settings.json)")]
    return [
        fail(f"Taqiqlangan kirish manbai: {name}", "uni olib tashlang: botlar bu bilan ishga tushmaydi (SPEC §2.5)")
        for name in found
    ]


def check_binaries(ctx: Context) -> list[Line]:
    report = proc.find_binaries(ctx.search_path, required=CHECKED_BINARIES, optional=())
    lines: list[Line] = []
    for name in CHECKED_BINARIES:
        where = report.found.get(name)
        if where is None:
            hint = "python scripts/install.py (ffmpeg DATA/bin ga tushadi)" if name == "ffmpeg" else ""
            lines.append(fail(f"{name}: topilmadi", hint))
        else:
            lines.append(ok(f"{name}: {_home_relative(ctx.paths, Path(where))}"))
    return lines


# --- launchd and heartbeats ----------------------------------------------------------------------------------------
async def _loaded(ctx: Context, process: ProcessName) -> bool | None:
    result = await ctx.run([LAUNCHCTL, "print", f"gui/{ctx.uid}/{label(process)}"], LAUNCHCTL_TIMEOUT_S)
    return None if result.returncode is None else result.returncode == 0


def personal_enabled(paths: Paths) -> bool:
    """The installer put the personal bot's plist in `~/Library/LaunchAgents` (`--enable-personal`): from then on it
    must be loaded and its heartbeat fresh, like the dev bot's."""
    return plist_path(paths, "personal").exists()


async def check_launchd(ctx: Context) -> list[Line]:
    lines: list[Line] = []
    for process in ("dev", "supervisor", "personal"):
        plist = plist_path(ctx.paths, process)
        loaded = await _loaded(ctx, process)
        name = label(process)
        if process == "personal" and not loaded and not personal_enabled(ctx.paths):
            lines.append(info(f"{name}: yoqilmagan", ENABLE_PERSONAL_HINT))
        elif loaded is None:
            lines.append(fail(f"{name}: launchctl javob bermadi"))
        elif loaded:
            lines.append(ok(f"{name}: launchd'da yuklangan"))
        elif not plist.exists():
            lines.append(fail(f"{name}: plist yo'q ({_home_relative(ctx.paths, plist)})", "python scripts/install.py"))
        else:
            lines.append(fail(f"{name}: yuklanmagan", f'launchctl bootstrap gui/{ctx.uid} "{plist}"'))
    return lines


def _heartbeat_line(ctx: Context, process: ProcessName) -> Line:
    path = ctx.paths.run / f"{process}.heartbeat.json"
    max_age = SUPERVISOR_HEARTBEAT_MAX_AGE_S if process == "supervisor" else BOT_HEARTBEAT_MAX_AGE_S
    if process == "personal" and not personal_enabled(ctx.paths):
        return info("personal bot heartbeat: bot yoqilmagan", ENABLE_PERSONAL_HINT)
    try:
        age = ctx.now - path.stat().st_mtime
    except FileNotFoundError:
        if process == "supervisor":
            return fail("supervisor heartbeat: fayl yo'q", f"launchctl kickstart -k gui/{ctx.uid}/{label(process)}")
        return info(f"{process} bot heartbeat: hali yo'q (bot ishga tushmagan)")
    except OSError:
        return fail(f"{process} heartbeat: o'qib bo'lmadi")
    seconds = max(0, int(age))
    if age > max_age:
        return fail(
            f"{process} heartbeat: eskirgan ({seconds} s oldin, chegara {int(max_age)} s)",
            f"log: ~/Library/Logs/Yordamchi/{label(process)}.launchd.log",
        )
    return ok(f"{process} heartbeat: yangi ({seconds} s oldin)")


def check_heartbeats(ctx: Context) -> list[Line]:
    return [_heartbeat_line(ctx, process) for process in ("supervisor", "dev", "personal")]


# --- bootstrap values and Telegram ---------------------------------------------------------------------------------
def _read(path: Path) -> tuple[dict[str, str] | None, Line | None]:
    if not path.exists():
        return None, fail(f"{path.name}: topilmadi", "python scripts/install.py ni ishga tushiring")
    try:
        return read_env_values(path), None
    except ConfigInvalid as exc:
        return None, fail(f"{path.name}: o'qilmadi ({exc.params.get('key')}: {exc.params.get('problem')})")


def check_ids(values: Mapping[str, str]) -> list[Line]:
    lines: list[Line] = []
    owner = values.get("OWNER_ID", "")
    if not owner:
        lines.append(fail("OWNER_ID: bo'sh", "python -m yordamchi.check --discover-ids"))
    elif not owner.isdigit() or int(owner) <= 0:
        lines.append(fail("OWNER_ID: foydalanuvchi id emas (musbat son bo'lishi kerak)"))
    else:
        lines.append(ok(f"OWNER_ID: {owner}"))
    group = values.get("DEV_GROUP_ID", "")
    if not group:
        lines.append(fail("DEV_GROUP_ID: bo'sh", "python -m yordamchi.check --discover-ids"))
    elif not SUPERGROUP_ID.fullmatch(group):
        lines.append(
            fail(
                f"DEV_GROUP_ID: {group} supergroup id emas (-100… bilan boshlanishi kerak)",
                "oddiy guruh supergroupga aylanganda id o'zgaradi va bot uni begona deb tark etadi; "
                "guruhni supergroupga aylantirib, yangi id ni yozing",
            )
        )
    else:
        lines.append(ok(f"DEV_GROUP_ID: {group} (supergroup)"))
    return lines


def _tg_fail(what: str, exc: TelegramError) -> Line:
    hints = {
        "unauthorized": "token yaroqsiz: BotFather dan yangisini oling (DEPLOY.md, token almashtirish)",
        "network": "internet aloqasini tekshiring",
        "conflict": "bot hozir ishlayapti yoki webhook o'rnatilgan",
        "bad_request": "id noto'g'ri yoki bot bu chatda emas",
        "not_found": "id noto'g'ri yoki bot bu chatda emas",
    }
    return fail(f"{what}: Telegram xatosi ({exc.problem})", hints.get(exc.problem, ""))


async def _bot_identity(api: BotApi, who: str, *, privacy_off_required: bool) -> tuple[list[Line], int | None]:
    try:
        me = _as_dict(await _tg(api, "getMe"))
    except TelegramError as exc:
        return [_tg_fail(f"{who}: getMe", exc)], None
    bot_id = me.get("id")
    lines = [ok(f"{who}: getMe javob berdi (@{me.get('username', '?')})")]
    if privacy_off_required:
        if me.get("can_read_all_group_messages") is True:
            lines.append(ok(f"{who}: privacy o'chirilgan (guruhdagi hamma xabarni o'qiydi)"))
        else:
            lines.append(
                fail(
                    f"{who}: privacy yoqilgan (can_read_all_group_messages = false)",
                    "BotFather → /setprivacy → Disable; keyin botni guruhdan chiqarib, qayta qo'shing",
                )
            )
    if me.get("can_join_groups") is False:
        lines.append(ok(f"{who}: guruhlarga qo'shish o'chirilgan"))
    else:
        lines.append(
            fail(
                f"{who}: guruhlarga qo'shish yoqilgan (can_join_groups = true)",
                "guruhga qo'shib bo'lgach: BotFather → /setjoingroups → Disable",
            )
        )
    try:
        commands = await _tg(api, "getMyCommands")
    except TelegramError as exc:
        lines.append(_tg_fail(f"{who}: getMyCommands", exc))
    else:
        if commands:
            lines.append(fail(f"{who}: buyruqlar menyusi bor", "BotFather → /deletecommands (menyu bo'lmasligi kerak)"))
        else:
            lines.append(ok(f"{who}: buyruqlar menyusi yo'q"))
    return lines, bot_id if isinstance(bot_id, int) else None


ANONYMOUS_ADMIN_REMINDER: Final = (
    "Guruhga hech qachon anonim admin sifatida yozmang: bunday xabarlar GroupAnonymousBot dan keladi "
    "va bot ularni tashlab yuboradi"
)


async def _bot_rights(api: BotApi, group: str, bot_id: int, *, forum: bool) -> list[Line]:
    try:
        member = _as_dict(await _tg(api, "getChatMember", {"chat_id": group, "user_id": bot_id}))
    except TelegramError as exc:
        return [_tg_fail("dev guruh: bot a'zoligi", exc)]
    rights = member.get("can_delete_messages") is True and member.get("can_pin_messages") is True
    if member.get("status") == "administrator" and rights:
        lines = [ok("dev guruh: bot admin, xabar o'chira va qadab qo'ya oladi")]
    else:
        lines = [
            fail(
                "dev guruh: bot admin emas yoki o'chirish/qadash huquqi yo'q",
                "guruh → Administratorlar → botni qo'shing: Delete messages va Pin messages",
            )
        ]
    if forum:
        lines.append(_topics_right(member))
    return lines


TOPICS_HINT: Final = "guruh → Tahrirlash (Edit) → Topics (Mavzular) ni yoqing"
MANAGE_TOPICS_HINT: Final = "guruh → Administratorlar → bot → Manage topics (Mavzularni boshqarish) ni yoqing"


def _forum_line(chat: Mapping[str, object]) -> Line:
    """Topics on: General plus one topic per project. Off is the flat group of before: information, not an error."""
    if chat.get("is_forum") is True:
        return ok("dev guruh: Topics yoqilgan (is_forum): har loyiha o'z mavzusida ishlaydi")
    return info("dev guruh: Topics o'chiq (is_forum = false): bot oddiy guruh kabi ishlaydi", TOPICS_HINT)


def _topics_right(member: Mapping[str, object]) -> Line:
    """In a forum the bot opens each project's topic itself; it needs the "Manage topics" admin right for that."""
    if member.get("can_manage_topics") is True:
        return ok("dev guruh: bot mavzu ocha oladi (can_manage_topics)")
    return fail(
        "dev guruh: botda Manage topics huquqi yo'q (can_manage_topics = false): loyiha mavzulari ochilmaydi",
        MANAGE_TOPICS_HINT,
    )


async def _owner_membership(api: BotApi, group: str, owner_id: int) -> Line:
    try:
        member = _as_dict(await _tg(api, "getChatMember", {"chat_id": group, "user_id": owner_id}))
    except TelegramError as exc:
        return _tg_fail("dev guruh: sizning a'zoligingiz", exc)
    status = member.get("status")
    if status not in {"creator", "administrator", "member"}:
        return fail(f"dev guruh: siz guruh a'zosi emassiz ({status!s})")
    if member.get("is_anonymous") is True:
        return fail(
            "dev guruh: siz anonim admin sifatida yozasiz, xabarlaringiz tashlab yuboriladi",
            "guruh → Administratorlar → o'zingiz → \"Anonim qolish\" ni o'chiring",
        )
    return ok("dev guruh: siz a'zosiz va anonim emassiz")


async def _dev_group(api: BotApi, values: Mapping[str, str], bot_id: int) -> list[Line]:
    group = values.get("DEV_GROUP_ID", "")
    if not SUPERGROUP_ID.fullmatch(group):
        return []  # check_ids already reported it
    try:
        chat = _as_dict(await _tg(api, "getChat", {"chat_id": group}))
    except TelegramError as exc:
        return [_tg_fail("dev guruh: getChat", exc)]
    if chat.get("type") == "supergroup":
        lines = [ok(f'dev guruh: supergroup "{chat.get("title", "")}"')]
    else:
        lines = [fail(f"dev guruh: turi {chat.get('type')!s}, kerak supergroup")]
    lines.append(_forum_line(chat))
    lines.extend(await _bot_rights(api, group, bot_id, forum=chat.get("is_forum") is True))
    owner = values.get("OWNER_ID", "")
    if owner.isdigit():
        lines.append(await _owner_membership(api, group, int(owner)))
    lines.append(info(ANONYMOUS_ADMIN_REMINDER))
    return lines


def _no_token(who: str) -> list[Line]:
    # The red line for the token itself is in the secrets section; this one only says what was skipped.
    return [info(f"{who}: Telegram tekshiruvi o'tkazib yuborildi (token Keychain'dan olinmadi)")]


async def _dev_telegram(ctx: Context, values: Mapping[str, str]) -> list[Line]:
    token, _ = await _token(ctx.run, BotName.DEV, values, "DEV_BOT_TOKEN")
    lines = [] if token else _no_token("dev bot")
    if token:
        api = ctx.bot_api(token)
        identity, bot_id = await _bot_identity(api, "dev bot", privacy_off_required=True)
        lines.extend(identity)
        if bot_id is not None:
            lines.extend(await _dev_group(api, values, bot_id))
    return lines


async def _personal_telegram(ctx: Context, values: Mapping[str, str]) -> list[Line]:
    if not values.get("PERSONAL_BOT_TOKEN", ""):
        return [info("PERSONAL_BOT_TOKEN: bo'sh (P3 gacha shart emas)")]
    if not ctx.telegram:
        return []
    token, _ = await _token(ctx.run, BotName.PERSONAL, values, "PERSONAL_BOT_TOKEN")
    lines = [] if token else _no_token("shaxsiy bot")
    if token:
        identity, _ = await _bot_identity(ctx.bot_api(token), "shaxsiy bot", privacy_off_required=False)
        lines.extend(identity)
    return lines


async def check_telegram(ctx: Context) -> list[Line]:
    lines: list[Line] = []
    dev_values, problem = _read(ctx.paths.env_file(BotName.DEV))
    if problem is not None or dev_values is None:
        return [problem or fail("dev.env: o'qilmadi")]
    lines.extend(check_ids(dev_values))
    if not dev_values.get("DEV_BOT_TOKEN", ""):
        lines.append(fail("DEV_BOT_TOKEN: bo'sh", "python scripts/install.py (token yashirin kiritiladi)"))
    elif ctx.telegram:
        lines.extend(await _dev_telegram(ctx, dev_values))
    personal_path = ctx.paths.env_file(BotName.PERSONAL)
    if personal_path.exists():
        personal_values, problem = _read(personal_path)
        if problem is not None or personal_values is None:
            lines.append(problem or fail("personal.env: o'qilmadi"))
        else:
            lines.extend(await _personal_telegram(ctx, personal_values))
    if not ctx.telegram:
        lines.append(info("Telegram tekshiruvlari o'tkazib yuborildi (--offline)"))
    return lines


# --- power, disk, FileVault ----------------------------------------------------------------------------------------
def ac_sleep_minutes(pmset_custom: str) -> int | None:
    """`sleep` under `AC Power:` in `pmset -g custom` (minutes; 0 = never)."""
    in_ac = False
    for raw in pmset_custom.splitlines():
        if raw and not raw[0].isspace():
            in_ac = raw.strip().rstrip(":") == "AC Power"
            continue
        parts = raw.split()
        if in_ac and len(parts) == 2 and parts[0] == "sleep" and parts[1].isdigit():
            return int(parts[1])
    return None


async def check_system(ctx: Context) -> list[Line]:
    lines: list[Line] = []
    pmset = await ctx.run([PMSET, "-g", "custom"], SYSTEM_TIMEOUT_S)
    minutes = ac_sleep_minutes(pmset.stdout) if pmset.returncode == 0 else None
    if minutes is None:
        lines.append(info("pmset: AC uyqu sozlamasi o'qilmadi"))
    elif minutes == 0:
        lines.append(ok("Quvvat: zaryadda Mac uxlamaydi (pmset -c sleep 0)"))
    else:
        lines.append(fail(f"Quvvat: zaryadda Mac {minutes} daqiqada uxlaydi", "sudo pmset -c sleep 0"))
    target = ctx.paths.data if ctx.paths.data.exists() else ctx.paths.home
    try:
        free_gb = shutil.disk_usage(target).free / 1024**3
    except OSError:
        lines.append(info("Disk: bo'sh joyni o'qib bo'lmadi"))
    else:
        lines.append(info(f"Disk: {free_gb:.1f} GB bo'sh"))
    vault = await ctx.run([FDESETUP, "status"], SYSTEM_TIMEOUT_S)
    if vault.returncode == 0 and "FileVault is On" in vault.stdout:
        lines.append(info("FileVault yoqilgan: qayta yuklangandan keyin botlar siz login qilguncha ishlamaydi"))
    elif vault.returncode == 0:
        lines.append(info("FileVault o'chirilgan"))
    return lines


OWNER_CHECKLIST: Final = (
    "Telegram 2FA (ikki bosqichli parol) yoqilganini tekshiring",
    "Begona sinovlari uchun ikkinchi Telegram akkaunt tayyor bo'lsin",
    "Bot tokenlarini chatga hech qachon yozmang: faqat scripts/install.py orqali (yashirin kiritiladi)",
)


async def run_checks(ctx: Context) -> Report:
    report = Report()
    report.add("Fayllar va ruxsatlar", check_files(ctx.paths))
    report.add("Maxfiy qiymatlar (Keychain)", await check_secrets(ctx))
    report.add("Claude", [*await check_claude(ctx), *check_credentials(ctx)])
    report.add("Dasturlar", check_binaries(ctx))
    report.add("launchd", await check_launchd(ctx))
    report.add("Heartbeat", check_heartbeats(ctx))
    report.add("Telegram", await check_telegram(ctx))
    report.add("Tizim", await check_system(ctx))
    report.add("Egasi uchun eslatma", [info(text) for text in OWNER_CHECKLIST])
    return report


# --- --discover-ids ------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Candidates:
    owners: dict[int, str]  # user id → display name, from private chats with the bot
    groups: dict[int, tuple[str, str]]  # chat id → (type, title)
    migrated: dict[int, int]  # basic group id → the supergroup id it became


def find_candidates(updates: Sequence[object]) -> Candidates:
    owners: dict[int, str] = {}
    groups: dict[int, tuple[str, str]] = {}
    migrated: dict[int, int] = {}
    for update in updates:
        body = _as_dict(update)
        for key in ("message", "edited_message", "my_chat_member", "chat_member"):
            item = _as_dict(body.get(key))
            if not item:
                continue
            chat = _as_dict(item.get("chat"))
            sender = _as_dict(item.get("from"))
            chat_id, chat_type = chat.get("id"), chat.get("type")
            if not isinstance(chat_id, int) or not isinstance(chat_type, str):
                continue
            sender_id = sender.get("id")
            if chat_type == "private" and isinstance(sender_id, int) and sender.get("is_bot") is not True:
                owners[sender_id] = str(sender.get("username") or sender.get("first_name") or "")
            elif chat_type in {"group", "supergroup"}:
                groups[chat_id] = (chat_type, str(chat.get("title", "")))
            target = item.get("migrate_to_chat_id")
            if isinstance(target, int):
                migrated[chat_id] = target
    return Candidates(owners, groups, migrated)


type Ask = Callable[[str], str]


def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"ha", "h", "yes", "y"}


def _choose(ask: Ask, write: Callable[[str], None], key: str, options: Sequence[int]) -> str | None:
    if len(options) == 1:
        return str(options[0]) if _yes(ask(f"{key}={options[0]} deb yozaymi? (ha/yo'q): ")) else None
    answer = ask(f"{key} uchun yuqoridagi id lardan birini kiriting (bo'sh qoldirsangiz yozilmaydi): ").strip()
    if answer and answer in {str(option) for option in options}:
        return answer
    if answer:
        write("Bu id ro'yxatda yo'q, yozilmadi.\n")
    return None


async def _discovery_token(paths: Paths, run: Runner) -> tuple[dict[str, str] | None, str | None, str]:
    """(dev.env values, the dev token from the Keychain, why not) for `--discover-ids`."""
    values, problem = _read(paths.env_file(BotName.DEV))
    if problem is not None or values is None:
        return None, None, problem.text if problem else "dev.env: topilmadi"
    if not values.get("DEV_BOT_TOKEN", ""):
        return values, None, "DEV_BOT_TOKEN bo'sh: avval python scripts/install.py"
    token, problem = await _token(run, BotName.DEV, values, "DEV_BOT_TOKEN")
    if token is None:
        if problem is None:
            return values, None, f"DEV_BOT_TOKEN Keychain havolasi emas -> {ROTATE_HINT}"
        return values, None, f"{problem.text} -> {problem.hint}"
    return values, token, ""


async def discover_ids(paths: Paths, bot_api: BotApiFactory, *, run: Runner, ask: Ask, out: TextIO) -> int:
    def say(text: str) -> None:
        out.write(redact.scrubbed(text))

    values, token, problem_text = await _discovery_token(paths, run)
    if values is None or token is None:
        say(f"[XATO] {problem_text}\n")
        return EXIT_FAILED
    try:
        # No offset: nothing is confirmed, so the bot still receives these updates once it runs.
        updates = await _tg(bot_api(token), "getUpdates", {"timeout": 0, "limit": 100})
    except TelegramError as exc:
        line = _tg_fail("getUpdates", exc)
        hint = " (launchctl bootout bilan botni to'xtatib, qayta urinib ko'ring)" if exc.problem == "conflict" else ""
        say(f"[XATO] {line.text}{hint}\n")
        return EXIT_FAILED
    found = find_candidates(updates if isinstance(updates, list) else [])
    if not found.owners and not found.groups:
        say(
            "Hech narsa topilmadi. Botga shaxsiy xabar yozing va dev guruhda botni eslab (@bot) xabar yuboring, "
            "keyin qayta ishga tushiring.\n"
        )
        return EXIT_OK
    _print_candidates(found, say)
    chosen = _choose_ids(found, values, ask=ask, say=say)
    if not chosen:
        say("dev.env o'zgartirilmadi (bor qiymatlar hech qachon ustidan yozilmaydi).\n")
        return EXIT_OK
    _write_ids(paths, chosen, say)
    return EXIT_OK


def _print_candidates(found: Candidates, say: Callable[[str], None]) -> None:
    say("Topilgan egalari (shaxsiy chat):\n")
    for user_id, name in sorted(found.owners.items()):
        say(f"  {user_id}  {name}\n")
    say("Topilgan guruhlar:\n")
    for chat_id, (chat_type, title) in sorted(found.groups.items()):
        note = "" if SUPERGROUP_ID.fullmatch(str(chat_id)) else "  (oddiy guruh: supergroupga aylantiring)"
        if chat_id in found.migrated:
            note = f"  (supergroupga aylangan: yangi id {found.migrated[chat_id]})"
        say(f"  {chat_id}  {chat_type}  {title}{note}\n")


def _choose_ids(
    found: Candidates, values: Mapping[str, str], *, ask: Ask, say: Callable[[str], None]
) -> dict[str, str]:
    """Only keys that are empty today are offered: an id already in the file is never replaced."""
    supergroups = sorted(
        {cid for cid in found.groups if SUPERGROUP_ID.fullmatch(str(cid))} | set(found.migrated.values())
    )
    chosen: dict[str, str] = {}
    for key, options in (("OWNER_ID", sorted(found.owners)), ("DEV_GROUP_ID", supergroups)):
        if values.get(key) or not options:
            continue
        picked = _choose(ask, say, key, options)
        if picked:
            chosen[key] = picked
    return chosen


def _write_ids(paths: Paths, chosen: Mapping[str, str], say: Callable[[str], None]) -> None:
    dev_path = paths.env_file(BotName.DEV)
    text, changed = update_env_text(dev_path.read_text(encoding="utf-8"), chosen)
    write_env_file(dev_path, text)
    say(f"dev.env ga yozildi: {', '.join(changed)}\n")
    personal_path = paths.env_file(BotName.PERSONAL)
    if "OWNER_ID" in chosen and personal_path.exists():
        text, changed = update_env_text(personal_path.read_text(encoding="utf-8"), {"OWNER_ID": chosen["OWNER_ID"]})
        if changed:
            write_env_file(personal_path, text)
            say("personal.env ga yozildi: OWNER_ID\n")


# --- entry point ---------------------------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yordamchi.check", description="Yordamchi o'rnatilishini tekshirish"
    )
    parser.add_argument("--discover-ids", action="store_true", help="getUpdates orqali OWNER_ID va DEV_GROUP_ID topish")
    parser.add_argument("--offline", action="store_true", help="Telegram tekshiruvlarisiz")
    parser.add_argument("--no-colour", action="store_true", help="rangsiz chiqarish")
    return parser


async def _main(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    paths = config.load_paths(environ)
    login_path = await proc.login_shell_path(env=proc.clean_env(path=":".join(proc.SYSTEM_PATH), base=environ))
    search_path = proc.build_search_path(paths, login_path)

    def factory(token: str) -> BotApi:
        return UrllibBotApi(token)

    run = process_runner(environ, search_path)
    if args.discover_ids:
        return await discover_ids(paths, factory, run=run, ask=input, out=out)
    ctx = Context(
        paths=paths,
        environ=environ,
        uid=os.getuid(),
        now=time.time(),  # noqa: TID251  # a CLI: the heartbeat age is wall-clock against the file's mtime
        run=run,
        bot_api=factory,
        search_path=search_path,
        telegram=not args.offline,
    )
    report = await run_checks(ctx)
    colour = not args.no_colour and out.isatty() and "NO_COLOR" not in environ
    out.write(render(report, colour=colour))
    return EXIT_FAILED if report.failures else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    environ = dict(os.environ)  # noqa: TID251  # judged, never exported: the check reports names only
    return asyncio.run(_main(args, environ, sys.stdout))  # noqa: TID251  # this CLI's one event loop


if __name__ == "__main__":
    raise SystemExit(main())
