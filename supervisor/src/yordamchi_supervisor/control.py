"""The supervisor's end of a bot's control socket `run/<bot>.sock` (SPEC §3.1.1): one JSON line in, one JSON line out.

Only `selftest` is used here. The request is `{"method": "selftest", "nonce": …}`; a healthy bot answers within 20 s
with `{"ok": true, "nonce": <the same nonce>, "steps": {…}}` after a synthetic owner update went through access →
inbox → burst → intent → outbox → a real `sendChatAction`. Anything else (no socket, refused, a timeout, a malformed
line, another nonce) is not a pass. A copy of the wire format, because A6 forbids importing `yordamchi.core.control`.
"""

import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from yordamchi_supervisor.paths import Bot, SupervisorPaths

SELFTEST_TIMEOUT_S: Final = 20.0
MAX_LINE_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class SelftestAnswer:
    answered: bool  # False: no answer at all (not listening yet, a timeout): worth asking again
    ok: bool = False
    failed_step: str = ""  # the first step the bot reported as failed


class SelfTester(Protocol):
    def selftest(self, bot: Bot, nonce: str) -> SelftestAnswer: ...


def _exchange(path: Path, line: bytes, timeout: float) -> bytes | None:
    deadline = time.monotonic() + timeout
    buffer = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
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
    except OSError:
        return None
    return buffer.partition(b"\n")[0]


def read_selftest_answer(line: bytes | None, nonce: str) -> SelftestAnswer:
    if line is None:
        return SelftestAnswer(answered=False)
    try:
        document = json.loads(line)
    except ValueError:
        return SelftestAnswer(answered=True, failed_step="malformed")
    if not isinstance(document, dict):
        return SelftestAnswer(answered=True, failed_step="malformed")
    steps = document.get("steps")
    failed = (
        next((str(name) for name, passed in steps.items() if passed is not True), "") if isinstance(steps, dict) else ""
    )
    if document.get("nonce") != nonce:
        return SelftestAnswer(answered=True, failed_step="nonce")
    if document.get("ok") is not True:
        return SelftestAnswer(answered=True, failed_step=failed or str(document.get("error_code", "not_ok"))[:64])
    return SelftestAnswer(answered=True, ok=True)


class SocketSelfTester:
    def __init__(self, paths: SupervisorPaths, timeout: float = SELFTEST_TIMEOUT_S) -> None:
        self._paths = paths
        self._timeout = timeout

    def selftest(self, bot: Bot, nonce: str) -> SelftestAnswer:
        line = json.dumps({"method": "selftest", "nonce": nonce}, separators=(",", ":")).encode() + b"\n"
        return read_selftest_answer(_exchange(self._paths.socket(bot), line, self._timeout), nonce)
