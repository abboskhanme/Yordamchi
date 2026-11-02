"""The personal bot's Windows composition (owner request 2026-10-31: other people run only this bot, from a terminal),
proven on this Mac through the `platform` parameter.

- No control socket on Windows (`control_socket=False`): neither the normal loops nor probation start one.
- `build(platform="win32")`: DPAPI keystore, no socket loop, the assistant kills the Windows way.
- The terminal's PATH stands in for the login shell; `claude.exe`, ffmpeg, ffprobe and node are found on it.
- Ctrl+C / Ctrl+Break: where the loop has no signal handlers (the Proactor loop), `signal.signal` sets the stop event.
- macOS keeps today's paths (`DATA/bin/ffmpeg`, `~/.local/bin/claude`).
"""

import asyncio
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tests.factories import OWNER_ID, PERSONAL_BOT_TOKEN, Fakes, make_personal_deps
from tests.fakes.clock import FakeClock
from tests.fakes.security import FakeSecurity
from yordamchi.adapters.claude.options import default_cli_path
from yordamchi.bots.personal import app, runtime
from yordamchi.bots.personal.app import Boot
from yordamchi.core import keychain
from yordamchi.core.config import PersonalConfig
from yordamchi.core.keychain import DpapiStore
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText
from yordamchi.persistence.uow import Database

ROOT = Path(__file__).resolve().parents[4]
RELEASE = "2026.10.31-abc1234"


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    security = FakeSecurity()
    security.put(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    monkeypatch.setattr(keychain, "security_runner", security)
    return security


def _program(name: str) -> str:
    """A program's file name on the machine running the test: Windows finds one only by its extension (PATHEXT)."""
    return f"{name}.exe" if sys.platform == "win32" else name


def _executable(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


# --- the control socket --------------------------------------------------------------------------------------------
async def test_without_the_control_socket_no_loop_or_probation_serves_one(
    personal_db: Database, paths: Paths, fakes: Fakes
) -> None:
    deps = make_personal_deps(db=personal_db, paths=paths, fakes=fakes)
    windows = runtime.build_app(deps, bot=None, version=RELEASE, control_socket=False)
    assert "control_socket" not in windows._loops(polling=False)
    assert "control_socket" in runtime.build_app(deps, bot=None, version=RELEASE)._loops(polling=False)
    await windows.start_probation()
    try:
        assert set(windows.tasks) == {"heartbeat"}
        assert not paths.socket(BotName.PERSONAL).exists()
    finally:
        await app._abandon(windows)


@pytest.mark.skipif(sys.platform == "win32", reason="the control socket is a unix socket, macOS only")
async def test_with_the_control_socket_probation_serves_it(personal_db: Database, paths: Paths, fakes: Fakes) -> None:
    built = runtime.build_app(make_personal_deps(db=personal_db, paths=paths, fakes=fakes), bot=None, version=RELEASE)
    await built.start_probation()
    try:
        assert set(built.tasks) == {"heartbeat", "control_socket"}
    finally:
        await app._abandon(built)


# --- build() for Windows -------------------------------------------------------------------------------------------
async def _notify(config: PersonalConfig, text: BotText) -> None:
    del config, text


async def _no_login_shell(environ: object) -> str | None:
    del environ
    return None


def _migrate(db_path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["db_path"] = db_path
    config.attributes["skip_logging_config"] = True
    command.upgrade(config, "head")


async def test_the_windows_build_uses_dpapi_and_no_control_socket(paths: Paths) -> None:
    _executable(paths.local_bin, _program("claude"))
    _executable(paths.local_bin, _program("git"))
    _migrate(paths.db_file(BotName.PERSONAL))
    if sys.platform == "win32":  # the real start reads the real DPAPI store there, not the fake Keychain
        sealed = keychain.Keychain(BotName.PERSONAL, store=keychain.DpapiStore(paths.secret_store))
        sealed.write(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    env_file = paths.env_file(BotName.PERSONAL)
    env_file.write_text(f"PERSONAL_BOT_TOKEN={keychain.PERSONAL_BOT_TOKEN.reference}\nOWNER_ID={OWNER_ID}\n")
    environ = {
        "YORDAMCHI_DATA_DIR": str(paths.data),
        "YORDAMCHI_CONFIG_DIR": str(paths.config),
        "YORDAMCHI_LOG_DIR": str(paths.logs),
        "YORDAMCHI_ENV_FILE": str(env_file),
    }
    booted = await app.prepare(
        environ, clock=FakeClock(), notify=_notify, home=paths.home, version=RELEASE, login_path=_no_login_shell
    )
    assert isinstance(booted, Boot)
    built = app.build(booted, FakeClock(), platform="win32")
    try:
        assert built.control_socket is False
        assert "control_socket" not in built._loops(polling=False)
        chain = built.deps.keystore._keychain  # type: ignore[attr-defined]
        assert isinstance(chain._store, DpapiStore)
        assert chain._store.file(keychain.MASTER_KEY).parent == paths.secret_store / "uz.yordamchi"
        assert built.deps.assistant._platform == "win32"  # type: ignore[attr-defined]
    finally:
        await app._abandon(built)
        await booted.db.dispose()


# --- PATH, claude.exe, ffmpeg --------------------------------------------------------------------------------------
async def test_on_windows_the_terminal_path_is_the_login_path() -> None:
    assert await app.read_login_path({"PATH": "C:\\Tools;C:\\Windows"}, platform="win32") == "C:\\Tools;C:\\Windows"
    assert await app.read_login_path({}, platform="win32") is None


def test_windows_finds_ffmpeg_ffprobe_and_node_on_the_path(tmp_path: Path) -> None:
    paths = Paths.windows_defaults(tmp_path)
    tools = tmp_path / "tools"
    ffmpeg, ffprobe, node = (_executable(tools, _program(name)) for name in ("ffmpeg", "ffprobe", "node"))
    found = app.media_binaries(paths, str(tools), platform="win32")
    assert (found.ffmpeg, found.ffprobe, found.ffmpeg_dir, found.node) == (ffmpeg, ffprobe, tools, node)
    missing = app.media_binaries(paths, str(tmp_path / "empty"), platform="win32")
    assert missing.ffmpeg == paths.bin / "ffmpeg.exe"
    assert not missing.ffmpeg.exists()
    assert missing.node is None


@pytest.mark.skipif(sys.platform == "win32", reason="macOS lookup of extensionless programs")
def test_macos_keeps_the_static_ffmpeg_in_data_bin(tmp_path: Path) -> None:
    paths = Paths.defaults(tmp_path)
    tools = tmp_path / "tools"
    _executable(tools, "ffmpeg")
    found = app.media_binaries(paths, str(tools), platform="darwin")
    assert (found.ffmpeg, found.ffprobe, found.ffmpeg_dir) == (paths.bin / "ffmpeg", paths.bin / "ffprobe", paths.bin)
    assert found.node is None
    node = _executable(paths.local_bin, "node")
    assert app.media_binaries(paths, str(tools), platform="darwin").node == node


def test_windows_runs_the_native_claude_exe(tmp_path: Path) -> None:
    paths = Paths.windows_defaults(tmp_path)
    tools = tmp_path / "tools"
    assert app.cli_path(paths, str(tools), platform="win32") == paths.local_bin / "claude.exe"  # where setup says
    on_path = _executable(tools, "claude.exe")
    assert app.cli_path(paths, str(tools), platform="win32") == on_path
    native = _executable(paths.local_bin, "claude.exe")
    assert app.cli_path(paths, str(tools), platform="win32") == native
    assert app.cli_path(Paths.defaults(tmp_path), str(tools), platform="darwin") == default_cli_path(tmp_path)


# --- stop signals --------------------------------------------------------------------------------------------------
class _ProactorLike:
    """A loop without `add_signal_handler`, as on Windows."""

    def __init__(self) -> None:
        self.soon: list[Callable[[], object]] = []

    def add_signal_handler(self, signum: int, callback: Callable[[], object]) -> None:
        del signum, callback
        raise NotImplementedError

    def call_soon_threadsafe(self, callback: Callable[[], object]) -> None:
        self.soon.append(callback)


def test_without_loop_signal_handlers_signal_signal_sets_the_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, Callable[[int, object], None]] = {}
    monkeypatch.setattr(signal, "signal", installed.setdefault)
    loop, stop = _ProactorLike(), asyncio.Event()
    fallback = app.install_stop_handlers(loop, stop, platform="win32")  # type: ignore[arg-type]
    assert set(fallback) == set(installed) >= {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGINT](signal.SIGINT, None)
    assert not stop.is_set()  # handed to the loop thread, not set from the signal handler
    for callback in loop.soon:
        callback()
    assert stop.is_set()


def test_ctrl_break_stops_the_bot_only_where_it_exists() -> None:
    assert app.stop_signals("darwin") == (signal.SIGTERM, signal.SIGINT)
    brk = getattr(signal, "SIGBREAK", None)
    expected = (signal.SIGTERM, signal.SIGINT) if brk is None else (signal.SIGTERM, signal.SIGINT, brk)
    assert app.stop_signals("win32") == expected


def test_a_loop_with_signal_handlers_keeps_them(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_fallback(*args: object) -> None:
        raise AssertionError(args)

    class UnixLike(_ProactorLike):
        def __init__(self) -> None:
            super().__init__()
            self.handlers: dict[int, Callable[[], object]] = {}

        def add_signal_handler(self, signum: int, callback: Callable[[], object]) -> None:
            self.handlers[signum] = callback

    monkeypatch.setattr(signal, "signal", no_fallback)
    loop, stop = UnixLike(), asyncio.Event()
    assert app.install_stop_handlers(loop, stop, platform="darwin") == ()  # type: ignore[arg-type]
    assert set(loop.handlers) == {signal.SIGTERM, signal.SIGINT}
    loop.handlers[signal.SIGTERM]()
    assert stop.is_set()
