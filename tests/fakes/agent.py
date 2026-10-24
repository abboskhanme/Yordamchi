"""`FakeAgentRuntime`: replays scripted turns and records sends, interrupts, answers and guard calls (SPEC §8).

    agents.script_turn(ToolStep("Bash", {"command": "git push"}), TurnResult(...))

On `send`, the next script runs: a `ToolStep` goes through `SessionSpec.guard` exactly as the real adapter's
PreToolUse hook does — allowed → `Progress(tool_start)`, denied → `ToolDenied` — and every other item is emitted
as-is. A `QuestionAsked` stays answerable until `answer()` takes it.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field

from yordamchi.core.errors import CallTimedOut
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    AgentRuntime,
    DeliveryMode,
    GuardVerdict,
    Progress,
    QuestionAsked,
    RuntimeFailed,
    RuntimeStatus,
    SessionClosed,
    SessionHandle,
    SessionReady,
    SessionSpec,
    ToolCall,
    ToolDenied,
    TurnResult,
)

EXECUTOR = "fake"
READY = RuntimeStatus(installed=True, version="2.1.309", logged_in=True, auth_method="claude.ai")


@dataclass(frozen=True, slots=True)
class ToolStep:
    """The agent attempts one tool call; the fake asks the session's guard about it."""

    tool_name: str
    tool_input: Mapping[str, JsonValue] = field(default_factory=dict)
    tool_use_id: str = "toolu_fake"
    agent_id: str | None = None


type ScriptItem = AgentEvent | ToolStep


def done_result(text: str = "Natija: bajarildi.\nTestlar: o'tdi (1)") -> TurnResult:
    return TurnResult(
        text=text,
        subtype="success",
        is_error=False,
        stop_reason="end_turn",
        cost_usd_micros=0,
        model_usage={},
        num_turns=1,
        duration_ms=1,
    )


@dataclass
class _Session:
    spec: SessionSpec
    queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    pending_questions: set[str] = field(default_factory=set)


class FakeAgentRuntime:
    def __init__(self, *, status: RuntimeStatus = READY, missing_sessions: Iterable[str] = ()) -> None:
        self.status = status
        self.missing_sessions = set(missing_sessions)
        self._turns: deque[tuple[ScriptItem, ...]] = deque()
        self._sessions: dict[str, _Session] = {}
        self._opened = 0
        self.opened: list[tuple[SessionSpec, str | None]] = []
        self.sends: list[tuple[str, AgentInput, DeliveryMode]] = []
        self.answers: list[tuple[str, str, Mapping[str, str]]] = []
        self.interrupts: list[str] = []
        self.models: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.guard_calls: list[tuple[ToolCall, GuardVerdict]] = []

    def script_turn(self, *items: ScriptItem) -> None:
        """Queue what the next `send` produces (a turn without a script just succeeds)."""
        self._turns.append(items)

    def spec_of(self, h: SessionHandle) -> SessionSpec:
        return self._sessions[h.session_id].spec

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return self.status

    async def open(self, spec: SessionSpec, *, resume_id: str | None, timeout: float) -> SessionHandle:
        self._opened += 1
        session_id = resume_id or f"fake-session-{self._opened}"
        session = _Session(spec)
        self._sessions[session_id] = session
        self.opened.append((spec, resume_id))
        if resume_id is not None and resume_id in self.missing_sessions:
            session.queue.put_nowait(RuntimeFailed(code="session_not_found", retryable=False))
        else:
            session.queue.put_nowait(
                SessionReady(
                    session_id=session_id,
                    tools=spec.tools or (),
                    mcp_servers=tuple(spec.mcp_servers),
                    model=spec.model,
                    cli_version=self.status.version or "",
                )
            )
        return SessionHandle(session_id=session_id, executor=EXECUTOR, kind=spec.kind)

    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: DeliveryMode, timeout: float) -> None:
        session = self._sessions[h.session_id]
        self.sends.append((h.session_id, msg, mode))
        items = self._turns.popleft() if self._turns else (done_result(),)
        for item in items:
            await session.queue.put(await self._play(h, session, item))

    async def _play(self, h: SessionHandle, session: _Session, item: ScriptItem) -> AgentEvent:
        if isinstance(item, ToolStep):
            call = ToolCall(
                tool_name=item.tool_name,
                tool_input=item.tool_input,
                tool_use_id=item.tool_use_id,
                agent_id=item.agent_id,
                session_id=h.session_id,
            )
            verdict = await session.spec.guard(call)
            self.guard_calls.append((call, verdict))
            if verdict.allow:
                return Progress(kind="tool_start", tool_name=item.tool_name, summary=item.tool_name)
            return ToolDenied(tool_name=item.tool_name, reason_code=verdict.reason_code or "denied")
        if isinstance(item, QuestionAsked):
            session.pending_questions.add(item.question_id)
        return item

    async def answer(self, h: SessionHandle, question_id: str, answers: Mapping[str, str], *, timeout: float) -> bool:
        session = self._sessions[h.session_id]
        if question_id not in session.pending_questions:
            return False
        session.pending_questions.discard(question_id)
        self.answers.append((h.session_id, question_id, dict(answers)))
        return True

    async def interrupt(self, h: SessionHandle, *, timeout: float) -> None:
        self.interrupts.append(h.session_id)

    async def set_model(self, h: SessionHandle, model: str, *, timeout: float) -> None:
        self.models.append((h.session_id, model))

    async def close(self, h: SessionHandle, *, timeout: float) -> None:
        self.closed.append(h.session_id)
        await self._sessions[h.session_id].queue.put(SessionClosed(reason="closed"))

    async def events(self, h: SessionHandle, *, idle_timeout: float) -> AsyncIterator[AgentEvent]:
        queue = self._sessions[h.session_id].queue
        while True:
            try:
                async with asyncio.timeout(idle_timeout):
                    event = await queue.get()
            except TimeoutError:
                raise CallTimedOut(operation="agent_events") from None
            yield event
            if isinstance(event, SessionClosed):
                return


_: AgentRuntime = FakeAgentRuntime()
