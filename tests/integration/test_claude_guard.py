"""The guard bridge: `PreToolUse` and `can_use_tool` → `SessionSpec.guard` (SPEC §5.2, §3.2.2–3.2.3; docs/SPIKES.md S3).

Fail closed is the point: every error, odd return, timeout or stop is a deny, and the hook never raises into the
SDK. Hook inputs come from the recorded sessions in `tests/fixtures/claude/streams/`. The `live` tests at the end
drive the real CLI (haiku).
"""

import asyncio
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import HookInput, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH, FakeClient, records, scrub_live_env
from yordamchi.adapters.claude import control
from yordamchi.adapters.claude.executor import ClaudeAgentRuntime
from yordamchi.adapters.claude.guard import (
    HOOK_MARGIN_S,
    REASON_ERROR,
    REASON_PARKED,
    REASON_SUBAGENT_QUESTION,
    REASON_TIMEOUT,
    GuardBridge,
)
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    GuardFn,
    GuardVerdict,
    QuestionAsked,
    SessionHandle,
    SessionSpec,
    ToolCall,
    ToolDenied,
    TurnResult,
)

CONTEXT: Any = {"signal": None}


async def _allow(call: ToolCall) -> GuardVerdict:
    del call
    return GuardVerdict(allow=True)


class Bridge:
    """A `GuardBridge` with its emitted events and the calls its guard saw."""

    def __init__(self, guard: GuardFn = _allow, *, blocked: str | None = None, **spec: object) -> None:
        self.events: list[AgentEvent] = []
        self.calls: list[ToolCall] = []
        self.blocked = blocked

        async def recording(call: ToolCall) -> GuardVerdict:
            self.calls.append(call)
            return await guard(call)

        values: dict[str, Any] = {"kind": "coding", "cwd": Path("/tmp/p"), "model": "opus", "guard": recording}
        values.update(spec)
        self.spec = SessionSpec(**values)
        self.bridge = GuardBridge(self.spec, session_id="sess-1", emit=self.events.append, blocked=lambda: self.blocked)


def _hook_input(name: str, tool: str, *, agent: bool = False) -> HookInput:
    for record in records(name):
        if record["kind"] == "hook" and record["data"]["input"].get("tool_name") == tool:
            data = record["data"]["input"]
            if bool(data.get("agent_id")) == agent:
                return cast("HookInput", dict(data))
    raise AssertionError(f"no {tool} hook input in {name}")


def _specific(output: object) -> object:
    return output.get("hookSpecificOutput") if isinstance(output, dict) else None


def _decision(output: object) -> object:
    specific = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    return specific.get("permissionDecision") if isinstance(specific, dict) else None


# --- PreToolUse ---------------------------------------------------------------------------------------------------
async def test_an_allowed_call_lets_the_mode_decide() -> None:
    probe = Bridge(permission_mode="bypassPermissions")
    data = _hook_input("hook_deny", "Bash")
    raw = dict(data)

    output = await probe.bridge.pre_tool_use(data, str(raw["tool_use_id"]), CONTEXT)

    assert output == {}
    (call,) = probe.calls
    assert call == ToolCall(
        tool_name="Bash",
        tool_input=cast("dict[str, Any]", raw["tool_input"]),
        tool_use_id="toolu_01C1CFhURzWjCzeNA7JNURCr",
        agent_id=None,
        session_id=str(raw["session_id"]),
    )
    assert probe.events == []


async def test_dont_ask_sessions_get_an_explicit_allow_except_for_questions() -> None:
    probe = Bridge(permission_mode="dontAsk")
    bash = _hook_input("hook_deny", "Bash")
    ask = _hook_input("question", "AskUserQuestion")

    assert _decision(await probe.bridge.pre_tool_use(bash, None, CONTEXT)) == "allow"
    assert await probe.bridge.pre_tool_use(ask, None, CONTEXT) == {}  # its answers come from can_use_tool


async def test_a_deny_returns_the_gate_message_and_emits_tool_denied_from_the_hook() -> None:
    async def deny(call: ToolCall) -> GuardVerdict:
        del call
        return GuardVerdict(allow=False, message="BLOCKED: owner decision #17 has been asked.", reason_code="ask")

    probe = Bridge(deny)
    output = await probe.bridge.pre_tool_use(_hook_input("hook_deny", "Bash"), None, CONTEXT)

    assert _specific(output) == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "BLOCKED: owner decision #17 has been asked.",
    }
    assert probe.events == [ToolDenied(tool_name="Bash", reason_code="ask")]


async def test_subagent_calls_reach_the_guard_with_their_agent_id() -> None:
    probe = Bridge()
    data = _hook_input("background_agent", "Bash", agent=True)

    await probe.bridge.pre_tool_use(data, None, CONTEXT)

    assert probe.calls[0].agent_id == "aef95892c7f83ebc5"


async def _boom(call: ToolCall) -> GuardVerdict:
    raise RuntimeError(call.tool_name)


async def _not_a_verdict(call: ToolCall) -> GuardVerdict:
    del call
    return True  # type: ignore[return-value]


async def _forever(call: ToolCall) -> GuardVerdict:
    del call
    await asyncio.Event().wait()  # a guard that waits for the owner: forbidden, and cut off
    return GuardVerdict(allow=True)


@pytest.mark.parametrize(
    ("guard", "reason"),
    [(_boom, REASON_ERROR), (_not_a_verdict, REASON_ERROR), (_forever, REASON_TIMEOUT)],
    ids=["raises", "not_a_verdict", "never_answers"],
)
async def test_the_guard_fails_closed(guard: GuardFn, reason: str) -> None:
    probe = Bridge(guard, hook_timeout_s=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()

    output = await probe.bridge.pre_tool_use(_hook_input("hook_deny", "Bash"), None, CONTEXT)

    assert _decision(output) == "deny"
    assert probe.events == [ToolDenied(tool_name="Bash", reason_code=reason)]
    assert loop.time() - started < 1.0


def test_the_cli_hook_timeout_is_later_than_ours() -> None:
    """A PreToolUse the CLI times out does not block (STACK §4.1): our own deny must land first."""
    probe = Bridge(hook_timeout_s=15.0)
    (matcher,) = probe.bridge.hooks()["PreToolUse"]
    assert matcher.timeout == 15.0 + HOOK_MARGIN_S
    assert matcher.matcher is None
    assert "UserPromptSubmit" not in probe.bridge.hooks()


@pytest.mark.parametrize("data", [None, {"tool_name": "Bash", "tool_input": "rm -rf /"}, {}])
async def test_malformed_hook_input_never_raises(data: object) -> None:
    probe = Bridge()
    output = await probe.bridge.pre_tool_use(data, None, CONTEXT)  # type: ignore[arg-type]
    assert output == {}
    assert probe.calls[0].tool_input == {}


@pytest.mark.parametrize("reason", ["stopping", "tools_unverified", "tools_mismatch"])
async def test_a_blocked_session_denies_every_call_without_asking_the_guard(reason: str) -> None:
    probe = Bridge(blocked=reason)

    output = await probe.bridge.pre_tool_use(_hook_input("hook_deny", "Bash"), None, CONTEXT)

    assert _decision(output) == "deny"
    assert probe.calls == []
    assert probe.events == [ToolDenied(tool_name="Bash", reason_code=reason)]


async def test_a_failing_event_sink_still_denies() -> None:
    def broken(event: AgentEvent) -> None:
        raise RuntimeError(type(event).__name__)

    spec = SessionSpec(kind="coding", cwd=Path("/tmp/p"), model="opus", guard=_boom)
    bridge = GuardBridge(spec, session_id="s", emit=broken, blocked=lambda: None)
    assert _decision(await bridge.pre_tool_use(_hook_input("hook_deny", "Bash"), None, CONTEXT)) == "deny"


# --- UserPromptSubmit -----------------------------------------------------------------------------------------------
async def test_every_turn_gets_the_current_context() -> None:
    probe = Bridge(context_provider=lambda: "Current instructions from the owner: answer in Uzbek.")
    data = next(r["data"]["input"] for r in records("simple") if r["kind"] == "hook")

    output = await probe.bridge.user_prompt_submit(data, None, CONTEXT)

    assert _specific(output) == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "Current instructions from the owner: answer in Uzbek.",
    }
    assert probe.bridge.hooks()["UserPromptSubmit"][0].timeout == 10


def _raise() -> str:
    raise ValueError("no snapshot")


@pytest.mark.parametrize("provider", [_raise, lambda: ""], ids=["raises", "empty"])
async def test_a_failing_or_empty_context_never_blocks_the_turn(provider: Callable[[], str]) -> None:
    probe = Bridge(context_provider=provider)
    assert await probe.bridge.user_prompt_submit({}, None, CONTEXT) == {}  # type: ignore[arg-type]


# --- can_use_tool: questions (UC-12) ------------------------------------------------------------------------------
def _question_input() -> tuple[dict[str, Any], ToolPermissionContext]:
    perm = next(r["data"] for r in records("question") if r["kind"] == "perm")
    return perm["input"], ToolPermissionContext(tool_use_id=perm["tool_use_id"], agent_id=perm["agent_id"])


async def _ask(probe: Bridge) -> tuple["asyncio.Task[Any]", QuestionAsked]:
    tool_input, context = _question_input()
    task = asyncio.get_running_loop().create_task(probe.bridge.can_use_tool("AskUserQuestion", tool_input, context))
    for _ in range(100):
        if probe.events:
            break
        await asyncio.sleep(0)
    asked = probe.events[0]
    assert isinstance(asked, QuestionAsked)
    return task, asked


async def test_a_question_becomes_question_asked_and_its_answer_round_trips() -> None:
    probe = Bridge()
    task, asked = await _ask(probe)

    assert asked.question_id == "toolu_014LZpSQNFaBDfVtgmu2vmbF"
    assert asked.questions[0]["options"] == [
        {"label": "Red", "description": "Choose red"},
        {"label": "Blue", "description": "Choose blue"},
    ]
    assert probe.bridge.pending_questions() == (asked.question_id,)
    assert probe.bridge.answer(asked.question_id, {"Which color do you prefer?": "Blue"}) is True
    result = await task

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {"Which color do you prefer?": "Blue"}
    assert result.updated_input["questions"] == _question_input()[0]["questions"]
    assert probe.bridge.answer(asked.question_id, {"x": "y"}) is False  # answered once
    assert probe.bridge.pending_questions() == ()
    assert probe.calls == []  # the PreToolUse hook already asked the guard about it


async def test_an_unanswered_question_is_parked_after_the_hold() -> None:
    probe = Bridge(question_hold_s=0.05, question_parked_message="PARKED: the owner will answer later.")
    task, asked = await _ask(probe)

    result = await task

    assert result == PermissionResultDeny(message="PARKED: the owner will answer later.")
    assert probe.events[-1] == ToolDenied(tool_name="AskUserQuestion", reason_code=REASON_PARKED)
    assert probe.bridge.answer(asked.question_id, {"q": "a"}) is False  # the caller then sends a message


async def test_closing_the_session_parks_held_questions() -> None:
    probe = Bridge(question_parked_message="PARKED")
    task, _ = await _ask(probe)

    probe.bridge.release_questions()

    assert await task == PermissionResultDeny(message="PARKED")


async def test_a_subagent_question_is_denied_with_a_message() -> None:
    probe = Bridge()
    tool_input, _ = _question_input()

    result = await probe.bridge.can_use_tool(
        "AskUserQuestion", tool_input, ToolPermissionContext(tool_use_id="t", agent_id="a1")
    )

    assert isinstance(result, PermissionResultDeny)
    assert "subagent" in result.message
    assert probe.events == [ToolDenied(tool_name="AskUserQuestion", reason_code=REASON_SUBAGENT_QUESTION)]


async def test_no_question_is_held_while_stopping() -> None:
    probe = Bridge(blocked="stopping")
    tool_input, context = _question_input()
    result = await probe.bridge.can_use_tool("AskUserQuestion", tool_input, context)
    assert isinstance(result, PermissionResultDeny)
    assert probe.bridge.pending_questions() == ()


# --- can_use_tool: fall-through calls go through the guard again --------------------------------------------------
async def test_a_fall_through_call_is_allowed_only_by_the_guard() -> None:
    async def only_ls(call: ToolCall) -> GuardVerdict:
        if call.tool_input.get("command") == "ls":
            return GuardVerdict(allow=True)
        return GuardVerdict(allow=False, message="FORBIDDEN", reason_code="forbidden")

    probe = Bridge(only_ls)
    context = ToolPermissionContext(tool_use_id="toolu_1", agent_id=None)

    allowed = await probe.bridge.can_use_tool("Bash", {"command": "ls"}, context)
    denied = await probe.bridge.can_use_tool("Bash", {"command": "rm -rf build"}, context)

    assert allowed == PermissionResultAllow(updated_input={"command": "ls"})
    assert denied == PermissionResultDeny(message="FORBIDDEN")
    assert probe.events == [ToolDenied(tool_name="Bash", reason_code="forbidden")]
    assert probe.calls[0].tool_use_id == "toolu_1"


async def test_a_fall_through_call_fails_closed() -> None:
    probe = Bridge(_boom)
    result = await probe.bridge.can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext(tool_use_id="t"))
    assert isinstance(result, PermissionResultDeny)


# --- the SDK warning (S3) ------------------------------------------------------------------------------------------
async def test_connect_filters_exactly_the_shadowed_warning() -> None:
    client = FakeClient(ClaudeAgentOptions())  # warns CanUseToolShadowedWarning, like the SDK under bypass
    await control.connect(client)  # type: ignore[arg-type]  # would fail: the suite turns warnings into errors
    await client.disconnect()

    class Other(FakeClient):
        async def connect(self) -> None:
            warnings.warn("something else", UserWarning, stacklevel=1)

    with pytest.warns(UserWarning, match="something else"):
        await control.connect(Other(ClaudeAgentOptions()))  # type: ignore[arg-type]


# --- live: the real CLI (haiku) --------------------------------------------------------------------------------
live = pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")


@pytest.fixture
def live_runtime(monkeypatch: pytest.MonkeyPatch) -> ClaudeAgentRuntime:
    scrub_live_env(monkeypatch)
    return ClaudeAgentRuntime(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH)


def _live_spec(tmp_path: Path, guard: GuardFn, tools: tuple[str, ...]) -> SessionSpec:
    return SessionSpec(
        kind="smoke",
        cwd=tmp_path,
        model="haiku",
        guard=guard,
        tools=tools,
        permission_mode="bypassPermissions",
        question_parked_message="PARKED",
    )


async def _collect(runtime: ClaudeAgentRuntime, h: SessionHandle, kind: type, *, idle: float = 120) -> list[Any]:
    seen: list[Any] = []
    async for event in runtime.events(h, idle_timeout=idle):
        seen.append(event)
        if isinstance(event, kind):
            break
    return seen


@pytest.mark.live
@live
async def test_live_a_gate_deny_holds_in_bypass_mode(live_runtime: ClaudeAgentRuntime, tmp_path: Path) -> None:
    async def deny_marker(call: ToolCall) -> GuardVerdict:
        if "7731" in str(call.tool_input.get("command")):
            return GuardVerdict(allow=False, message="BLOCKED: test gate", reason_code="forbidden")
        return GuardVerdict(allow=True)

    h = await live_runtime.open(_live_spec(tmp_path, deny_marker, ("Bash",)), resume_id=None, timeout=60)
    try:
        await live_runtime.send(
            h,
            AgentInput(
                text="Run this exact bash command yourself: `echo m7731 > blocked.txt`. If blocked, reply: blocked"
            ),
            mode="new_turn",
            timeout=10,
        )
        events = await _collect(live_runtime, h, TurnResult)
        assert ToolDenied(tool_name="Bash", reason_code="forbidden") in events
        assert not (tmp_path / "blocked.txt").exists()
        assert events[-1].permission_denials == ("Bash",)
    finally:
        await live_runtime.close(h, timeout=15)


@pytest.mark.live
@live
async def test_live_ask_user_question_round_trips(live_runtime: ClaudeAgentRuntime, tmp_path: Path) -> None:
    h = await live_runtime.open(_live_spec(tmp_path, _allow, ("Bash", "AskUserQuestion")), resume_id=None, timeout=60)
    try:
        await live_runtime.send(
            h,
            AgentInput(
                text="Use the AskUserQuestion tool to ask me one question: 'Which color do you prefer?' with options "
                "Red and Blue. Then reply with only the chosen color."
            ),
            mode="new_turn",
            timeout=10,
        )
        asked = (await _collect(live_runtime, h, QuestionAsked))[-1]
        assert isinstance(asked, QuestionAsked)
        question = str(asked.questions[0]["question"])
        assert await live_runtime.answer(h, asked.question_id, {question: "Blue"}, timeout=5) is True
        events = await _collect(live_runtime, h, TurnResult)
        assert "Blue" in events[-1].text
    finally:
        await live_runtime.close(h, timeout=15)
