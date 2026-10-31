"""`ClaudeAssistant`, the personal `qa` kind in agent mode (SPEC §3.2.18; slice A: the adapter only).

Offline: the clients replay the recorded `assistant_qa` stream (`test_claude_assistant_qa.py`), with the `init` frame
edited to what the CLI preset reports, so the hooks, the guard bridge and the stream translator run for real. Pinned
here: the session options (workspace cwd, the preset, `bypassPermissions`, no `--restricted`, strict MCP, the clean
env), the guard (called for built-ins, its deny honoured, the in-process tools allowed without it, the latest ask's
guard used), the relaxed `init` check (`Bash` and the toolsets present, MCP servers exactly the toolsets), the launch
signature (agent mode on/off and the workspace rebuild and resume the conversation) and the refusals.
"""

import stat
from pathlib import Path
from typing import Any, cast

import pytest

from tests.fakes.clock import FakeClock
from tests.integration.test_claude_assistant_qa import (
    ALLOWLIST,
    BASE_ENV,
    BRIDGE_TOOL,
    CLI,
    CONVERSATION,
    SEARCH_PATH,
    TIMEOUT,
    Factory,
    Recording,
    _asks,
    _hook_outputs,
    _pre_tool_hooks_renamed,
    _qa,
    _toolset,
    _with_init,
)
from yordamchi.adapters.claude import assistant as adapter
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.adapters.claude.options import ALWAYS_DISALLOWED
from yordamchi.core.errors import ConfigInvalid, PersonalToolsMismatch
from yordamchi.ports.agent import GuardVerdict, ToolCall
from yordamchi.ports.assistant import AgentMode, AssistantRequest

PRESET = ["Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write", "WebFetch", "WebSearch", "TodoWrite", BRIDGE_TOOL]
WAIT = "approval_pending: deploy"


class Guard:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> GuardVerdict:
        self.calls.append(call)
        if self.allow:
            return GuardVerdict(allow=True)
        return GuardVerdict(allow=False, message=WAIT, reason_code="ask_deploy")


def _assistant(tmp_path: Path, factory: Factory) -> ClaudeAssistant:
    return ClaudeAssistant(
        cli_path=CLI,
        search_path=SEARCH_PATH,
        sessions_dir=tmp_path / "data" / "sessions",
        base_env=BASE_ENV,
        client_factory=factory,
        clock=FakeClock(),
    )


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "Projects" / "_shaxsiy"


def _agent(tmp_path: Path, guard: Guard | None = None, *, workspace: Path | None = None) -> AgentMode:
    return AgentMode(workspace=workspace or _workspace(tmp_path), guard=guard or Guard())


def _preset(recording: Recording | None = None, tools: list[str] | None = None) -> Recording:
    return _with_init(recording or _asks(1), tools=tools if tools is not None else PRESET)


def _decisions(factory: Factory, index: int = 0) -> list[Any]:
    return [out.get("permissionDecision") for out in _hook_outputs(factory.clients[index], "PreToolUse")]


# --- session options (SPEC §3.2.18 item 1) -------------------------------------------------------------------------
async def test_agent_options_run_the_cli_preset_in_the_workspace(tmp_path: Path) -> None:
    factory = Factory(_preset())
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    options = factory.options[0]

    workspace = _workspace(tmp_path)
    assert options.cwd == str(workspace)
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert options.tools is None  # the CLI preset: every built-in tool
    assert "restricted" not in options.extra_args
    assert options.permission_mode == "bypassPermissions"
    assert options.allowed_tools == [BRIDGE_TOOL]  # bypass covers the built-ins; the guard decides them
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    servers = cast("dict[str, Any]", options.mcp_servers)
    assert set(servers) == {"yordamchi_bridge"}
    assert {*ALWAYS_DISALLOWED, "AskUserQuestion"} <= set(options.disallowed_tools)
    assert options.session_id == CONVERSATION
    env = options.env
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    for refused in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        assert refused not in env
    assert "CLAUDECODE" not in env
    assert "bare" not in options.extra_args
    assert set(options.hooks or {}) == {"PreToolUse", "UserPromptSubmit"}
    await assistant.close(timeout=TIMEOUT)


async def test_an_existing_workspace_is_used_as_it_is(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.mkdir(parents=True, mode=0o755)
    (workspace / "notes.txt").write_text("kept", encoding="utf-8")
    factory = Factory(_preset())
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "kept"
    await assistant.close(timeout=TIMEOUT)


# --- the guard -----------------------------------------------------------------------------------------------------
async def test_the_in_process_tool_is_allowed_without_the_guard(tmp_path: Path) -> None:
    guard = Guard(allow=False)
    factory = Factory(_preset())
    assistant = _assistant(tmp_path, factory)
    reply = await assistant.ask(_qa(agent=_agent(tmp_path, guard)), timeout=TIMEOUT)
    assert reply.tool_calls == (BRIDGE_TOOL,)
    assert guard.calls == []
    assert "deny" not in _decisions(factory)
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize("tool", ["Bash", "Write", "mcp__yordamchi_bridge__other"])
async def test_the_guard_decides_every_other_tool_and_its_deny_is_honoured(tmp_path: Path, tool: str) -> None:
    guard = Guard(allow=False)
    factory = Factory(_preset(_pre_tool_hooks_renamed(tool)))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=_agent(tmp_path, guard)), timeout=TIMEOUT)
    assert [call.tool_name for call in guard.calls] == [tool]
    decisions = _hook_outputs(factory.clients[0], "PreToolUse")
    assert [out.get("permissionDecision") for out in decisions] == ["deny"]
    assert decisions[0]["permissionDecisionReason"] == WAIT
    await assistant.close(timeout=TIMEOUT)


async def test_a_guard_allow_lets_bypass_decide(tmp_path: Path) -> None:
    guard = Guard(allow=True)
    factory = Factory(_preset(_pre_tool_hooks_renamed("Bash")))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=_agent(tmp_path, guard)), timeout=TIMEOUT)
    assert [call.tool_name for call in guard.calls] == ["Bash"]
    assert "deny" not in _decisions(factory)
    await assistant.close(timeout=TIMEOUT)


async def test_a_raising_guard_is_a_deny(tmp_path: Path) -> None:
    async def broken(call: ToolCall) -> GuardVerdict:
        raise RuntimeError(call.tool_name)

    factory = Factory(_preset(_pre_tool_hooks_renamed("Bash")))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=AgentMode(workspace=_workspace(tmp_path), guard=broken)), timeout=TIMEOUT)
    assert _decisions(factory) == ["deny"]
    await assistant.close(timeout=TIMEOUT)


async def test_each_ask_brings_its_own_guard(tmp_path: Path) -> None:
    warmed, asked = Guard(allow=False), Guard(allow=False)
    factory = Factory(_preset(_pre_tool_hooks_renamed("Bash")))
    assistant = _assistant(tmp_path, factory)
    await assistant.warm(_qa(agent=_agent(tmp_path, warmed)).profile, timeout=TIMEOUT)
    await assistant.ask(_qa(agent=_agent(tmp_path, asked)), timeout=TIMEOUT)
    assert len(factory.clients) == 1  # a new guard is not a new launch
    assert warmed.calls == []
    assert [call.tool_name for call in asked.calls] == ["Bash"]
    await assistant.close(timeout=TIMEOUT)


# --- the init check (SPEC §3.2.18 item 1, last bullet) -------------------------------------------------------------
async def test_the_preset_with_bash_and_connected_toolsets_passes(tmp_path: Path) -> None:
    factory = Factory(_preset(tools=[*PRESET, "NotebookEdit", "ToolSearch", "SomeFutureTool"]))
    assistant = _assistant(tmp_path, factory)
    reply = await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    assert reply.session_id == CONVERSATION
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize(
    "tools",
    [
        ALLOWLIST,  # no Bash: the preset is not on (a restricted session answering agent mode)
        [tool for tool in PRESET if tool != BRIDGE_TOOL],  # the in-process tool missing
    ],
)
async def test_an_agent_init_without_bash_or_a_toolset_tool_is_refused(tmp_path: Path, tools: list[str]) -> None:
    factory = Factory(_preset(tools=tools))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(PersonalToolsMismatch):
        await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    assert len(factory.clients) == 1
    await assistant.close(timeout=TIMEOUT)


@pytest.mark.parametrize(
    "servers",
    [
        [
            {"name": "yordamchi_bridge", "status": "connected", "source": "sdk"},
            {"name": "github", "status": "connected", "source": "user"},
        ],
        [{"name": "yordamchi_bridge", "status": "failed", "source": "sdk"}],
        [],
    ],
)
async def test_agent_mcp_servers_must_be_exactly_the_connected_toolsets(tmp_path: Path, servers: list[Any]) -> None:
    factory = Factory(_with_init(_preset(), mcp_servers=servers))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(PersonalToolsMismatch):
        await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


# --- the launch signature ------------------------------------------------------------------------------------------
async def test_turning_agent_mode_on_rebuilds_and_resumes_the_conversation(tmp_path: Path) -> None:
    factory = Factory(_asks(1), _preset(), _asks(1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    await assistant.ask(_qa(), timeout=TIMEOUT)
    assert len(factory.clients) == 3
    restricted, agent, back = factory.options
    assert restricted.permission_mode == "dontAsk"
    assert agent.permission_mode == "bypassPermissions"
    assert (agent.resume, back.resume) == (CONVERSATION, CONVERSATION)  # the same conversation, resumed
    assert back.extra_args == adapter.QA_EXTRA_ARGS
    await assistant.close(timeout=TIMEOUT)


async def test_moving_the_workspace_rebuilds_the_client(tmp_path: Path) -> None:
    factory = Factory(_preset())
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_qa(agent=_agent(tmp_path)), timeout=TIMEOUT)
    await assistant.ask(_qa(agent=_agent(tmp_path, workspace=tmp_path / "elsewhere")), timeout=TIMEOUT)
    assert [options.cwd for options in factory.options] == [str(_workspace(tmp_path)), str(tmp_path / "elsewhere")]
    await assistant.close(timeout=TIMEOUT)


# --- refusals ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("inside", ["data", "data/releases/current", "data/sessions/assistant"])
async def test_a_workspace_inside_yordamchi_data_is_refused(tmp_path: Path, inside: str) -> None:
    factory = Factory(_preset())
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(ConfigInvalid) as raised:
        await assistant.ask(_qa(agent=_agent(tmp_path, workspace=tmp_path / inside)), timeout=TIMEOUT)
    assert raised.value.params["problem"] == "inside_data_dir"
    assert factory.clients == []
    await assistant.close(timeout=TIMEOUT)


def test_a_relative_workspace_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute"):
        AgentMode(workspace=Path("Projects/_shaxsiy"), guard=Guard())


async def test_dev_kinds_take_no_agent(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path, Factory(_asks(1)))
    request = AssistantRequest(
        kind="report", system_prompt="x", prompt="y", model="haiku", settings_version=1, agent=_agent(tmp_path)
    )
    with pytest.raises(ConfigInvalid) as raised:
        await assistant.ask(request, timeout=TIMEOUT)
    assert raised.value.params["problem"] == "dev_kinds_have_no_agent"
    await assistant.close(timeout=TIMEOUT)


def test_the_guard_is_not_part_of_the_profile() -> None:
    workspace = Path("/Users/owner/Documents/Projects/_shaxsiy")
    one = _qa(agent=AgentMode(workspace=workspace, guard=Guard())).profile
    two = _qa(agent=AgentMode(workspace=workspace, guard=Guard(allow=False))).profile
    assert one == two
    assert one != _qa().profile
    assert _toolset() == _toolset()
