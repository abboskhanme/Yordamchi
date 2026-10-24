from pathlib import Path

import pytest

from tests.fakes.agent import FakeAgentRuntime, ToolStep
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    AgentRuntime,
    GuardVerdict,
    Progress,
    QuestionAsked,
    RuntimeFailed,
    SessionClosed,
    SessionHandle,
    SessionReady,
    SessionSpec,
    ToolCall,
    ToolDenied,
    TurnResult,
)

TIMEOUT = 5.0


async def _allow_all(call: ToolCall) -> GuardVerdict:
    del call
    return GuardVerdict(allow=True)


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(kind="smoke", cwd=tmp_path, model="haiku", guard=_allow_all)


async def _until(runtime: AgentRuntime, h: SessionHandle, kind: type[AgentEvent]) -> list[AgentEvent]:
    seen: list[AgentEvent] = []
    async for event in runtime.events(h, idle_timeout=TIMEOUT):
        seen.append(event)
        if isinstance(event, kind):
            break
    return seen


class AgentRuntimeContract:
    @pytest.fixture
    def runtime(self) -> AgentRuntime:
        raise NotImplementedError

    async def test_probe_reports_install_and_login(self, runtime: AgentRuntime) -> None:
        status = await runtime.probe(timeout=TIMEOUT)
        assert status.installed
        assert status.auth_method == "claude.ai"

    async def test_a_session_starts_ready_and_a_turn_ends_with_a_result(
        self, runtime: AgentRuntime, tmp_path: Path
    ) -> None:
        h = await runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
        first = await _until(runtime, h, SessionReady)
        assert isinstance(first[0], SessionReady)
        assert first[0].session_id == h.session_id
        await runtime.send(h, AgentInput(text="Reply with: ok"), mode="new_turn", timeout=TIMEOUT)
        turn = await _until(runtime, h, TurnResult)
        assert isinstance(turn[-1], TurnResult)
        await runtime.close(h, timeout=TIMEOUT)
        assert isinstance((await _until(runtime, h, SessionClosed))[-1], SessionClosed)

    async def test_answering_an_unknown_question_reports_false(self, runtime: AgentRuntime, tmp_path: Path) -> None:
        h = await runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
        assert await runtime.answer(h, "no-such-question", {"q": "a"}, timeout=TIMEOUT) is False
        await runtime.close(h, timeout=TIMEOUT)

    def test_an_input_carries_at_most_five_images(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at most"):
            AgentInput(text="x", images=tuple(tmp_path / f"{n}.png" for n in range(6)))


class TestFakeAgentRuntime(AgentRuntimeContract):
    @pytest.fixture
    def runtime(self) -> AgentRuntime:
        return FakeAgentRuntime()

    async def test_tool_steps_go_through_the_session_guard(self, tmp_path: Path) -> None:
        async def deny_push(call: ToolCall) -> GuardVerdict:
            if call.tool_input.get("command") == "git push --force":
                return GuardVerdict(allow=False, message="BLOCKED", reason_code="force_push")
            return GuardVerdict(allow=True)

        runtime = FakeAgentRuntime()
        runtime.script_turn(
            ToolStep("Bash", {"command": "git status"}),
            ToolStep("Bash", {"command": "git push --force"}),
            TurnResult("Natija: x", "success", False, "end_turn", 0, {}, 1, 1),
        )
        spec = SessionSpec(kind="coding", cwd=tmp_path, model="opus", guard=deny_push)
        h = await runtime.open(spec, resume_id=None, timeout=TIMEOUT)
        await runtime.send(h, AgentInput(text="push"), mode="new_turn", timeout=TIMEOUT)
        events = await _until(runtime, h, TurnResult)
        assert [type(event) for event in events] == [SessionReady, Progress, ToolDenied, TurnResult]
        assert [verdict.allow for _, verdict in runtime.guard_calls] == [True, False]
        assert runtime.sends[0][2] == "new_turn"

    async def test_a_pending_question_is_answerable_once(self, tmp_path: Path) -> None:
        runtime = FakeAgentRuntime()
        runtime.script_turn(QuestionAsked("q1", ({"question": "Qaysi baza?"},)))
        h = await runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
        await runtime.send(h, AgentInput(text="x"), mode="new_turn", timeout=TIMEOUT)
        await _until(runtime, h, QuestionAsked)
        assert await runtime.answer(h, "q1", {"Qaysi baza?": "Postgres"}, timeout=TIMEOUT) is True
        assert await runtime.answer(h, "q1", {"Qaysi baza?": "Postgres"}, timeout=TIMEOUT) is False

    async def test_a_missing_transcript_fails_the_resume(self, tmp_path: Path) -> None:
        runtime = FakeAgentRuntime(missing_sessions={"gone"})
        h = await runtime.open(_spec(tmp_path), resume_id="gone", timeout=TIMEOUT)
        events = await _until(runtime, h, RuntimeFailed)
        assert events == [RuntimeFailed(code="session_not_found", retryable=False)]
