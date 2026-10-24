"""The personal bot's control socket `run/personal.sock` (SPEC §2.2, §3.1.1): one JSON request line in, one out.

The personal bot runs no executor session, so no shim, hook or helper ever calls it: of SPEC §3.1.1 it serves
`status` only (the supervisor, the check script): `{version, mode, live_turns, drained}`, where `live_turns` is the
answers being written right now (0 or 1). Anything else, a line that is not JSON, one that is too long or one that
does not arrive within `READ_TIMEOUT_S` is answered `{"error_code": …}`. The socket is 0600 in the 0700 `run/`.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

from yordamchi.core import control
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName

log = get_log(__name__)

STATUS: Final = "status"
READ_TIMEOUT_S: Final = 5.0
WRITE_TIMEOUT_S: Final = 5.0

type StatusReader = Callable[[], dict[str, object]]


class ControlSocket:
    """Serves `path` for the life of the process (`aio.forever("control_socket", socket.serve)`)."""

    def __init__(self, bot: BotName, *, path: Path, status: StatusReader) -> None:
        self._bot = bot
        self._path = path
        self._status = status

    async def serve(self) -> None:
        """Bind (replacing a stale socket file a crash left), make it 0600, answer until cancelled."""
        await asyncio.to_thread(self._prepare)
        server = await asyncio.start_unix_server(self._client, path=str(self._path), limit=control.MAX_LINE_BYTES + 2)
        try:
            await asyncio.to_thread(self._path.chmod, 0o600)
            log.info("control_socket_listening", bot=self._bot.value)
            async with server:
                await server.serve_forever()
        finally:
            await asyncio.to_thread(self.remove)

    def _prepare(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path.unlink(missing_ok=True)

    def remove(self) -> None:
        """Shutdown: the socket file goes with the process, so a caller fails fast instead of waiting on a dead one."""
        self._path.unlink(missing_ok=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                async with asyncio.timeout(READ_TIMEOUT_S):
                    line = await reader.readline()
            except (TimeoutError, ValueError):  # ValueError: a line past the reader's limit
                answer = control.error_line("bad_request")
            else:
                answer = self.answer(line)
            writer.write(answer)
            async with asyncio.timeout(WRITE_TIMEOUT_S):
                await writer.drain()
        except (TimeoutError, OSError) as error:
            log.warning("control_socket_client_failed", error_type=type(error).__name__)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def answer(self, line: bytes) -> bytes:
        """One request line → one answer line."""
        method = control.method_of(line)
        if method == STATUS:
            reply = (json.dumps(self._status(), separators=(",", ":")) + "\n").encode()
        else:
            reply = control.error_line("unknown_method" if method is not None else "bad_request")
        log.info("control_socket_answered", method=method if method == STATUS else "other")
        return reply
