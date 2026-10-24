"""The dev bot's control socket `run/dev.sock` (SPEC §3.1.1): one JSON request line in, one JSON answer line out.

- `gate_check` (the ssh/scp/sftp/rsync shims) and `secret_blocked` (the pre-push hook) go to the gate through
  `core.control.dispatch`. The caller's `session_token` names its executor session, whose gate scope
  `turns.TurnsState.scope_for` holds; an unknown or closed session gets no scope, and the gate refuses it.
- `status` (the supervisor, the check script): `{version, mode, live_turns, drained}`.

Anything else, a line that is not JSON, one that is too long or one that does not arrive within `READ_TIMEOUT_S` is
answered `{"error_code": …}`, which every caller reads as a no (fail closed). The socket is 0600 in the 0700 `run/`;
the session token is no boundary against same-user processes, the gate's forbidden rules keep the agent away from
it (`yordamchi_socket`).
"""

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

from yordamchi.bots.dev import jobs
from yordamchi.core import control
from yordamchi.core.log import get_log
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate.service import SecretReport, ShimRequest
from yordamchi.dev.turns.service import TurnsState
from yordamchi.shared.health.service import HealthState

log = get_log(__name__)

STATUS: Final = "status"
READ_TIMEOUT_S: Final = 5.0  # a caller writes its one line at once (the shims wait ≤ 10 s for the answer)
WRITE_TIMEOUT_S: Final = 5.0

type StatusReader = Callable[[], dict[str, object]]


class ControlSocket:
    """Serves `path` for the life of the process (`aio.forever("control_socket", socket.serve)`)."""

    def __init__(
        self, deps: DevDeps, turns: TurnsState, health: HealthState, *, path: Path, status: StatusReader
    ) -> None:
        self._deps = deps
        self._health = health
        self._turns = turns
        self._path = path
        self._status = status
        self._handlers = control.GateHandlers(gate_check=self._gate_check, secret_blocked=self._secret_blocked)

    async def serve(self) -> None:
        """Bind (replacing a stale socket file a crash left), make it 0600, answer until cancelled."""
        await asyncio.to_thread(self._prepare)
        server = await asyncio.start_unix_server(self._client, path=str(self._path), limit=control.MAX_LINE_BYTES + 2)
        try:
            await asyncio.to_thread(self._path.chmod, 0o600)
            log.info("control_socket_listening", bot=self._deps.bot.value)
            async with server:
                await server.serve_forever()
        finally:
            await asyncio.to_thread(self.remove)

    def _prepare(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path.unlink(missing_ok=True)

    def remove(self) -> None:
        """Shutdown: the socket file goes with the process, so a shim fails fast instead of waiting on a dead one."""
        self._path.unlink(missing_ok=True)

    # --- one connection -------------------------------------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                async with asyncio.timeout(READ_TIMEOUT_S):
                    line = await reader.readline()
            except (TimeoutError, ValueError):  # ValueError: a line past the reader's limit
                answer = control.error_line("bad_request")
            else:
                answer = await self.answer(line)
            writer.write(answer)
            async with asyncio.timeout(WRITE_TIMEOUT_S):
                await writer.drain()
        except (TimeoutError, OSError) as error:
            log.warning("control_socket_client_failed", error_type=type(error).__name__)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def answer(self, line: bytes) -> bytes:
        """One request line → one answer line. A failure inside the gate is an error answer, never a yes."""
        method = control.method_of(line)
        if method in control.GATE_METHODS:
            dispatched = control.dispatch(line, handlers=self._handlers)
            answered = await jobs.run(self._deps, self._health, dispatched, job="control_socket")
            reply = answered if answered is not None else control.error_line("internal")
        elif method == STATUS:
            reply = (json.dumps(self._status(), separators=(",", ":")) + "\n").encode()
        else:
            reply = control.error_line("unknown_method" if method is not None else "bad_request")
        log.info("control_socket_answered", method=method if method in {*control.GATE_METHODS, STATUS} else "other")
        return reply

    # --- the gate's two methods -----------------------------------------------------------------------------------
    async def _gate_check(self, request: control.GateCheck) -> control.GateAnswer:
        scope = self._turns.scope_for(request.session_token)
        shim = ShimRequest(tool=request.tool, argv=request.argv, cwd=request.cwd)
        answer = await gate.check_shim(self._deps, scope, shim)
        return control.GateAnswer(allow=answer.allow, reason=answer.reason)

    async def _secret_blocked(self, request: control.SecretBlocked) -> control.SecretAnswer:
        scope = self._turns.scope_for(request.session_token)
        report = SecretReport(cwd=request.cwd, file=request.file, line=request.line, rule=request.rule)
        answer = await gate.report_secret_block(self._deps, scope, report)
        return control.SecretAnswer(ok=answer.ok, enforce=answer.enforce)
