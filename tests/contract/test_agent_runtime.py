import uuid
from pathlib import Path

import pytest

from tests.fakes.agent import FakeAgentRuntime, ToolStep
from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH, scrub_live_env
from yordamchi.adapters.claude.executor import ClaudeAgentRuntime
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
# A probe runs a fake `claude` script written fresh into tmp_path; macOS scans a new executable on its first run
# (XProtect), which took over 5 s while a VM loaded the Mac (2026-10-25). The bound is the harness's, not a latency
# claim: the production probe timeout is a setting.
PROBE_TIMEOUT = 30.0
LIVE_TIMEOUT = 90.0


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
        status = await runtime.probe(timeout=PROBE_TIMEOUT)
        assert status.installed
        assert status.auth_method == "claude.ai"

    async def test_a_session_starts_ready_and_a_turn_ends_with_a_result(
        self, runtime: AgentRuntime, tmp_path: Path
    ) -> None:
        # SessionReady comes first in the stream; the Claude CLI sends its `init` with the first turn, so the
        # contract does not require it before the first send.
        h = await runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
        await runtime.send(h, AgentInput(text="Reply with: ok"), mode="new_turn", timeout=TIMEOUT)
        turn = await _until(runtime, h, TurnResult)
        assert isinstance(turn[0], SessionReady)
        assert turn[0].session_id == h.session_id
        assert isinstance(turn[-1], TurnResult)
        assert turn[-1].interrupted is False
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


@pytest.mark.live
@pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
class TestClaudeAgentRuntime(AgentRuntimeContract):
    """The same contract on the real `~/.local/bin/claude` (haiku); `pytest -m live tests/contract`."""

    @pytest.fixture
    def runtime(self, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
        scrub_live_env(monkeypatch)
        return ClaudeAgentRuntime(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH)

    @pytest.fixture(autouse=True)
    def _live_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real session takes seconds to spawn and answer; the contract's 5 s bound is for the fake."""
        monkeypatch.setattr(f"{__name__}.TIMEOUT", LIVE_TIMEOUT)

    async def test_a_missing_transcript_fails_the_resume(self, runtime: AgentRuntime, tmp_path: Path) -> None:
        h = await runtime.open(_spec(tmp_path), resume_id=str(uuid.uuid4()), timeout=LIVE_TIMEOUT)
        events = await _until(runtime, h, RuntimeFailed)
        assert events == [RuntimeFailed(code="session_not_found", retryable=False)]
