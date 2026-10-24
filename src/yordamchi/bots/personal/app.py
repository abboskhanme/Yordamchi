"""`python -m yordamchi.bots.personal.app`: the personal bot's entry point and composition root (SPEC §2.5–§2.7, §5.3;
CONVENTIONS §4, §6, C4, C7). launchd runs it from `releases/current` with `YORDAMCHI_ENV_FILE` (`DATA/personal.env`)
and `YORDAMCHI_DATA_DIR` set (`ops/launchd/uz.yordamchi.personal.plist.tmpl`).

The same steps as `bots/dev/app.py` (C7 keeps the two bots from importing each other), for the personal process:
1. `os.umask(0o077)`; `core.redact` on stderr, stdout and every uncaught-exception path, before aiogram or the Agent
   SDK is imported. Vendor modules are imported inside functions.
2. Logging to `~/Library/Logs/Yordamchi/personal.jsonl`.
3. The env file (`core.config.load_personal_config`): its token, the owner id and the optional group id. Its Keychain
   scope never includes the dev token (UC-36). It never enters `os.environ`.
4. **Refusal**: a credential source that outranks the keychain login (`core.proc.REFUSED_ENV_VARS`, an
   `apiKeyHelper`, …) → `run/personal.lasterror`, `S.STARTUP_REFUSED` to the owner's DM at most once per 6 h
   (marker `run/personal.refused`), exit 78. The other refusals take the same path.
5. `os.environ` scrubbed of `CLAUDECODE` / `CLAUDE_CODE_*`.
6. The child PATH (shims, `~/.local/bin`, `DATA/bin`, the login shell's PATH, the system dirs). No `claude` → refused;
   no `ffmpeg` or `node` → degraded (voice notes, video downloads), shown in `holat?`.
7. `personal.db` must be at the Alembic head unless an update is under way; dev.db is opened read-only (`mode=ro`,
   `query_only`) for the bridge, when it exists. The bot never migrates.
8. `run/flags/probation.json` naming this version → probation (heartbeat and control socket only, P2).
9. Adapters → one `PersonalDeps` (no executor, git, GitHub or SSH field) → `runtime.PersonalApp`.
10. **The tool check** (NFR-SEC-08, spike S5): one throwaway ask with the exact tools an answer gets; the session runs
    `--restricted`, `dontAsk`, strict MCP, with `ENABLE_CLAUDEAI_MCP_SERVERS=false` and `SendMessage`/`ListAgents`
    disallowed (`adapters/claude/assistant.py`), and its `init` tool and MCP list must be the allowlist. A mismatch
    refuses the start (`S.PERSONAL_STARTUP_TOOLS_REFUSED`, exit 78) and is remembered per release and `claude`
    version, so launchd's restarts do not ask again for 6 h. Claude out of reach (a limit, the network) does not
    stop the bot: the first answer makes the same check.
11. The loops, under `core.aio.forever`, until SIGTERM; then the shutdown of SPEC §2.6 and exit 0.

`--dry-run` runs steps 1–9 and builds every adapter, `PersonalDeps` and the app, then exits 0 without asking Claude,
polling, binding the socket or sending anything: proof that a release starts, safe against a temp
`YORDAMCHI_DATA_DIR`.
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
from yordamchi.core.config import PersonalConfig
from yordamchi.core.errors import ConfigInvalid, ExternalError, PersonalToolsMismatch
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText
from yordamchi.ports.clock import Clock

if TYPE_CHECKING:
    from yordamchi.bots.personal.runtime import Closer, PersonalApp
    from yordamchi.persistence.uow import Database
    from yordamchi.ports.assistant import Assistant
    from yordamchi.ports.media import MediaAnalyzer
    from yordamchi.ports.speech import Synthesizer, Transcriber
    from yordamchi.ports.web import WebTools

logger = get_log(__name__)

EXIT_OK: Final = 0
EXIT_REFUSED: Final = proc.EXIT_REFUSED  # 78, EX_CONFIG
NOTICE_EVERY: Final = timedelta(hours=6)  # SPEC §2.5 step 3: the refusal notice at most once per 6 h
NOTICE_TIMEOUT_S: Final = 10.0
PROBE_TIMEOUT_S: Final = 10.0  # `claude --version` + `claude auth status` (no model is asked)
RELEASE_MARKER: Final = ".yordamchi-release.json"  # written by scripts/install.py into each release
DEV_VERSION: Final = "dev"  # running from a working copy, not an installed release
PROBATION_POLL_S: Final = 5.0
ASSISTANT_CLOSE_S: Final = 10.0
TOOLS_REASON: Final = "tools"
BOT: Final = BotName.PERSONAL

type Notify = Callable[[PersonalConfig, BotText], Awaitable[None]]
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
    return paths.run / f"{BOT}.refused"


def _read_marker(marker: Path) -> dict[str, object]:
    try:
        raw = json_value.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _marker_at(raw: Mapping[str, object]) -> datetime | None:
    at = raw.get("at")
    try:
        moment = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


def _notice_due(marker: Path, now: datetime) -> bool:
    last = _marker_at(_read_marker(marker))
    return last is None or now - last >= NOTICE_EVERY


async def refuse(
    paths: Paths,
    config: PersonalConfig | None,
    *,
    reason: str,
    what: str,
    notice: BotText | None,
    clock: Clock,
    notify: Notify,
) -> int:
    """Write `run/personal.lasterror` (one scrubbed line: what the supervisor quotes), tell the owner at most once per
    6 h, and return the exit code. `what` holds names, never values."""
    now = clock.now()
    line = redact.scrubbed(f"{now.isoformat()} startup_refused {reason}: {what}")
    await asyncio.to_thread(_write_private, paths.last_error(BOT), line + "\n")
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

    config: PersonalConfig
    version: str
    search_path: str  # every child's PATH (shims first)
    degraded: tuple[str, ...]
    db: "Database"
    peer_db: "Database | None"  # dev.db, read-only, for the bridge
    probation: bool


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
        config = bootstrap.load_personal_config(environ, home=home)
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
    db = open_database(paths.db_file(BOT))
    current = await schema_is_current(db, migrations or release_root() / "migrations")
    if not current and not update_in_progress(paths):
        await db.dispose()
        notice = S.STARTUP_SCHEMA_MISMATCH()
        return await refuse(
            paths, config, reason="schema", what="personal.db", notice=notice, clock=clock, notify=notify
        )
    peer_file = paths.db_file(BotName.DEV)
    peer_db = open_database(peer_file, readonly=True) if peer_file.exists() else None
    return Boot(
        config=config,
        version=version,
        search_path=search_path,
        degraded=binaries.missing_optional,
        db=db,
        peer_db=peer_db,
        probation=probation_names(paths, version),
    )


# --- the tool check (NFR-SEC-08, step 10) ---------------------------------------------------------------------------
def tools_marker(paths: Paths) -> Path:
    """`run/personal.tools-refused.json`: the release and `claude` version whose tool list was refused, and when."""
    return paths.run / f"{BOT}.tools-refused.json"


def tools_remembered(paths: Paths, *, release: str, cli: str, now: datetime) -> bool:
    """This release's tool list was refused on this `claude` version less than 6 h ago: refuse again without asking
    Claude (launchd restarts a refused bot within seconds, and each check is a model call)."""
    raw = _read_marker(tools_marker(paths))
    last = _marker_at(raw)
    same = raw.get("release") == release and raw.get("cli") == cli
    return same and last is not None and now - last < NOTICE_EVERY


async def check_tools(app: "PersonalApp", config: PersonalConfig, *, clock: Clock, notify: Notify) -> int | None:
    """Step 10: None when the bot may start; the exit code of the refusal (already reported) when it may not."""
    paths = config.paths
    try:
        status = await app.deps.assistant.probe(timeout=PROBE_TIMEOUT_S)
        cli = status.version or ""
    except ExternalError as error:
        logger.warning("tools_probe_failed", error_code=error.code.value)
        cli = ""
    notice = S.PERSONAL_STARTUP_TOOLS_REFUSED()
    if await asyncio.to_thread(tools_remembered, paths, release=app.version, cli=cli, now=clock.now()):
        what = "personal session tools (remembered)"
        return await refuse(paths, config, reason=TOOLS_REASON, what=what, notice=notice, clock=clock, notify=notify)
    try:
        verified = await app.verify_tools()
    except PersonalToolsMismatch:
        memory = {"at": clock.now().isoformat(), "release": app.version, "cli": cli}
        await asyncio.to_thread(_write_private, tools_marker(paths), json.dumps(memory))
        what = "personal session tools"
        return await refuse(paths, config, reason=TOOLS_REASON, what=what, notice=notice, clock=clock, notify=notify)
    logger.info("tools_checked", verified=verified, cli=cli)
    return None


# --- the composition root (C4: the only place adapters are constructed) ---------------------------------------------
def _home_chat(config: PersonalConfig) -> int:
    """The owner's DM (a private chat's id is the user's id): the personal bot's home chat (`access.policy_for`)."""
    return config.owner_id


async def send_notice(config: PersonalConfig, text: BotText) -> None:
    """The refusal notice, straight to the owner: the DB and the outbox may be what is broken."""
    from yordamchi.adapters.telegram.messenger import TelegramMessenger

    messenger = TelegramMessenger(config.bot_token)
    try:
        await messenger.send_text(_home_chat(config), text, timeout=NOTICE_TIMEOUT_S)
    finally:
        await messenger.close()


async def log_notice(config: PersonalConfig, text: BotText) -> None:
    """`--dry-run`: the notice is logged as sent, never sent."""
    del config, text
    logger.info("startup_notice_skipped", reason="dry_run")


def _web_tools(boot: Boot) -> "WebTools":
    """Screenshots, file fetches, page text and video downloads (T74): Chromium, urllib, yt-dlp and the static
    ffmpeg from `DATA/bin`; Node from `~/.local/bin` runs yt-dlp's YouTube challenges."""
    from yordamchi.adapters.web.browser import PlaywrightBrowser
    from yordamchi.adapters.web.download import HttpDownloader
    from yordamchi.adapters.web.encode import FfmpegEncoder
    from yordamchi.adapters.web.tools import LocalWebTools
    from yordamchi.adapters.web.ytdl import YtDlpDownloader

    paths = boot.config.paths
    node = paths.local_bin / "node"
    return LocalWebTools(
        browser=PlaywrightBrowser(),
        http=HttpDownloader(),
        videos=YtDlpDownloader(ffmpeg_dir=paths.bin, node=node if node.exists() else None),
        encoder=FfmpegEncoder(ffmpeg=paths.bin / "ffmpeg", ffprobe=paths.bin / "ffprobe", child_path=boot.search_path),
    )


@dataclass(frozen=True, slots=True)
class _Keyed:
    """The adapters that read a speech key from this bot's vault at call time, by provider."""

    transcribers: "dict[str, Transcriber]"
    synthesizers: "dict[str, Synthesizer]"
    analyzers: "dict[str, MediaAnalyzer]"
    fallback: "Assistant"
    closers: "tuple[Closer, ...]"


def _keyed_adapters(read_key: Callable[[str], Awaitable[str]], ffmpeg: Path | None) -> _Keyed:
    """STT, TTS in the SPEC §3.2.11 order (OpenRouter → Google → Edge, which needs no key), the video readers and the
    "zaxira model" (Q4: Gemini on the Google key). `read_key(provider)` reads the vault when a call needs the key
    (late binding, as in the dev bot), so a key pasted by chat works from the next message."""
    from yordamchi.adapters.edge_tts.tts import EdgeSynthesizer
    from yordamchi.adapters.gemini.assistant import GeminiAssistant
    from yordamchi.adapters.gemini.stt import GeminiTranscriber
    from yordamchi.adapters.gemini.tts import GeminiSynthesizer
    from yordamchi.adapters.gemini.video import GeminiMediaAnalyzer
    from yordamchi.adapters.openrouter.stt import OpenRouterTranscriber
    from yordamchi.adapters.openrouter.tts import OpenRouterSynthesizer
    from yordamchi.adapters.openrouter.video import OpenRouterMediaAnalyzer

    google_key, openrouter_key = partial(read_key, "google"), partial(read_key, "openrouter")
    google_stt = GeminiTranscriber(api_key=google_key)
    openrouter_stt = OpenRouterTranscriber(api_key=openrouter_key, ffmpeg=ffmpeg)
    openrouter_tts = OpenRouterSynthesizer(api_key=openrouter_key, ffmpeg=ffmpeg)
    google_tts = GeminiSynthesizer(api_key=google_key, ffmpeg=ffmpeg)
    edge_tts = EdgeSynthesizer(ffmpeg=ffmpeg)
    google_video = GeminiMediaAnalyzer(api_key=google_key)
    openrouter_video = OpenRouterMediaAnalyzer(api_key=openrouter_key)
    fallback = GeminiAssistant(api_key=google_key)
    return _Keyed(
        transcribers={"google": google_stt, "openrouter": openrouter_stt},
        synthesizers={"openrouter": openrouter_tts, "google": google_tts, "edge": edge_tts},
        analyzers={"google": google_video, "openrouter": openrouter_video},
        fallback=fallback,
        closers=(
            fallback.aclose,
            google_stt.aclose,
            openrouter_stt.aclose,
            openrouter_tts.aclose,
            google_tts.aclose,
            edge_tts.aclose,
            google_video.aclose,
            openrouter_video.aclose,
        ),
    )


def build(boot: Boot, clock: Clock) -> "PersonalApp":
    """Every adapter, one `PersonalDeps`, the app. Nothing connects here: each adapter opens its connection on first
    use. This process holds no executor, version-control, GitHub or SSH adapter (C7)."""
    from yordamchi.adapters.claude.assistant import ClaudeAssistant
    from yordamchi.adapters.claude.options import default_cli_path
    from yordamchi.adapters.keystore.keychain import KeychainKeystore
    from yordamchi.adapters.macos.host import MacOsHost
    from yordamchi.adapters.mcp.client import McpHttpClient
    from yordamchi.adapters.telegram.messenger import TelegramMessenger
    from yordamchi.bots.personal import runtime
    from yordamchi.personal.deps import PersonalDeps
    from yordamchi.shared.settings import defaults
    from yordamchi.shared.voice import service as voice

    config, paths = boot.config, boot.config.paths
    messenger = TelegramMessenger(config.bot_token)
    assistant = ClaudeAssistant(
        cli_path=default_cli_path(paths.home),
        search_path=boot.search_path,
        sessions_dir=paths.data / "sessions",
        clock=clock,
    )
    ffmpeg = paths.bin / "ffmpeg"
    keys: list[PersonalDeps] = []  # the deps, once built: the keyed adapters read the vault through them

    async def read_key(provider: str) -> str:
        return await voice.read_speech_key(keys[0], provider)

    keyed = _keyed_adapters(read_key, ffmpeg if ffmpeg.exists() else None)
    deps = PersonalDeps(
        bot=BOT,
        db=boot.db,
        peer_db=boot.peer_db,
        clock=clock,
        paths=paths,
        bootstrap=defaults.bootstrap_values(owner_id=config.owner_id, personal_group_id=config.personal_group_id),
        messenger=messenger,
        keystore=KeychainKeystore(bot=BOT, legacy_key_file=paths.master_key),
        assistant=assistant,
        transcribers=keyed.transcribers,
        synthesizers=keyed.synthesizers,
        mac_host=MacOsHost(home=paths.home),
        mcp=McpHttpClient(),  # the personal integrations gateway (T76) is not built: nothing calls it yet
        media_analyzers=keyed.analyzers,
        web=_web_tools(boot),
        fallback_assistant=keyed.fallback,
    )
    keys.append(deps)
    closers: list[Closer] = [
        partial(assistant.close, timeout=ASSISTANT_CLOSE_S),
        *keyed.closers,
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


async def _probation(app: "PersonalApp", boot: Boot, stop: asyncio.Event) -> bool:
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


async def _abandon(app: "PersonalApp") -> None:
    """A start that does not go on (a refused tool list, the dry run): stop what runs (probation's heartbeat and
    socket) and close the adapters. Nothing was recovered, so nothing is written."""
    tasks = list(app.tasks.values())
    app.tasks.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(app.socket.remove)
    for close in app.closers:
        await close()


async def serve(boot: Boot, clock: Clock, *, notify: Notify = send_notice) -> int:
    """Steps 9–11, then SIGTERM's shutdown (SPEC §2.6). The tool check runs before normal running, after a
    self-update's probation too (probation itself asks no model)."""
    app = build(boot, clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        if not boot.probation or await _probation(app, boot, stop):
            refused = await check_tools(app, boot.config, clock=clock, notify=notify)
            if refused is not None:
                await _abandon(app)
                return refused
            await app.start()
            logger.info("personal_bot_started", version=boot.version, degraded=list(app.health.degraded))
            await stop.wait()
        await app.stop()
    finally:
        await _dispose(boot)
    return EXIT_OK


async def dry_run(boot: Boot, clock: Clock) -> int:
    """Everything up to the tool check, then a clean exit: no model, no poll, no socket, no write, no message."""
    app = build(boot, clock)
    logger.info(
        "dry_run_ready",
        release=boot.version,
        degraded=list(boot.degraded),
        probation=boot.probation,
        bridge=boot.peer_db is not None,
        handlers=len(app.dispatcher.sub_routers),
    )
    await _abandon(app)
    await _dispose(boot)
    return EXIT_OK


async def run(*, dry: bool) -> int:
    from yordamchi.adapters.system_clock import SystemClock

    redact.install_asyncio_handler(asyncio.get_running_loop())
    environ = os.environ
    paths = bootstrap.load_paths(environ)
    version = release_version()
    logs = log.configure(
        process="personal",
        version=version,
        file=None if dry else paths.log_file("personal"),
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
        prog="python -m yordamchi.bots.personal.app",
        description=(
            "Yordamchi personal bot (launchd uz.yordamchi.personal). Reads YORDAMCHI_ENV_FILE and YORDAMCHI_DATA_DIR."
        ),
        epilog="Exit codes: 0 stopped cleanly; 78 refused to start (reason in run/personal.lasterror).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the startup checks and build every adapter, then exit without asking Claude, polling or sending",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    redact.install_process_hooks()  # before aiogram or the Agent SDK is imported (SPEC §2.5 step 1)
    arguments = _arguments(argv)
    return asyncio.run(run(dry=arguments.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
