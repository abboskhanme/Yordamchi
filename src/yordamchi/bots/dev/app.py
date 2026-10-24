"""`python -m yordamchi.bots.dev.app`: the dev bot's entry point and composition root (SPEC §2.5–§2.7; CONVENTIONS
§4, §6, C4). launchd runs it from `releases/current` with `YORDAMCHI_ENV_FILE` and `YORDAMCHI_DATA_DIR` set.

Startup, in this order (SPEC §2.5):
1. `os.umask(0o077)`; `core.redact` on stderr, stdout and every uncaught-exception path, before aiogram or the Agent
   SDK is imported (their tracebacks carry the bot token in URLs). Vendor modules are imported inside functions.
2. Logging to `~/Library/Logs/Yordamchi/dev.jsonl`.
3. The env file (`core.config`): token, owner id, dev group id. It never enters `os.environ`.
4. **Refusal**: a credential source that outranks the keychain login (an Anthropic key or token, another config
   dir, an `apiKeyHelper`, …; `core.proc.REFUSED_ENV_VARS`) → `run/dev.lasterror`, `S.STARTUP_REFUSED` to the dev
   group at most once per 6 h (marker `run/dev.refused`), exit 78. The other refusals take the same path.
5. `os.environ` scrubbed of `CLAUDECODE` / `CLAUDE_CODE_*` (the SDK merges it into every `claude` child).
6. The child PATH: shims, `~/.local/bin`, `DATA/bin`, the login shell's PATH, the system dirs. No `claude` or `git`
   → refused; no `ffmpeg`, `gh` or `node` → degraded, shown in `holat?`.
7. `dev.db` must be at the Alembic head unless an update is under way; the bot never migrates.
8. `run/flags/probation.json` naming this version → probation (heartbeat and control socket only, P2).
9. Adapters → one `DevDeps` → `runtime.DevApp`; recovery: `turns.recover_turns` → `questions.recover_questions` →
   `health.report_start`.
10. The loops, under `core.aio.forever`, until SIGTERM; then the shutdown of SPEC §2.6 and exit 0.

`--dry-run` runs steps 1–8 and builds every adapter, `DevDeps` and the app, then exits 0 without polling, recovering,
binding the socket or sending anything: proof that a release starts, safe against a temp `YORDAMCHI_DATA_DIR`.
"""

import argparse
import asyncio
import importlib.metadata
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

from yordamchi.core import config as bootstrap
from yordamchi.core import json_value, log, proc, redact
from yordamchi.core import strings as S
from yordamchi.core.config import DevConfig
from yordamchi.core.errors import ConfigInvalid, ExternalError
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText
from yordamchi.ports.clock import Clock

if TYPE_CHECKING:
    from yordamchi.bots.dev.runtime import Closer, DevApp
    from yordamchi.persistence.uow import Database

logger = get_log(__name__)

EXIT_OK: Final = 0
EXIT_REFUSED: Final = proc.EXIT_REFUSED  # 78, EX_CONFIG
NOTICE_EVERY: Final = timedelta(hours=6)  # SPEC §2.5 step 3: the refusal notice at most once per 6 h
NOTICE_TIMEOUT_S: Final = 10.0
RELEASE_MARKER: Final = ".yordamchi-release.json"  # written by scripts/install.py into each release
DEV_VERSION: Final = "dev"  # running from a working copy, not an installed release
SSH_BINARY: Final = "/usr/bin/ssh"
PROBATION_POLL_S: Final = 5.0
ASSISTANT_CLOSE_S: Final = 10.0

type Notify = Callable[[DevConfig, BotText], Awaitable[None]]
# The owner's login-shell PATH (`/bin/zsh -lc`, 5 s), or None when it cannot be read.
type LoginPath = Callable[[Mapping[str, str]], Awaitable[str | None]]


# --- the release this process runs from -----------------------------------------------------------------------------
def release_root() -> Path:
    """`releases/<version>/` (its `.venv` is this interpreter's prefix), or the working copy in development."""
    return Path(sys.prefix).resolve().parent


def release_version(root: Path | None = None) -> str:
    """`YYYY.MM.DD-<sha>` from the installer's release marker; `dev` outside an installed release."""
    marker = (root or release_root()) / RELEASE_MARKER
    try:
        parsed = json_value.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEV_VERSION
    version = parsed.get("version") if isinstance(parsed, dict) else None
    return version if isinstance(version, str) and version else DEV_VERSION


def sdk_version() -> str:
    try:
        return importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        return ""


# --- refusing to start (SPEC §2.5 steps 3–7) ------------------------------------------------------------------------
def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(text)


def refusal_marker(paths: Paths) -> Path:
    return paths.run / f"{BotName.DEV}.refused"


def _notice_due(marker: Path, now: datetime) -> bool:
    try:
        raw = json_value.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    at = raw.get("at") if isinstance(raw, dict) else None
    try:
        last = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        return True
    return last is None or last.tzinfo is None or now - last >= NOTICE_EVERY


async def refuse(
    paths: Paths,
    config: DevConfig | None,
    *,
    reason: str,
    what: str,
    notice: BotText | None,
    clock: Clock,
    notify: Notify,
) -> int:
    """Write `run/dev.lasterror` (one scrubbed line: what the supervisor quotes), tell the owner at most once per
    6 h, and return the exit code. `what` holds names, never values."""
    now = clock.now()
    line = redact.scrubbed(f"{now.isoformat()} startup_refused {reason}: {what}")
    await asyncio.to_thread(_write_private, paths.last_error(BotName.DEV), line + "\n")
    logger.error("startup_refused", reason=reason, what=what)
    marker = refusal_marker(paths)
    if notice is not None and config is not None and await asyncio.to_thread(_notice_due, marker, now):
        try:
            await notify(config, notice)
        except ExternalError as error:
            logger.warning("startup_notice_failed", error_code=error.code.value)
        else:
            await asyncio.to_thread(_write_private, marker, json.dumps({"at": now.isoformat(), "reason": reason}))
    return EXIT_REFUSED


@dataclass(frozen=True, slots=True)
class Boot:
    """What steps 3–8 established; the DBs are open."""

    config: DevConfig
    version: str
    search_path: str  # every child's PATH (shims first)
    ssh_path: str  # the real ssh's PATH: the login shell's and the system's, never the shims
    degraded: tuple[str, ...]
    db: "Database"
    peer_db: "Database | None"
    probation: bool


def _ssh_path(paths: Paths, login: str | None) -> str:
    entries = [*(login or "").split(":"), *proc.SYSTEM_PATH]
    shims = str(paths.shims)
    return ":".join(dict.fromkeys(entry for entry in entries if entry and entry.rstrip("/") != shims))


def update_in_progress(paths: Paths) -> bool:
    """A self-update drains or probes (P2): the schema may be ahead of or behind this release for a while."""
    return paths.drain_flag.exists() or paths.probation_flag.exists()


def probation_names(paths: Paths, version: str) -> bool:
    try:
        raw = json_value.loads(paths.probation_flag.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("version") == version


async def read_login_path(environ: Mapping[str, str]) -> str | None:
    return await proc.login_shell_path(env=proc.clean_env(path=":".join(proc.SYSTEM_PATH), base=environ))


async def prepare(
    environ: MutableMapping[str, str],
    *,
    clock: Clock,
    notify: Notify,
    home: Path | None = None,
    migrations: Path | None = None,
    version: str | None = None,
    login_path: LoginPath = read_login_path,
) -> Boot | int:
    """SPEC §2.5 steps 3–8. A `Boot` to run with, or the exit code of a refusal (already reported)."""
    from yordamchi.persistence.engine import open_database
    from yordamchi.persistence.schema import schema_is_current

    paths = bootstrap.load_paths(environ, home=home)
    version = version or release_version()
    try:
        config = bootstrap.load_dev_config(environ, home=home)
    except ConfigInvalid as error:
        what = f"{error.params.get('key')}: {error.params.get('problem')}"
        return await refuse(paths, None, reason="config", what=what, notice=None, clock=clock, notify=notify)
    found = proc.check_credentials([paths.claude_user_settings], environ=environ)
    if found:
        what = ", ".join(found)
        notice = S.STARTUP_REFUSED(what=what)
        return await refuse(paths, config, reason="credentials", what=what, notice=notice, clock=clock, notify=notify)
    proc.scrub_environ(environ)
    login = await login_path(environ)
    search_path = proc.build_search_path(paths, login)
    binaries = proc.find_binaries(search_path)
    if binaries.fatal:
        what = ", ".join(binaries.missing_required)
        notice = S.ERR_NOT_INSTALLED(binary=what)
        return await refuse(paths, config, reason="binaries", what=what, notice=notice, clock=clock, notify=notify)
    db = open_database(paths.db_file(BotName.DEV))
    current = await schema_is_current(db, migrations or release_root() / "migrations")
    if not current and not update_in_progress(paths):
        await db.dispose()
        notice = S.STARTUP_SCHEMA_MISMATCH()
        return await refuse(paths, config, reason="schema", what="dev.db", notice=notice, clock=clock, notify=notify)
    peer_file = paths.db_file(BotName.PERSONAL)
    peer_db = open_database(peer_file, readonly=True) if peer_file.exists() else None
    return Boot(
        config=config,
        version=version,
        search_path=search_path,
        ssh_path=_ssh_path(paths, login),
        degraded=binaries.missing_optional,
        db=db,
        peer_db=peer_db,
        probation=probation_names(paths, version),
    )


# --- the composition root (C4: the only place adapters are constructed) ---------------------------------------------
async def send_notice(config: DevConfig, text: BotText) -> None:
    """The refusal notice, straight to the dev group: the DB and the outbox may be what is broken."""
    from yordamchi.adapters.telegram.messenger import TelegramMessenger

    messenger = TelegramMessenger(config.bot_token)
    try:
        await messenger.send_text(config.dev_group_id, text, timeout=NOTICE_TIMEOUT_S)
    finally:
        await messenger.close()


async def log_notice(config: DevConfig, text: BotText) -> None:
    """`--dry-run`: the notice is logged as sent, never sent."""
    del config, text
    logger.info("startup_notice_skipped", reason="dry_run")


def build(boot: Boot, clock: Clock) -> "DevApp":
    """Every adapter, one `DevDeps`, the app. Nothing connects here: each adapter opens its connection on first use."""
    from yordamchi.adapters.claude.assistant import ClaudeAssistant
    from yordamchi.adapters.claude.executor import ClaudeAgentRuntime
    from yordamchi.adapters.claude.options import default_cli_path
    from yordamchi.adapters.gemini.stt import GeminiTranscriber
    from yordamchi.adapters.git.cli import GitCli
    from yordamchi.adapters.keystore.file import FileKeystore
    from yordamchi.adapters.macos.host import MacOsHost
    from yordamchi.adapters.mcp.client import McpHttpClient
    from yordamchi.adapters.openrouter.stt import OpenRouterTranscriber
    from yordamchi.adapters.ssh.shell import OpenSshRemoteShell
    from yordamchi.adapters.telegram.messenger import TelegramMessenger
    from yordamchi.bots.dev import runtime
    from yordamchi.bots.dev.unavailable import GitHubNotYet
    from yordamchi.dev.deps import DevDeps
    from yordamchi.ports.speech import Transcriber
    from yordamchi.shared.settings import defaults
    from yordamchi.shared.voice import service as voice

    config, paths = boot.config, boot.config.paths
    cli = default_cli_path(paths.home)
    messenger = TelegramMessenger(config.bot_token)
    assistant = ClaudeAssistant(
        cli_path=cli, search_path=boot.search_path, sessions_dir=paths.data / "sessions", clock=clock
    )
    transcribers: dict[str, Transcriber] = {}  # filled below: each reads its key through the deps (late binding)
    deps = DevDeps(
        bot=BotName.DEV,
        db=boot.db,
        peer_db=boot.peer_db,
        clock=clock,
        paths=paths,
        bootstrap=defaults.bootstrap_values(owner_id=config.owner_id, dev_group_id=config.dev_group_id),
        messenger=messenger,
        keystore=FileKeystore(paths.master_key),
        assistant=assistant,
        transcribers=transcribers,
        synthesizers={},  # text-to-speech is P3
        mac_host=MacOsHost(home=paths.home),
        mcp=McpHttpClient(),  # its callers are the P2 integration intents (T58), which answer S.NOT_YET now
        agents=ClaudeAgentRuntime(cli_path=cli, search_path=boot.search_path, githooks_dir=paths.githooks),
        git=GitCli(boot.search_path),
        github=GitHubNotYet(),
        remote_shell=OpenSshRemoteShell(ssh=SSH_BINARY, path=boot.ssh_path),
    )
    ffmpeg = paths.bin / "ffmpeg"
    google = GeminiTranscriber(api_key=partial(voice.read_speech_key, deps, "google"))
    openrouter = OpenRouterTranscriber(
        api_key=partial(voice.read_speech_key, deps, "openrouter"), ffmpeg=ffmpeg if ffmpeg.exists() else None
    )
    transcribers.update(google=google, openrouter=openrouter)
    closers: list[Closer] = [
        partial(assistant.close, timeout=ASSISTANT_CLOSE_S),
        google.aclose,
        openrouter.aclose,
        messenger.close,  # last: the outbox flush before it still sends
    ]
    return runtime.build_app(
        deps,
        bot=messenger.bot,
        version=boot.version,
        sdk_version=sdk_version(),
        degraded=boot.degraded,
        closers=closers,
    )


async def _dispose(boot: Boot) -> None:
    await boot.db.dispose()
    if boot.peer_db is not None:
        await boot.peer_db.dispose()


async def _probation(app: "DevApp", boot: Boot, stop: asyncio.Event) -> bool:
    """Wait while the supervisor probes this release; True once normal running may begin, False on SIGTERM."""
    from yordamchi.shared.health import service as health
    from yordamchi.shared.health.domain import Mode

    await app.start_probation()
    paths = boot.config.paths
    while await asyncio.to_thread(probation_names, paths, boot.version):
        try:
            async with asyncio.timeout(PROBATION_POLL_S):
                await stop.wait()
        except TimeoutError:
            continue
        return False
    await health.set_mode(app.deps, app.health, Mode.NORMAL)
    return True


async def serve(boot: Boot, clock: Clock) -> int:
    """Steps 9–10, then SIGTERM's shutdown (SPEC §2.6)."""
    app = build(boot, clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        if not boot.probation or await _probation(app, boot, stop):
            await app.start()
            logger.info("dev_bot_started", version=boot.version, degraded=list(app.health.degraded))
            await stop.wait()
        await app.stop()
    finally:
        await _dispose(boot)
    return EXIT_OK


async def dry_run(boot: Boot, clock: Clock) -> int:
    """Everything up to the loops, then a clean exit: no poll, no socket, no write, no message."""
    app = build(boot, clock)
    logger.info(
        "dry_run_ready",
        release=boot.version,
        degraded=list(boot.degraded),
        probation=boot.probation,
        handlers=len(app.dispatcher.sub_routers),
    )
    for close in app.closers:
        await close()
    await _dispose(boot)
    return EXIT_OK


async def run(*, dry: bool) -> int:
    from yordamchi.adapters.system_clock import SystemClock

    redact.install_asyncio_handler(asyncio.get_running_loop())
    environ = os.environ
    paths = bootstrap.load_paths(environ)
    version = release_version()
    logs = log.configure(
        process="dev",
        version=version,
        file=None if dry else paths.log_file("dev"),
        stream=sys.stderr if dry else None,
    )
    try:
        clock = SystemClock()
        boot = await prepare(environ, clock=clock, notify=log_notice if dry else send_notice, version=version)
        if isinstance(boot, int):
            return boot
        return await (dry_run(boot, clock) if dry else serve(boot, clock))
    finally:
        logs.stop()


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m yordamchi.bots.dev.app",
        description="Yordamchi dev bot (launchd uz.yordamchi.dev). Reads YORDAMCHI_ENV_FILE and YORDAMCHI_DATA_DIR.",
        epilog="Exit codes: 0 stopped cleanly; 78 refused to start (reason in run/dev.lasterror).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the startup checks and build every adapter, then exit without polling or sending anything",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    redact.install_process_hooks()  # before aiogram or the Agent SDK is imported (SPEC §2.5 step 1)
    arguments = _arguments(argv)
    return asyncio.run(run(dry=arguments.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
