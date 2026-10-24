"""`ClaudeAssistant`, the personal `qa` kind (T72; SPEC §5.3 personal column, §3.2.13; NFR-SEC-08; spike S5).

Offline: every client is a `FakeClient` replaying `tests/fixtures/claude/streams/assistant_qa.jsonl`, recorded from the
real CLI through this adapter (`test_live_record_the_qa_stream` re-records it with `--record-llm`), so the hooks,
the allowlist guard and the stream translator run for real. Pinned here: the session options, the allowlist guard,
the C2 check (`allowlist ⊆ init ⊆ allowlist + harness`, MCP servers exactly the toolsets and `connected`) with its
`PersonalToolsMismatch` refusal, the per-ask instructions, one pool per conversation (reused, resumed, reset), and the
in-process tool calls. The dev kinds' own suite (`test_claude_assistant.py`) is unchanged.
"""

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from tests.fakes.claude_client import FIXTURES, LIVE_CLI, LIVE_SEARCH_PATH, FakeClient, records, scrub_live_env
from tests.fakes.claude_tee import TeeClient, write_fixture
from tests.fakes.clock import FakeClock
from yordamchi.adapters.claude import assistant as adapter
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.adapters.claude.options import ALWAYS_DISALLOWED
from yordamchi.adapters.claude.stream import StreamTranslator
from yordamchi.core.errors import ConfigInvalid, PersonalToolsMismatch, ProviderUnavailable
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.assistant import (
    AssistantDelta,
    AssistantReply,
    AssistantRequest,
    AssistantTool,
    AssistantToolset,
    ToolHandler,
    ToolOutput,
)

TIMEOUT = 5.0
SEARCH_PATH = "/Users/owner/.local/bin:/usr/bin:/bin"
CLI = Path("/Users/owner/.local/bin/claude")
BASE_ENV = {"HOME": "/Users/owner", "PATH": "/elsewhere", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent"}
CONVERSATION = "6f1b9a52-3c41-4d7e-9a35-0d2c1f6b8e10"
OTHER_CONVERSATION = "0b7a3c1e-55d2-4c39-8f0e-9e4b2d7a6c31"
QA_SYSTEM = (
    "You are a concise personal assistant (test data). The application adds system reminders; each turn carries the "
    "owner's current instructions, and the latest one is authoritative. When asked about a software project, call "
    "the project_status tool. End every answer with a line ---OVOZ--- and one short spoken sentence."
)
UZ = "Current instructions from the owner: answer in Uzbek, in at most two sentences, end the answer with [UZ]."
EN = "Current instructions from the owner: answer in English, in one sentence, end the answer with [EN]."
PROJECT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"project": {"type": "string"}},
    "required": ["project"],
    "additionalProperties": False,
}
STATUS = {"project": "BonviHR", "data_minutes_ago": 3, "results": [{"status": "done", "natija": "Login tuzatildi"}]}
BRIDGE_TOOL = "mcp__yordamchi_bridge__project_status"
ALLOWLIST = ["WebFetch", "WebSearch", BRIDGE_TOOL]

type Recording = list[dict[str, Any]]


class ToolCalls:
    def __init__(self) -> None:
        self.arguments: list[dict[str, JsonValue]] = []

    async def __call__(self, arguments: Mapping[str, JsonValue]) -> ToolOutput:
        self.arguments.append(dict(arguments))
        return ToolOutput(text=json.dumps(STATUS))


def _toolset(handler: ToolHandler | None = None) -> AssistantToolset:
    tool = AssistantTool(
        name="project_status",
        description="Read-only status of one of the owner's software projects (test data).",
        input_schema=PROJECT_SCHEMA,
        timeout_s=2.0,
        handler=handler or ToolCalls(),
    )
    return AssistantToolset(server="yordamchi_bridge", tools=(tool,))


def _qa(prompt: str = "BonviHR qanday?", **overrides: object) -> AssistantRequest:
    values: dict[str, Any] = {
        "kind": "qa",
        "system_prompt": QA_SYSTEM,
        "prompt": prompt,
        "model": "haiku",
        "settings_version": 1,
        "effort": "low",
        "conversation": CONVERSATION,
        "instructions": UZ,
        "prompt_version": 1,
        "toolsets": (_toolset(),),
        "max_web_searches": 1000,
    }
    values.update(overrides)
    return AssistantRequest(**values)


# --- recordings ----------------------------------------------------------------------------------------------------
def _turns(name: str = "assistant_qa") -> list[Recording]:
    segments: list[Recording] = []
    for record in records(name):
        if record["kind"] == "sent":
            segments.append([])
        if segments:
            segments[-1].append(record)
    return segments


def _asks(n: int) -> Recording:
    turns = _turns()
    return [record for index in range(n) for record in turns[index % len(turns)]]


def _copy(recording: Recording) -> Recording:
    copied: Recording = json.loads(json.dumps(recording))
    return copied


def _with_init(recording: Recording, **fields: object) -> Recording:
    out = _copy(recording)
    for record in out:
        if record["kind"] == "raw" and record["data"].get("subtype") == "init":
            record["data"].update(fields)
    return out


class Factory:
    def __init__(self, *scripted: Recording) -> None:
        self.scripted = list(scripted)
        self.clients: list[FakeClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        recording = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        client = FakeClient(options, recording, warn=False)
        self.clients.append(client)
        return cast("ClaudeSDKClient", client)

    @property
    def options(self) -> list[ClaudeAgentOptions]:
        return [client.options for client in self.clients]


def _assistant(tmp_path: Path, factory: Factory) -> ClaudeAssistant:
    return ClaudeAssistant(
        cli_path=CLI,
        search_path=SEARCH_PATH,
        sessions_dir=tmp_path / "sessions",
        base_env=BASE_ENV,
        client_factory=factory,
        clock=FakeClock(),
    )


async def _until(predicate: Callable[[], bool], rounds: int = 500) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def _hook_outputs(client: FakeClient, event: str) -> list[dict[str, Any]]:
    return [
        output.get("hookSpecificOutput") or {}
        for output in client.hook_outputs
        if (output.get("hookSpecificOutput") or {}).get("hookEventName") == event
    ]


# --- live: record the fixture (`pytest -m live --record-llm tests/integration/test_claude_assistant_qa.py`) --------
live = pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")


@pytest.mark.live
@live
async def test_live_record_the_qa_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The real CLI with the personal options: `init` passes the C2 check under `--restricted`, the bridge tool is
    called through the in-process MCP server, the per-ask instructions are followed, and text streams as deltas."""
    from tests.eval.recorder import kill_leftover_children

    scrub_live_env(monkeypatch)
    tees: list[TeeClient] = []

    def factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        tee = TeeClient(options)
        tees.append(tee)
        return cast("ClaudeSDKClient", tee)

    sessions = tmp_path / "sessions"
    calls = ToolCalls()
    live_adapter = ClaudeAssistant(
        cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH, sessions_dir=sessions, client_factory=factory
    )
    try:
        first_items = [
            item
            async for item in live_adapter.stream(
                _qa("BonviHR loyihasi qanday? project_status vositasidan foydalaning.", toolsets=(_toolset(calls),)),
                idle_timeout=90,
            )
        ]
        started = time.monotonic()
        second = await live_adapter.ask(
            _qa("Thanks. What is a qubit?", instructions=EN, prompt_version=2, toolsets=(_toolset(calls),)),
            timeout=90,
        )
        warm_s = time.monotonic() - started
    finally:
        await live_adapter.close(timeout=15)
        assert await kill_leftover_children() == []
    first = first_items[-1]
    assert isinstance(first, AssistantReply)
    assert any(isinstance(item, AssistantDelta) for item in first_items[:-1])
    assert calls.arguments, "the bridge tool was called through the in-process MCP server"
    assert BRIDGE_TOOL in first.tool_calls
    assert first.session_id == CONVERSATION
    assert "---OVOZ---" in first.text
    assert second.session_id == CONVERSATION
    assert second.tool_calls == ()
    assert "[EN]" in second.text
    assert warm_s < 20
    if request.config.getoption("--record-llm"):
        write_fixture(
            tees[0].records,
            FIXTURES / "streams" / "assistant_qa.jsonl",
            home=Path.home(),
            cwd=sessions / "assistant",
            name="assistant",
        )


# --- session options (SPEC §5.3 personal column) -------------------------------------------------------------------
async def test_session_options_follow_the_personal_column(tmp_path: Path) -> None:
    factory = Factory(_asks(2))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    options = factory.options[0]

    assert options.cwd == str(tmp_path / "sessions" / "assistant")
    assert options.setting_sources == []
    assert options.system_prompt == QA_SYSTEM  # custom, frozen, fixed parts only
    assert options.tools == ["WebSearch", "WebFetch"]
    assert options.allowed_tools == sorted(ALLOWLIST)
    assert options.permission_mode == "dontAsk"
    assert options.strict_mcp_config is True
    servers = cast("dict[str, Any]", options.mcp_servers)
    assert set(servers) == {"yordamchi_bridge"}
    assert servers["yordamchi_bridge"]["type"] == "sdk"
    assert options.extra_args == {"restricted": None}
    assert options.include_partial_messages is True
    assert options.session_id == CONVERSATION  # the conversation key is the CLI session id
    assert options.resume is None
    assert options.model == "haiku"
    assert options.effort == "low"
    assert set(ALWAYS_DISALLOWED) <= set(options.disallowed_tools)
    env = options.env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "1000"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert set(options.hooks or {}) == {"PreToolUse", "UserPromptSubmit"}
    await assistant.close(timeout=TIMEOUT)


async def test_the_web_search_cap_is_the_callers(tmp_path: Path) -> None:
    factory = Factory(_asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(max_web_searches=1500), timeout=TIMEOUT)
    assert factory.options[0].env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "1500"
    await assistant.close(timeout=TIMEOUT)


# --- answers and tools ---------------------------------------------------------------------------------------------
async def test_a_qa_ask_streams_deltas_then_one_reply_with_its_tool_calls(tmp_path: Path) -> None:
    factory = Factory(_asks(2))
    assistant = _assistant(tmp_path, factory)
    items = [item async for item in assistant.stream(_qa(), idle_timeout=TIMEOUT)]
    reply = items[-1]
    assert isinstance(reply, AssistantReply)
    deltas = [item.text for item in items[:-1] if isinstance(item, AssistantDelta)]
    assert len(deltas) > 10  # partial messages: many small fragments
    assert "".join(deltas) == reply.text
    assert reply.tool_calls == (BRIDGE_TOOL,)
    assert reply.session_id == CONVERSATION
    assert "---OVOZ---" in reply.text
    await assistant.close(timeout=TIMEOUT)


def _pre_tool_hooks_renamed(tool_name: str) -> Recording:
    renamed = _copy(_asks(1))
    for record in renamed:
        if record["kind"] == "hook" and record["data"]["input"].get("hook_event_name") == "PreToolUse":
            record["data"]["input"]["tool_name"] = tool_name
    return renamed


async def test_the_allowlist_guard_allows_the_bridge_tool(tmp_path: Path) -> None:
    factory = Factory(_asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    assert [out.get("permissionDecision") for out in _hook_outputs(factory.clients[0], "PreToolUse")] == ["allow"]
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize(
    "forbidden", ["Bash", "Write", "Read", "Agent", "mcp__yordamchi_bridge__other", "mcp__remote__create_issue"]
)
async def test_the_allowlist_guard_denies_anything_else(tmp_path: Path, forbidden: str) -> None:
    factory = Factory(_pre_tool_hooks_renamed(forbidden))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    decisions = _hook_outputs(factory.clients[0], "PreToolUse")
    assert [out.get("permissionDecision") for out in decisions] == ["deny"]
    assert decisions[0]["permissionDecisionReason"] == adapter.QA_DENY_MESSAGE
    await assistant.close(timeout=TIMEOUT)


async def test_every_ask_carries_the_current_instructions(tmp_path: Path) -> None:
    factory = Factory(_asks(2))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(instructions=UZ, prompt_version=1), timeout=TIMEOUT)
    await assistant.ask(_qa("Thanks. What is a qubit?", instructions=EN, prompt_version=2), timeout=TIMEOUT)
    contexts = [out["additionalContext"] for out in _hook_outputs(factory.clients[0], "UserPromptSubmit")]
    assert contexts == [UZ, EN]
    assert len(factory.clients) == 1  # one warm session, no rotation
    await assistant.close(timeout=TIMEOUT)


async def test_the_in_process_tool_runs_the_current_ask_s_handler() -> None:
    calls = ToolCalls()
    request = _qa(toolsets=(_toolset(calls),))
    client = adapter._Client(
        kind="qa",
        key=adapter._LaunchKey.of(request.profile),
        session_id=CONVERSATION,
        model="haiku",
        settings_version=1,
        translator=StreamTranslator(session_id=CONVERSATION),
    )
    tool = request.toolsets[0].tools[0]
    sdk_tool = adapter._sdk_tool(client, BRIDGE_TOOL, tool)
    assert sdk_tool.name == "project_status"
    assert sdk_tool.input_schema == PROJECT_SCHEMA

    unavailable = await sdk_tool.handler({"project": "BonviHR"})
    assert unavailable == {"content": [{"type": "text", "text": adapter.TOOL_UNAVAILABLE}], "is_error": True}

    client.handlers = {BRIDGE_TOOL: tool.handler}
    result = await sdk_tool.handler({"project": "BonviHR"})
    assert result["is_error"] is False
    assert json.loads(result["content"][0]["text"]) == STATUS
    assert calls.arguments == [{"project": "BonviHR"}]

    async def slow(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        await asyncio.Event().wait()
        raise AssertionError(arguments)

    async def broken(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        raise ValueError(arguments)

    client.handlers = {BRIDGE_TOOL: slow}
    fast = AssistantTool(name="project_status", description="x", input_schema=PROJECT_SCHEMA, timeout_s=0.01)
    timed_out = await adapter._sdk_tool(client, BRIDGE_TOOL, fast).handler({})
    assert timed_out["content"][0]["text"] == adapter.TOOL_TIMED_OUT
    client.handlers = {BRIDGE_TOOL: broken}
    failed = await adapter._sdk_tool(client, BRIDGE_TOOL, tool).handler({})
    assert failed == {"content": [{"type": "text", "text": adapter.TOOL_FAILED}], "is_error": True}


# --- the C2 check (NFR-SEC-08, revised after S5) -------------------------------------------------------------------
@pytest.mark.parametrize(
    "tools",
    [
        [*ALLOWLIST, "Bash"],  # an extra tool
        ["WebFetch", "WebSearch"],  # the bridge tool missing
        [*ALLOWLIST, "mcp__yordamchi_bridge__write"],  # a tool the toolset does not declare
    ],
)
async def test_an_init_outside_the_allowlist_is_refused(tmp_path: Path, tools: list[str]) -> None:
    factory = Factory(_with_init(_asks(1), tools=tools))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(PersonalToolsMismatch) as raised:
        await assistant.ask(_qa(), timeout=TIMEOUT)
    assert raised.value.retryable is False
    assert len(factory.clients) == 1  # never retried on a second client
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize(
    "servers",
    [
        [{"name": "yordamchi_bridge", "status": "failed", "source": "sdk"}],
        [
            {"name": "yordamchi_bridge", "status": "connected", "source": "sdk"},
            {"name": "claude.ai Gmail", "status": "connected", "source": "claudeai"},
        ],
        [],
    ],
)
async def test_mcp_servers_must_be_exactly_the_toolsets_and_connected(tmp_path: Path, servers: list[Any]) -> None:
    factory = Factory(_with_init(_asks(1), mcp_servers=servers))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(PersonalToolsMismatch):
        await assistant.ask(_qa(), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_harness_tools_are_tolerated(tmp_path: Path) -> None:
    factory = Factory(_with_init(_asks(1), tools=[*ALLOWLIST, "ToolSearch", "EndConversation"]))
    assistant = _assistant(tmp_path, factory)
    reply = await assistant.ask(_qa(), timeout=TIMEOUT)
    assert reply.session_id == CONVERSATION
    await assistant.close(timeout=TIMEOUT)


async def test_a_dev_kind_refusal_is_unchanged(tmp_path: Path) -> None:
    """The dev kinds still refuse a wrong `init` as before: `ProviderUnavailable`, not the personal message."""
    factory = Factory(_with_init(records("assistant_report"), tools=["Bash"]))
    assistant = _assistant(tmp_path, factory)
    request = AssistantRequest(kind="report", system_prompt="x", prompt="y", model="haiku", settings_version=1)
    with pytest.raises(ProviderUnavailable):
        await assistant.ask(request, timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


# --- one pool per conversation -------------------------------------------------------------------------------------
async def test_conversations_have_their_own_sessions(tmp_path: Path) -> None:
    factory = Factory(_asks(2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_qa(), timeout=TIMEOUT)
    other = await assistant.ask(_qa(conversation=OTHER_CONVERSATION), timeout=TIMEOUT)
    again = await assistant.ask(_qa("Thanks. What is a qubit?", instructions=EN), timeout=TIMEOUT)
    assert [options.session_id for options in factory.options] == [CONVERSATION, OTHER_CONVERSATION]
    assert first.session_id == again.session_id == CONVERSATION
    assert other.session_id == OTHER_CONVERSATION
    assert len(factory.clients) == 2  # no hot spare for a conversation
    await assistant.close(timeout=TIMEOUT)


async def test_a_conversation_with_turns_resumes_after_a_restart(tmp_path: Path) -> None:
    factory = Factory(_asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(resume=True), timeout=TIMEOUT)
    options = factory.options[0]
    assert options.resume == CONVERSATION
    assert options.session_id is None
    await assistant.close(timeout=TIMEOUT)


async def test_a_settings_change_never_rotates_a_conversation(tmp_path: Path) -> None:
    factory = Factory(_asks(2))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(settings_version=1), timeout=TIMEOUT)
    await assistant.ask(_qa(settings_version=9, instructions=EN), timeout=TIMEOUT)
    assert len(factory.clients) == 1
    await assistant.close(timeout=TIMEOUT)


async def test_an_effort_change_resumes_the_same_session(tmp_path: Path) -> None:
    factory = Factory(_asks(1), _asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(effort="low"), timeout=TIMEOUT)
    await assistant.ask(_qa(effort="high"), timeout=TIMEOUT)
    first, second = factory.options
    assert first.session_id == CONVERSATION
    assert second.resume == CONVERSATION  # the conversation survives a relaunch
    assert second.effort == "high"
    assert factory.clients[0].disconnected  # the old process went first: one process per session id
    await assistant.close(timeout=TIMEOUT)


async def test_reset_ends_the_conversation(tmp_path: Path) -> None:
    factory = Factory(_asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    await assistant.reset("qa", conversation=CONVERSATION, timeout=TIMEOUT)
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.ask(_qa(conversation=OTHER_CONVERSATION), timeout=TIMEOUT)
    assert factory.options[1].session_id == OTHER_CONVERSATION
    await assistant.close(timeout=TIMEOUT)


async def test_warm_starts_the_conversation_without_asking(tmp_path: Path) -> None:
    factory = Factory(_asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.warm(_qa().profile, timeout=TIMEOUT)
    assert len(factory.clients) == 1
    assert factory.clients[0].sent == []
    await assistant.ask(_qa(), timeout=TIMEOUT)
    assert len(factory.clients) == 1  # the ask was warm
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"conversation": None}, "conversation_required"),
        ({"conversation": "not-a-uuid"}, "conversation_not_uuid"),
        ({"output_schema": PROJECT_SCHEMA}, "unsupported_for_qa"),
    ],
)
async def test_a_malformed_qa_request_is_refused(tmp_path: Path, overrides: dict[str, Any], problem: str) -> None:
    assistant = _assistant(tmp_path, Factory(_asks(1)))
    with pytest.raises(ConfigInvalid) as raised:
        await assistant.ask(_qa(**overrides), timeout=TIMEOUT)
    assert raised.value.params["problem"] == problem
    await assistant.close(timeout=TIMEOUT)


async def test_dev_kinds_take_no_toolsets(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path, Factory(_asks(1)))
    request = AssistantRequest(
        kind="report", system_prompt="x", prompt="y", model="haiku", settings_version=1, toolsets=(_toolset(),)
    )
    with pytest.raises(ConfigInvalid):
        await assistant.ask(request, timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)
