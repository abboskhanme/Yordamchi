"""Spike S5 (SPEC §11, T06): `--restricted` in a personal-style session (`setting_sources=[]`, tools WebSearch +
WebFetch, `strict_mcp_config=True`, one in-process SDK MCP tool). Do the three tools still work, and which tool
list does `init` report (C2)?

Findings in docs/SPIKES.md. Opt in with `pytest -m live` (uses the network: WebSearch, WebFetch of example.com).
"""

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ProcessError,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput, PermissionMode

from yordamchi.core import proc

CLI = Path("~/.local/bin/claude").expanduser()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which(str(CLI)) is None, reason="claude CLI not installed at ~/.local/bin/claude"),
]
ALLOWLIST = ("WebSearch", "WebFetch", "mcp__ybridge__ping")
HARNESS = frozenset({"EndConversation", "ToolSearch", "WaitForMcpServers"})  # STACK C2


def _raw(client: ClaudeSDKClient) -> AsyncIterator[dict[str, Any]]:
    """Raw stream-json messages: the SDK parser drops some types (`command_lifecycle`)."""
    query = client._query
    assert query is not None, "not connected"
    stream: AsyncIterator[dict[str, Any]] = query.receive_messages()
    return stream


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    inherited = dict(os.environ)  # noqa: TID251 - read-only snapshot; the SDK child inherits this process env
    if proc.refused_credentials(inherited):
        pytest.skip("a refused credential source is set in this environment")
    for name in proc.scrub_environ(dict(inherited)):
        monkeypatch.delenv(name)
    for name in ("CLAUDE_PID", "CLAUDE_EFFORT"):  # set by a parent Claude Code session; not in the scrub list yet
        monkeypatch.delenv(name, raising=False)


PINGS: list[str] = []


@tool("ping", "Return pong plus the given text.", {"text": str})
async def ping(args: dict[str, Any]) -> dict[str, Any]:
    PINGS.append(str(args["text"]))
    return {"content": [{"type": "text", "text": f"pong:{args['text']}"}]}


async def _allowlist(data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
    del tool_use_id, context
    allowed = dict(data).get("tool_name") in ALLOWLIST
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": "allowlist",
        }
    }


def _options(cwd: Path, *, restricted: bool, permission_mode: PermissionMode = "dontAsk") -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(cwd),
        model="haiku",
        setting_sources=[],
        tools=["WebSearch", "WebFetch"],
        allowed_tools=list(ALLOWLIST),
        permission_mode=permission_mode,
        strict_mcp_config=True,
        mcp_servers={"ybridge": create_sdk_mcp_server(name="ybridge", version="1.0.0", tools=[ping])},
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_allowlist], timeout=10)]},
        system_prompt="You are a concise assistant. Use the tools you are asked to use.",
        extra_args={"restricted": None} if restricted else {},
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "1000"},
    )


@pytest.mark.usefixtures("clean_env")
async def test_restricted_keeps_websearch_webfetch_and_sdk_mcp(tmp_path: Path) -> None:
    PINGS.clear()
    turns = {
        "WebSearch": "Use the WebSearch tool to search for 'Tashkent population'. Reply with one number you found.",
        "WebFetch": "Use the WebFetch tool to fetch https://example.com and reply with the page title only.",
        "mcp__ybridge__ping": "Call the ping tool with text 'abc123' and reply with exactly what it returned.",
    }
    init: dict[str, Any] = {}
    answers: dict[str, str] = {}
    used: dict[str, set[str]] = {}
    async with ClaudeSDKClient(options=_options(tmp_path, restricted=True)) as client:
        for expected_tool, prompt in turns.items():
            await client.query(prompt)
            used[expected_tool] = set()
            async for msg in _raw(client):
                if msg.get("subtype") == "init" and not init:
                    init = msg
                if msg.get("type") == "assistant":
                    used[expected_tool] |= {b["name"] for b in msg["message"]["content"] if b.get("type") == "tool_use"}
                if msg.get("type") == "result":
                    assert not msg["is_error"]
                    assert msg.get("permission_denials") == []
                    answers[expected_tool] = str(msg.get("result") or "")
                    break

    tools = set(init["tools"])
    assert set(ALLOWLIST) <= tools <= set(ALLOWLIST) | HARNESS, f"C2 check: {sorted(tools)}"
    assert init["mcp_servers"] == [{"name": "ybridge", "status": "connected", "source": "sdk"}]
    assert init["permissionMode"] == "dontAsk"
    for expected_tool in turns:
        assert expected_tool in used[expected_tool]
    assert "Example Domain" in answers["WebFetch"]
    assert "pong:abc123" in answers["mcp__ybridge__ping"]
    assert PINGS == ["abc123"]


async def _run_once(options: ClaudeAgentOptions) -> None:
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Reply OK.")
        async for _ in _raw(client):
            pass


@pytest.mark.usefixtures("clean_env")
async def test_restricted_is_active_it_refuses_bypass_permissions(tmp_path: Path) -> None:
    lines: list[str] = []
    options = _options(tmp_path, restricted=True, permission_mode="bypassPermissions")
    options.stderr = lines.append
    with pytest.raises(ProcessError):
        await _run_once(options)
    assert any("bypassPermissions not supported in restricted mode" in line for line in lines)
