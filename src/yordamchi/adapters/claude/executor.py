"""`ClaudeAgentRuntime`: the `AgentRuntime` port on `claude-agent-sdk` 0.2.173 (SPEC §5.1–5.3, §3.2.2, §3.2.5).

One `ClaudeSDKClient` streaming session per project session, spawned from the installed `~/.local/bin/claude`. The
moving parts each live in their own module so they are tested without a process:
- `options.py` builds the spawn options and environment (SPEC §5.3);
- `guard.py` bridges `PreToolUse` / `can_use_tool` to `SessionSpec.guard` and holds questions;
- `stream.py` turns the raw stream into events and decides when a turn is over;
- `control.py` is the only code touching the SDK's private surface;
- `proctree.py` is the interrupt backstop's kill list.

Delivery (docs/SPIKES.md S1, S2):
- `send(mode="next")` writes the user message with `priority: "next"`, explicitly: the CLI reads it once the running
  tools finish, inside the same turn. `"now"` is never used (it backgrounds the running shell and splits the turn).
  `new_turn` uses `"later"`: a separate turn, even if one is still finishing.
- A redirect is `interrupt()` followed by `send(new_turn)`.
- Every message carries the session's current context (`spec.context_provider`, SPEC §5.4): the `UserPromptSubmit`
  hook adds a short one; a long one opens the message itself (`guard.message_with_context`).
- `interrupt()` closes the guard (deny-all), sends the interrupt with `cancel_queued` when the CLI supports it, stops
  background tasks, then waits up to `timeout` for the interrupted result. If none arrives (S2: the CLI's own SIGKILL
  escalation lands at ~1.5 s), the backstop TERMs and then KILLs the CLI and its tools' process groups, never the bot's
  own group; the session then ends with `RuntimeFailed(process_died, retryable=True)`, so the caller resumes it.

`SessionReady` is emitted once, from the first `system/init`; the CLI sends that at the start of the first turn, not
at connect, so it arrives after the first `send()`. A failed connect (missing transcript, missing CLI) returns a
handle whose events are `RuntimeFailed` then `SessionClosed`.

Logs: session id, kind, codes, counts, durations, cost. Never prompts, answers, tool input or model output.
"""

import asyncio
import base64
import contextlib
import dataclasses
import json
import os
import signal
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError

from yordamchi.adapters.claude import control, proctree
from yordamchi.adapters.claude.guard import GuardBridge
from yordamchi.adapters.claude.options import Launch, build_options, session_env
from yordamchi.adapters.claude.stream import StreamTranslator
from yordamchi.core import aio, proc
from yordamchi.core.errors import CallTimedOut, ConfigInvalid, ProviderUnavailable
from yordamchi.core.log import get_log
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    AgentRuntime,
    DeliveryMode,
    RuntimeFailed,
    RuntimeStatus,
    SessionClosed,
    SessionHandle,
    SessionSpec,
    TurnResult,
)

log = get_log(__name__)

EXECUTOR: Final = "claude"
PROVIDER: Final = "claude"
PRIORITY: Final[Mapping[DeliveryMode, str]] = {"next": "next", "new_turn": "later"}
CONTROL_TIMEOUT_S: Final = 2.0  # the interrupt receipt answers in 2–9 ms (S2)
STOP_TASK_TIMEOUT_S: Final = 1.0
KILL_GRACE_S: Final = 0.5
PS_TIMEOUT_S: Final = 1.0
IMAGE_TYPES: Final = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

type ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


@dataclass(eq=False)
class _Session:
    handle: SessionHandle
    spec: SessionSpec
    translator: StreamTranslator
    queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    turn_done: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    client: ClaudeSDKClient | None = None
    bridge: GuardBridge | None = None
    cli_pid: int | None = None
    pump: asyncio.Task[None] | None = None
    closing: bool = False
    failed: bool = False
    closed_emitted: bool = False

    def emit(self, event: AgentEvent) -> None:
        if self.closed_emitted:
            return
        if isinstance(event, SessionClosed):
            self.closed_emitted = True
        elif isinstance(event, RuntimeFailed):
            self.failed = True
        self.queue.put_nowait(event)


async def _one(message: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield message


def _version(text: str) -> str | None:
    first = text.strip().split(maxsplit=1)
    return first[0] if first and first[0][:1].isdigit() else None


class ClaudeAgentRuntime:
    """Bootstrap values only (CONVENTIONS §4): binary, child PATH, githooks dir. Behaviour arrives per call."""

    def __init__(
        self,
        *,
        cli_path: Path,
        search_path: str,
        githooks_dir: Path | None = None,
        base_env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        sdk_servers: Mapping[str, Any] | None = None,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self._cli_path = cli_path
        self._search_path = search_path
        self._githooks_dir = githooks_dir
        self._base_env = base_env
        self._factory: ClientFactory = client_factory or (lambda options: ClaudeSDKClient(options=options))
        self._sdk_servers = dict(sdk_servers or {})
        self._kill_grace_s = kill_grace_s
        self._sessions: dict[str, _Session] = {}

    # --- probe --------------------------------------------------------------------------------------------------
    async def probe(self, *, timeout: float) -> RuntimeStatus:
        """`claude --version` and `claude auth status` (JSON): installed, version, `authMethod: "claude.ai"`."""
        env = proc.clean_env(path=self._search_path, base=self._base_env)
        cli = str(self._cli_path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            version = await proc.run([cli, "--version"], env=env, timeout=timeout / 2)
            status = await proc.run([cli, "auth", "status"], env=env, timeout=max(0.1, deadline - loop.time()))
        except (FileNotFoundError, PermissionError):
            return RuntimeStatus(installed=False, version=None, logged_in=False, auth_method=None, missing=("claude",))
        except TimeoutError:
            raise CallTimedOut(operation="claude_probe") from None
        auth = _auth_status(status.stdout)
        logged_in = auth.get("loggedIn") is True
        method = auth.get("authMethod")
        auth_method = str(method) if isinstance(method, str) else None
        result = RuntimeStatus(
            installed=version.returncode == 0,
            version=_version(version.stdout.decode("utf-8", errors="replace")),
            logged_in=logged_in,
            auth_method=auth_method,
            missing=() if logged_in else ("login",),
        )
        log.info(
            "claude_probed",
            version=result.version,
            logged_in=logged_in,
            auth_method=auth_method,
            duration_ms=version.duration_ms + status.duration_ms,
        )
        return result

    # --- open ---------------------------------------------------------------------------------------------------
    async def open(self, spec: SessionSpec, *, resume_id: str | None, timeout: float) -> SessionHandle:
        launch = Launch(session_id=resume_id or str(uuid.uuid4()), resume=resume_id is not None)
        handle = SessionHandle(session_id=launch.session_id, executor=EXECUTOR, kind=spec.kind)
        translator = StreamTranslator(session_id=launch.session_id, expected_tools=spec.expected_tools)
        session = _Session(handle=handle, spec=spec, translator=translator)
        bridge = GuardBridge(spec, session_id=launch.session_id, emit=session.emit, blocked=translator.blocked_reason)
        session.bridge = bridge
        env = session_env(spec, search_path=self._search_path, githooks_dir=self._githooks_dir, base=self._base_env)
        options = build_options(
            spec,
            cli_path=self._cli_path,
            env=env,
            launch=launch,
            hooks=bridge.hooks(),
            can_use_tool=bridge.can_use_tool,
            sdk_servers=self._sdk_servers,
            stderr=_drop_stderr,
        )
        client = self._factory(options)
        self._sessions[launch.session_id] = session
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                await control.connect(client)
        except TimeoutError:
            log.warning("claude_open_timed_out", session_id=launch.session_id, kind=spec.kind, timeout_s=timeout)
            session.client = client
            session.cli_pid = control.cli_pid(client)
            await self._stop_process(session, disconnect_timeout=KILL_GRACE_S)
            session.emit(SessionClosed(reason="open_timed_out"))
            session.ended.set()
            raise CallTimedOut(operation="claude_open") from None
        except (ClaudeSDKError, OSError, control.PrivateApiMissing) as error:
            code, retryable = control.failure(error)
            log.warning("claude_open_failed", session_id=launch.session_id, kind=spec.kind, code=code)
            session.emit(RuntimeFailed(code=code, retryable=retryable))
            session.emit(SessionClosed(reason=code))
            session.ended.set()
            return handle
        session.client = client
        session.cli_pid = control.cli_pid(client)
        session.pump = aio.spawn(f"claude_session_{launch.session_id[:8]}", self._pump(session, client))
        log.info(
            "claude_session_opened",
            session_id=launch.session_id,
            kind=spec.kind,
            resumed=launch.resume,
            cli_pid=session.cli_pid,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return handle

    async def _pump(self, session: _Session, client: ClaudeSDKClient) -> None:
        """Raw stream → translator → the session's queue. Ends with exactly one `SessionClosed`."""
        reason = "closed"
        clean = False
        try:
            async with contextlib.aclosing(control.raw_messages(client)) as stream:
                async for message in stream:
                    for event in session.translator.feed(message):
                        session.emit(event)
                        self._observe(session, event)
                    if session.translator.refused and not session.closing:
                        reason = "tools_mismatch"
                        break
            clean = True
        except (ClaudeSDKError, OSError, control.PrivateApiMissing, control.StreamFailed) as error:
            code, retryable = control.failure(error)
            reason = code
            if not session.closing:
                log.warning("claude_session_failed", session_id=session.handle.session_id, code=code)
                session.emit(RuntimeFailed(code=code, retryable=retryable))
        finally:
            if not clean and not session.failed and not session.closing and reason == "closed":
                session.emit(RuntimeFailed(code="process_died", retryable=True))
                reason = "process_died"
            if session.bridge is not None:
                session.bridge.release_questions()
            session.turn_done.set()
            session.emit(SessionClosed(reason=reason))
            session.ended.set()
        if reason == "tools_mismatch":
            await self._stop_process(session, disconnect_timeout=KILL_GRACE_S * 4)

    def _observe(self, session: _Session, event: AgentEvent) -> None:
        if isinstance(event, TurnResult):
            session.turn_done.set()
            log.info(
                "claude_turn_finished",
                session_id=session.handle.session_id,
                subtype=event.subtype,
                interrupted=event.interrupted,
                background=event.background,
                num_turns=event.num_turns,
                duration_ms=event.duration_ms,
                cost_usd_micros=event.cost_usd_micros,
                denials=len(event.permission_denials),
            )
        elif isinstance(event, RuntimeFailed):
            log.warning("claude_runtime_failed", session_id=session.handle.session_id, code=event.code)

    # --- send / answer ------------------------------------------------------------------------------------------
    def _live(self, h: SessionHandle) -> tuple[_Session, ClaudeSDKClient]:
        session = self._sessions.get(h.session_id)
        if session is None or session.client is None or session.ended.is_set() or session.closing:
            raise ProviderUnavailable(provider=PROVIDER, retryable=False)
        return session, session.client

    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: DeliveryMode, timeout: float) -> None:
        session, client = self._live(h)
        if session.bridge is not None:  # a long per-turn context opens the message; a short one rides the hook
            msg = dataclasses.replace(msg, text=session.bridge.message_text(msg.text))
        content = await _content(msg)
        command_uuid = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "type": "user",
            "uuid": command_uuid,
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "default",
            "priority": PRIORITY[mode],
        }
        session.translator.sent(command_uuid)
        session.turn_done.clear()
        try:
            async with asyncio.timeout(timeout):
                await client.query(_one(payload))
        except TimeoutError:
            raise CallTimedOut(operation="claude_send") from None
        except ClaudeSDKError as error:
            raise ProviderUnavailable(provider=PROVIDER, retryable=False) from error
        log.info(
            "claude_input_sent", session_id=h.session_id, mode=mode, images=len(msg.images), command_uuid=command_uuid
        )

    async def answer(self, h: SessionHandle, question_id: str, answers: Mapping[str, str], *, timeout: float) -> bool:
        del timeout  # resolving a future does not wait
        session = self._sessions.get(h.session_id)
        if session is None or session.bridge is None:
            return False
        return session.bridge.answer(question_id, answers)

    # --- interrupt ----------------------------------------------------------------------------------------------
    async def interrupt(self, h: SessionHandle, *, timeout: float) -> None:
        session = self._sessions.get(h.session_id)
        if session is None or session.client is None or session.ended.is_set():
            return
        client = session.client
        translator = session.translator
        running = translator.stop_requested()  # the guard denies every call from here on
        started = time.monotonic()
        receipt = control.InterruptReceipt()
        try:
            async with asyncio.timeout(min(timeout, CONTROL_TIMEOUT_S)):
                receipt = await control.interrupt(
                    client, cancel_queued=translator.supports_cancel_queued, timeout=CONTROL_TIMEOUT_S * 2
                )
        except (TimeoutError, control.ControlFailed) as error:
            log.warning("claude_interrupt_failed", session_id=h.session_id, error_type=type(error).__name__)
        translator.cancelled(receipt.cancelled)
        for task_id in translator.background_task_ids():
            try:
                async with asyncio.timeout(STOP_TASK_TIMEOUT_S):
                    await control.stop_task(client, task_id)
            except (TimeoutError, control.ControlFailed) as error:
                log.warning("claude_stop_task_failed", session_id=h.session_id, error_type=type(error).__name__)
        killed = False
        if running:
            remaining = max(0.0, timeout - (time.monotonic() - started))
            try:
                async with asyncio.timeout(remaining):
                    await session.turn_done.wait()
            except TimeoutError:
                killed = True
                await self._backstop(session)
        log.info(
            "claude_interrupted",
            session_id=h.session_id,
            running=running,
            receipt=receipt.has_receipt,
            cancelled=len(receipt.cancelled),
            killed=killed,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _backstop(self, session: _Session) -> None:
        """TERM, grace, KILL: the CLI by pid and its tools' process groups, never the bot's own group (S2)."""
        pid = session.cli_pid
        if pid is None:
            log.error("claude_backstop_skipped", session_id=session.handle.session_id, reason="no_pid")
            return
        env = proc.clean_env(path=self._search_path, base=self._base_env)
        try:
            rows = await proctree.list_processes(env=env, timeout=PS_TIMEOUT_S)
        except (TimeoutError, OSError):
            rows = []
        own_pgid, own_pid = os.getpgrp(), os.getpid()
        plan = proctree.kill_plan(rows, root=pid, own_pgid=own_pgid, own_pid=own_pid)
        hit = proctree.send(plan, signal.SIGTERM, own_pgid=own_pgid)
        try:
            async with asyncio.timeout(self._kill_grace_s):
                await session.ended.wait()
        except TimeoutError:
            pass
        proctree.send(plan, signal.SIGKILL, own_pgid=own_pgid)
        log.warning(
            "claude_backstop_killed",
            session_id=session.handle.session_id,
            groups=len(plan.groups),
            pids=len(plan.pids),
            signalled=hit,
        )

    async def _stop_process(self, session: _Session, *, disconnect_timeout: float) -> None:
        session.closing = True
        client = session.client
        if client is None:
            return
        try:
            async with asyncio.timeout(disconnect_timeout):
                await client.disconnect()
        except (TimeoutError, ClaudeSDKError, OSError) as error:
            log.warning("claude_disconnect_failed", session_id=session.handle.session_id, error=type(error).__name__)
            await self._backstop(session)

    # --- model / close / events ---------------------------------------------------------------------------------
    async def set_model(self, h: SessionHandle, model: str, *, timeout: float) -> None:
        session, client = self._live(h)
        try:
            async with asyncio.timeout(timeout):
                await control.set_model(client, model)
        except TimeoutError:
            raise CallTimedOut(operation="claude_set_model") from None
        except control.ControlFailed as error:
            raise ProviderUnavailable(provider=PROVIDER, retryable=False) from error
        log.info("claude_model_set", session_id=session.handle.session_id, model=model)

    async def close(self, h: SessionHandle, *, timeout: float) -> None:
        session = self._sessions.get(h.session_id)
        if session is None:
            return
        if session.bridge is not None:
            session.bridge.release_questions()
        if session.client is not None and not session.ended.is_set():
            await self._stop_process(session, disconnect_timeout=timeout)
        try:
            async with asyncio.timeout(self._kill_grace_s * 2):
                await session.ended.wait()
        except TimeoutError:
            pass
        session.closing = True
        session.emit(SessionClosed(reason="closed"))  # no-op when the pump already ended the session
        session.ended.set()
        log.info("claude_session_closed", session_id=h.session_id)

    async def events(self, h: SessionHandle, *, idle_timeout: float) -> AsyncIterator[AgentEvent]:
        session = self._sessions[h.session_id]
        while True:
            try:
                async with asyncio.timeout(idle_timeout):
                    event = await session.queue.get()
            except TimeoutError:
                raise CallTimedOut(operation="agent_events") from None
            yield event
            if isinstance(event, SessionClosed):
                if session.ended.is_set():
                    self._sessions.pop(h.session_id, None)
                return


def _drop_stderr(line: str) -> None:
    """The CLI's stderr may echo content; it is never logged. Errors arrive typed through the stream."""
    del line


def _auth_status(stdout: bytes) -> Mapping[str, Any]:
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


async def _content(msg: AgentInput) -> str | list[dict[str, Any]]:
    """Plain text, or text plus base64 image blocks (at most five, `AgentInput` enforces it)."""
    if not msg.images:
        return msg.text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": msg.text}]
    for path in msg.images:
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise ConfigInvalid(key="images", problem="unsupported_image_type")
        data = await asyncio.to_thread(path.read_bytes)
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
            }
        )
    return blocks


def _conforms(runtime: ClaudeAgentRuntime) -> AgentRuntime:
    """mypy checks the port here (the fakes do the same with a module-level binding)."""
    return runtime
