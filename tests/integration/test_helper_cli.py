"""`bin/yordamchi-helper` as a real subprocess against a control socket in a temp dir (SPEC §3.2.10 step 6; STACK
§4.2): it prints the headers the bot answers and nothing else, fails closed without a socket, a token, a server name or
a usable answer, and runs through a shell from the quoted `headersHelper` command exactly as Claude Code runs it, from
a data directory with a space in its path. The last test mounts the real `integrations.serve_headers` behind the socket.
"""

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tests.factories import INTEGRATION_TOKEN, INTEGRATION_URL, Fakes, make_mcp_tools, make_owner_origin, vault_secret
from yordamchi.core import control, proc
from yordamchi.core.control import HeadersAnswer, HeadersRequest
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.integrations import domain, service
from yordamchi.shared.integrations.service import AddIntegration, IntegrationsState

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "bin" / "yordamchi-helper"
TOKEN = "tok-session-9"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class FakeBot:
    """The dev bot's end of the control socket with a scripted `headers` answer."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self.requests: list[HeadersRequest] = []
        self.answer = HeadersAnswer(headers={"Authorization": "Bearer fixture-access"})
        self.silent = False

    async def handle(self, request: HeadersRequest) -> HeadersAnswer:
        self.requests.append(request)
        return self.answer

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if not self.silent:
            writer.write(await control.dispatch_headers(line, handler=self.handle))
            await writer.drain()
        writer.close()
        await writer.wait_closed()


@pytest.fixture
def run_dir() -> Iterator[Path]:
    """`DATA/run` stand-in; a unix socket path must stay under 104 bytes on macOS."""
    folder = Path(tempfile.mkdtemp(prefix="yd-", dir="/tmp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
async def bot(run_dir: Path) -> AsyncIterator[FakeBot]:
    fake = FakeBot(run_dir / "dev.sock")
    async with await asyncio.start_unix_server(fake.serve, path=str(fake.socket)):
        yield fake


def helper_env(home: Path, *, socket: Path | None, server: str | None = "bonvi_hr") -> dict[str, str]:
    """What Claude Code gives a headersHelper of an SDK-supplied server: the session's env plus the server name."""
    env = {"PATH": SYSTEM_PATH, "HOME": str(home), "YORDAMCHI_PY": sys.executable, "YORDAMCHI_SESSION_TOKEN": TOKEN}
    if socket is not None:
        env["YORDAMCHI_SOCK"] = str(socket)
    if server is not None:
        env["CLAUDE_CODE_MCP_SERVER_NAME"] = server
        env["CLAUDE_CODE_MCP_SERVER_URL"] = INTEGRATION_URL
    return env


async def run_helper(env: dict[str, str], cwd: Path, *args: str) -> proc.ProcResult:
    return await proc.run([str(HELPER), *(args or ("headers",))], env=env, cwd=cwd, timeout=30)


async def test_the_helper_prints_the_headers_the_bot_answers(bot: FakeBot, tmp_path: Path) -> None:
    result = await run_helper(helper_env(tmp_path, socket=bot.socket), tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"Authorization": "Bearer fixture-access"}
    assert result.stderr == b""
    assert bot.requests == [HeadersRequest("bonvi_hr", TOKEN)]


async def test_the_helper_runs_from_the_quoted_command_in_a_shell(bot: FakeBot, tmp_path: Path) -> None:
    release = tmp_path / "Application Support" / "Yordamchi" / "releases" / "current"
    (release / "bin").mkdir(parents=True)
    shutil.copy2(HELPER, release / "bin" / "yordamchi-helper")
    command = domain.helper_command(str(release))

    result = await proc.run(["/bin/sh", "-c", command], env=helper_env(tmp_path, socket=bot.socket), timeout=30)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"Authorization": "Bearer fixture-access"}


@pytest.mark.parametrize(
    ("socket", "server", "reason"),
    [(False, "bonvi_hr", b"socket_unknown"), (True, None, b"server_unknown")],
)
async def test_missing_context_means_no_headers(
    bot: FakeBot, tmp_path: Path, socket: bool, server: str | None, reason: bytes
) -> None:
    env = helper_env(tmp_path, socket=bot.socket if socket else None, server=server)

    result = await run_helper(env, tmp_path)

    assert result.returncode == 1
    assert result.stdout == b""
    assert reason in result.stderr
    assert bot.requests == []


async def test_no_socket_listening_means_no_headers(run_dir: Path, tmp_path: Path) -> None:
    result = await run_helper(helper_env(tmp_path, socket=run_dir / "dev.sock"), tmp_path)

    assert (result.returncode, result.stdout) == (1, b"")
    assert b"no_headers" in result.stderr


async def test_a_refusal_or_silence_means_no_headers(bot: FakeBot, tmp_path: Path) -> None:
    bot.answer = HeadersAnswer(error_code="needs_reauth")
    refused = await run_helper(helper_env(tmp_path, socket=bot.socket), tmp_path)
    bot.silent = True
    silent = await run_helper(helper_env(tmp_path, socket=bot.socket), tmp_path)

    for result in (refused, silent):
        assert (result.returncode, result.stdout) == (1, b"")


async def test_the_helper_refuses_other_commands_and_a_missing_python(bot: FakeBot, tmp_path: Path) -> None:
    usage = await run_helper(helper_env(tmp_path, socket=bot.socket), tmp_path, "token")
    env = helper_env(tmp_path, socket=bot.socket)
    del env["YORDAMCHI_PY"]
    no_python = await run_helper(env, tmp_path)

    assert (usage.returncode, usage.stdout) == (1, b"")
    assert (no_python.returncode, no_python.stdout) == (1, b"")
    assert bot.requests == []


async def test_the_real_service_behind_the_socket(
    dev_deps: DevDeps, fakes: Fakes, run_dir: Path, tmp_path: Path
) -> None:
    """Mounted the way T34/T58 will: `dispatch_headers` with `serve_headers` and the turns feature's token check."""
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools(), headers={"Authorization": f"Bearer {INTEGRATION_TOKEN}"})
    ref = await vault_secret(dev_deps, "generic_secret", INTEGRATION_TOKEN)
    state = IntegrationsState()
    await service.add_integration(
        dev_deps, state, make_owner_origin(), AddIntegration("Bonvi HR", INTEGRATION_URL, secret=ref)
    )

    async def handle(request: HeadersRequest) -> HeadersAnswer:
        return await service.serve_headers(dev_deps, request, session_known=lambda token: token == TOKEN)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await control.dispatch_headers(await reader.readline(), handler=handle))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket = run_dir / "dev.sock"
    async with await asyncio.start_unix_server(serve, path=str(socket)):
        served = await run_helper(helper_env(tmp_path, socket=socket), tmp_path)
        unknown = await run_helper(helper_env(tmp_path, socket=socket, server="maktab"), tmp_path)

    assert served.returncode == 0, served.stderr
    assert json.loads(served.stdout) == {"Authorization": f"Bearer {INTEGRATION_TOKEN}"}
    assert (unknown.returncode, unknown.stdout) == (1, b"")
    assert INTEGRATION_TOKEN.encode() not in unknown.stderr + served.stderr
