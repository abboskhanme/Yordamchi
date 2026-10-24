"""The control socket's gate methods on the wire (SPEC §3.1.1): one JSON object per line in, one per line out.

Shared by both ends, so it lives in `core`: `bots/cli` may import nothing else (A9). The CLI side (the shims, the
pre-push hook) builds requests, sends them with `request()` and reads the answers; the bot side
(`bots/dev/app.py`, T34) hands each request line to `dispatch` with the gate service behind it:

    answer = await control.dispatch(line, handlers=control.GateHandlers(gate_check=…, secret_blocked=…))

Everything here is pure except `request()`, the CLI's one socket call. Fail closed on both ends: `dispatch` turns
anything malformed into an error answer, and the CLI reads every answer that is not a well-formed yes
(`{"allow": true}`; `{"ok": true, "enforce": false}`) as a no. The session token says which executor session is
calling; it is not a security boundary (SPEC §3.1.1), the gate's forbidden rules are.
"""

import json
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from yordamchi.core.errors import YordamchiError

GATE_CHECK: Final = "gate_check"
SECRET_BLOCKED: Final = "secret_blocked"  # noqa: S105  # a method name, not a secret
GATE_METHODS: Final = frozenset({GATE_CHECK, SECRET_BLOCKED})
MAX_LINE_BYTES: Final = 64 * 1024
MAX_ARGS: Final = 1024


class ProtocolError(ValueError):
    """A request line that is not one of ours; `code` goes back as `error_code`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class GateCheck:
    """A shim is about to run `tool argv…` in `cwd`."""

    tool: str
    argv: tuple[str, ...]
    cwd: str
    session_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecretBlocked:
    """The pre-push scan found a secret at `file:line` by `rule`. Never the secret itself."""

    cwd: str
    file: str
    line: int
    rule: str
    session_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GateAnswer:
    allow: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SecretAnswer:
    ok: bool
    enforce: bool


REFUSED: Final = GateAnswer(allow=False, reason="no_answer")


def _line(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


# --- the CLI side -----------------------------------------------------------------------------------------------
def gate_check_request(check: GateCheck) -> bytes:
    return _line(
        {
            "method": GATE_CHECK,
            "tool": check.tool,
            "argv": list(check.argv),
            "cwd": check.cwd,
            "session_token": check.session_token,
        }
    )


def secret_blocked_request(report: SecretBlocked) -> bytes:
    return _line(
        {
            "method": SECRET_BLOCKED,
            "cwd": report.cwd,
            "file": report.file,
            "line": report.line,
            "rule": report.rule,
            "session_token": report.session_token,
        }
    )


def _object(line: bytes | None) -> dict[str, object] | None:
    if line is None or len(line) > MAX_LINE_BYTES:
        return None
    try:
        document: object = json.loads(line)
    except ValueError:
        return None
    return {str(key): value for key, value in document.items()} if isinstance(document, dict) else None


def read_gate_answer(line: bytes | None) -> GateAnswer:
    """The bot's answer to `gate_check`; no answer or anything malformed is a refusal."""
    document = _object(line)
    if document is None:
        return REFUSED
    reason = document.get("reason")
    reason_text = reason if isinstance(reason, str) else str(document.get("error_code", "malformed"))
    return GateAnswer(allow=document.get("allow") is True, reason=reason_text[:64])


def read_secret_answer(line: bytes | None) -> SecretAnswer | None:
    """The bot's answer to `secret_blocked`, or None when there is none: the hook then blocks."""
    document = _object(line)
    if document is None or document.get("ok") is not True:
        return None
    return SecretAnswer(ok=True, enforce=document.get("enforce") is not False)


# --- the bot side -----------------------------------------------------------------------------------------------
def _text(document: Mapping[str, object], key: str, *, limit: int = 4096) -> str:
    value = document.get(key)
    if not isinstance(value, str) or len(value) > limit:
        raise ProtocolError("bad_request")
    return value


def method_of(line: bytes) -> str | None:
    """The request's `method`, so the socket server can route the gate's methods here and the others elsewhere."""
    document = _object(line)
    method = document.get("method") if document is not None else None
    return method if isinstance(method, str) else None


def parse_request(line: bytes) -> GateCheck | SecretBlocked:
    """One request line → its typed request. `ProtocolError` for anything else."""
    document = _object(line)
    if document is None:
        raise ProtocolError("bad_request")
    method = document.get("method")
    token = _text(document, "session_token", limit=256)
    if method == GATE_CHECK:
        argv = document.get("argv")
        if not isinstance(argv, list) or len(argv) > MAX_ARGS or not all(isinstance(item, str) for item in argv):
            raise ProtocolError("bad_request")
        return GateCheck(_text(document, "tool", limit=16), tuple(argv), _text(document, "cwd"), token)
    if method == SECRET_BLOCKED:
        line_no = document.get("line")
        if not isinstance(line_no, int) or isinstance(line_no, bool):
            raise ProtocolError("bad_request")
        return SecretBlocked(
            _text(document, "cwd"), _text(document, "file"), line_no, _text(document, "rule", limit=64), token
        )
    raise ProtocolError("unknown_method")


def gate_answer_line(answer: GateAnswer) -> bytes:
    return _line({"allow": answer.allow, "reason": answer.reason})


def secret_answer_line(answer: SecretAnswer) -> bytes:
    return _line({"ok": answer.ok, "enforce": answer.enforce})


def error_line(code: str) -> bytes:
    return _line({"error_code": code})


@dataclass(frozen=True, slots=True)
class GateHandlers:
    """What T34 mounts: each resolves the session token to the session's scope and calls the gate service."""

    gate_check: Callable[[GateCheck], Awaitable[GateAnswer]]
    secret_blocked: Callable[[SecretBlocked], Awaitable[SecretAnswer]]


async def dispatch(line: bytes, *, handlers: GateHandlers) -> bytes:
    """One gate request line → one answer line. A malformed request or an expected failure of the gate
    (`YordamchiError`) becomes `{"error_code": …}`, which the CLI reads as a no."""
    try:
        request = parse_request(line)
        if isinstance(request, GateCheck):
            return gate_answer_line(await handlers.gate_check(request))
        return secret_answer_line(await handlers.secret_blocked(request))
    except ProtocolError as error:
        return error_line(error.code)
    except YordamchiError as error:
        return error_line(error.code.value)


# --- the CLI end: the one socket call -----------------------------------------------------------------------------
def request(socket_path: Path, line: bytes, *, timeout: float) -> bytes | None:
    """Send one request line and read one answer line (without its newline), the whole exchange bounded by
    `timeout`. None on any failure (no socket, refused, timed out, closed early, an over-long answer)."""
    deadline = time.monotonic() + timeout
    buffer = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            connection.sendall(line)
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(buffer) > MAX_LINE_BYTES:
                    return None
                connection.settimeout(remaining)
                chunk = connection.recv(4096)
                if not chunk:
                    return None
                buffer += chunk
    except OSError:  # a timeout too, and a path too long for a unix socket
        return None
    answer, _, _ = buffer.partition(b"\n")
    return answer
