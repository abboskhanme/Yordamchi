"""core.control: the gate methods of the control socket on the wire (SPEC §3.1.1) — both ends fail closed."""

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from yordamchi.core import control
from yordamchi.core.config import load_cli_config
from yordamchi.core.control import GateAnswer, GateCheck, ProtocolError, SecretAnswer, SecretBlocked
from yordamchi.core.errors import DecisionNotFound

CHECK = GateCheck("ssh", ("prod", "uptime"), "/Users/owner/Documents/Projects/BonviHR", "tok-1")
REPORT = SecretBlocked("/Users/owner/Documents/Projects/BonviHR", "config/.env", 3, "env_file", "tok-1")


def test_requests_round_trip() -> None:
    assert control.parse_request(control.gate_check_request(CHECK)) == CHECK
    assert control.parse_request(control.secret_blocked_request(REPORT)) == REPORT
    assert control.method_of(control.gate_check_request(CHECK)) == control.GATE_CHECK
    assert control.gate_check_request(CHECK).endswith(b"\n")


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"not json\n", "bad_request"),
        (b"[1, 2]\n", "bad_request"),
        (b'{"method": "gate_check", "tool": "ssh", "argv": "prod", "cwd": "/", "session_token": "t"}', "bad_request"),
        (b'{"method": "gate_check", "tool": "ssh", "argv": [1], "cwd": "/", "session_token": "t"}', "bad_request"),
        (b'{"method": "gate_check", "tool": "ssh", "argv": [], "cwd": "/"}', "bad_request"),
        (
            b'{"method": "secret_blocked", "cwd": "/", "file": "f", "line": true, "rule": "r", "session_token": "t"}',
            "bad_request",
        ),
        (b'{"method": "headers", "session_token": "t"}', "unknown_method"),
        (b'{"method": "gate_check"' + b" " * control.MAX_LINE_BYTES + b"}", "bad_request"),
    ],
)
def test_anything_else_is_a_protocol_error(line: bytes, code: str) -> None:
    with pytest.raises(ProtocolError) as raised:
        control.parse_request(line)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"allow": true, "reason": "free"}', GateAnswer(True, "free")),
        (b'{"allow": false, "reason": "remote_command"}', GateAnswer(False, "remote_command")),
        (b'{"allow": "yes", "reason": "free"}', GateAnswer(False, "free")),  # only `true` is a yes
        (b'{"error_code": "bad_request"}', GateAnswer(False, "bad_request")),
        (b"garbage", control.REFUSED),
        (None, control.REFUSED),
    ],
)
def test_the_cli_reads_anything_but_a_clear_yes_as_no(line: bytes | None, expected: GateAnswer) -> None:
    assert control.read_gate_answer(line) == expected


def test_a_secret_answer_without_ok_means_blocked() -> None:
    assert control.read_secret_answer(b'{"ok": true, "enforce": false}') == SecretAnswer(True, False)
    assert control.read_secret_answer(b'{"ok": true}') == SecretAnswer(True, True)
    assert control.read_secret_answer(b'{"error_code": "x"}') is None
    assert control.read_secret_answer(None) is None


async def _allow(check: GateCheck) -> GateAnswer:
    return GateAnswer(allow=check.tool == "ssh", reason="free")


async def _report(report: SecretBlocked) -> SecretAnswer:
    if report.line < 0:
        raise DecisionNotFound(decision_id=report.line)
    return SecretAnswer(ok=True, enforce=True)


HANDLERS = control.GateHandlers(gate_check=_allow, secret_blocked=_report)


async def test_dispatch_answers_each_method_and_turns_failures_into_error_codes() -> None:
    assert json.loads(await control.dispatch(control.gate_check_request(CHECK), handlers=HANDLERS)) == {
        "allow": True,
        "reason": "free",
    }
    assert json.loads(await control.dispatch(control.secret_blocked_request(REPORT), handlers=HANDLERS)) == {
        "ok": True,
        "enforce": True,
    }
    failing = SecretBlocked("/", "f", -1, "r", "t")
    assert json.loads(await control.dispatch(control.secret_blocked_request(failing), handlers=HANDLERS)) == {
        "error_code": "decision_not_found"
    }
    assert json.loads(await control.dispatch(b"{}", handlers=HANDLERS)) == {"error_code": "bad_request"}


# --- request(): the CLI's socket call ---------------------------------------------------------------------------
@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A unix socket path must stay under 104 bytes on macOS; pytest's tmp_path can be longer."""
    folder = Path(tempfile.mkdtemp(prefix="yd-", dir="/tmp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
async def server(short_dir: Path) -> AsyncIterator[Path]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if b"silent" not in line:
            writer.write(await control.dispatch(line, handlers=HANDLERS))
            await writer.drain()
        writer.close()

    path = short_dir / "dev.sock"
    async with await asyncio.start_unix_server(serve, path=str(path)):
        yield path


async def test_request_gets_one_answer_line(server: Path) -> None:
    answer = await asyncio.to_thread(control.request, server, control.gate_check_request(CHECK), timeout=5)
    assert control.read_gate_answer(answer) == GateAnswer(True, "free")


async def test_request_fails_closed_without_an_answer(server: Path, short_dir: Path) -> None:
    closed = await asyncio.to_thread(control.request, server, b'{"method": "silent"}\n', timeout=5)
    missing = await asyncio.to_thread(control.request, short_dir / "none.sock", b"{}\n", timeout=1)
    assert closed is None
    assert missing is None


def test_the_cli_config_reads_the_session_environment() -> None:
    token = "tok-9f3a"
    config = load_cli_config({"YORDAMCHI_SOCK": "/d/run/dev.sock", "YORDAMCHI_SESSION_TOKEN": token, "PATH": "/bin"})
    assert (config.socket, config.session_token, config.search_path) == (Path("/d/run/dev.sock"), token, "/bin")
    assert config.secret_hashes == Path("/d/run/secret-hashes.json")
    empty = load_cli_config({})
    assert (empty.socket, empty.session_token, empty.secret_hashes) == (None, "", None)
    assert token not in repr(config)  # the token never lands in a log line through repr
    assert token not in repr(CHECK)
