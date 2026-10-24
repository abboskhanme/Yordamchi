"""Spike S3 (SPEC §11, T04): under `bypassPermissions`, do PreToolUse hooks fire inside subagents, does a hook
`deny` hold, and does `AskUserQuestion` reach `can_use_tool`?

Findings in docs/SPIKES.md. Opt in with `pytest -m live`.
"""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from yordamchi.core import proc

CLI = Path("~/.local/bin/claude").expanduser()
pytestmark = [
    pytest.mark.live,
    # The SDK warns on every connect that bypass shadows can_use_tool. AskUserQuestion still reaches it (below).
    pytest.mark.filterwarnings("ignore::claude_agent_sdk.types.CanUseToolShadowedWarning"),
    pytest.mark.skipif(shutil.which(str(CLI)) is None, reason="claude CLI not installed at ~/.local/bin/claude"),
]
MARKER = "7731"  # a neutral token: a scary-looking one makes the model refuse to delegate the command
REASON = "SPIKE_GATE_DENIED"


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


class Probe:
    def __init__(self) -> None:
        self.hook_calls: list[dict[str, Any]] = []
        self.permission_calls: list[tuple[str, dict[str, Any]]] = []
        self.denied: list[dict[str, Any]] = []

    async def guard(self, data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        del tool_use_id, context
        raw: dict[str, Any] = dict(data)
        self.hook_calls.append(raw)
        if raw.get("tool_name") == "Bash" and MARKER in json.dumps(raw.get("tool_input")):
            self.denied.append(raw)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON,
                }
            }
        return {}

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        self.permission_calls.append((tool_name, tool_input))
        if tool_name == "AskUserQuestion":
            answers = {q["question"]: q["options"][-1]["label"] for q in tool_input.get("questions", [])}
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
        return PermissionResultDeny(message="fall-through denied")


def _options(cwd: Path, probe: Probe) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(cwd),
        model="haiku",
        setting_sources=[],
        permission_mode="bypassPermissions",
        tools=["Bash", "Agent", "AskUserQuestion"],
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[probe.guard], timeout=10)]},
        can_use_tool=probe.can_use_tool,
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )


async def _turn(cwd: Path, probe: Probe, prompt: str) -> list[dict[str, Any]]:
    """Raw messages until 20 s of silence after a result: subagents may run in the background and end in a
    second, CLI-started turn."""
    seen: list[dict[str, Any]] = []
    async with ClaudeSDKClient(options=_options(cwd, probe)) as client:
        await client.query(prompt)
        stream = _raw(client).__aiter__()
        got_result = False
        while True:
            try:
                msg = await asyncio.wait_for(stream.__anext__(), timeout=20 if got_result else 180)
            except TimeoutError:
                break
            seen.append(msg)
            got_result = got_result or msg.get("type") == "result"
    return seen


def _tool_results(seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block
        for m in seen
        if m.get("type") == "user" and isinstance(m["message"].get("content"), list)
        for block in m["message"]["content"]
        if block.get("type") == "tool_result"
    ]


@pytest.mark.usefixtures("clean_env")
async def test_hook_deny_holds_in_bypass_mode(tmp_path: Path) -> None:
    probe = Probe()
    seen = await _turn(tmp_path, probe, f"Run this exact bash command yourself: `echo m{MARKER} > main_blocked.txt`.")

    assert not (tmp_path / "main_blocked.txt").exists()
    assert any(REASON in str(r.get("content")) and r.get("is_error") for r in _tool_results(seen))
    denials = next(m for m in seen if m.get("type") == "result")["permission_denials"]
    assert [d["tool_name"] for d in denials] == ["Bash"]
    bash_calls = [c for c in probe.hook_calls if c.get("tool_name") == "Bash"]
    assert bash_calls[0]["permission_mode"] == "bypassPermissions"
    assert bash_calls[0].get("agent_id") is None


@pytest.mark.usefixtures("clean_env")
async def test_pretooluse_fires_inside_a_subagent_and_its_deny_holds(tmp_path: Path) -> None:
    probe = Probe()
    seen = await _turn(
        tmp_path,
        probe,
        "Use the Agent tool to start a general-purpose subagent. Tell the subagent to run two bash commands, one "
        f"after another: `echo hello > sub_ok.txt` and then `echo s{MARKER} > sub_blocked.txt`, and to report each "
        "tool result verbatim. Do not run any command yourself.",
    )

    sub_calls = [c for c in probe.hook_calls if c.get("tool_name") == "Bash" and c.get("agent_id")]
    assert sub_calls, "no PreToolUse call carried an agent_id"
    assert {c["agent_type"] for c in sub_calls} == {"general-purpose"}
    assert (tmp_path / "sub_ok.txt").exists()
    assert not (tmp_path / "sub_blocked.txt").exists()
    assert [d.get("agent_id") for d in probe.denied] == [sub_calls[0]["agent_id"]]
    # `permission_denials` carried the subagent's denial when it ran in the foreground, but not when the model
    # started it in the background, so the hook (not the result) is the source of ToolDenied for subagents.
    del seen


@pytest.mark.usefixtures("clean_env")
async def test_ask_user_question_reaches_can_use_tool_in_bypass_mode(tmp_path: Path) -> None:
    probe = Probe()
    seen = await _turn(
        tmp_path,
        probe,
        "Use the AskUserQuestion tool to ask me one question: 'Which color do you prefer?' with options Red and "
        "Blue. Then reply with only the chosen color.",
    )

    asked = [inp for name, inp in probe.permission_calls if name == "AskUserQuestion"]
    assert len(asked) == 1
    assert asked[0]["questions"][0]["options"][-1]["label"] == "Blue"
    # The hook sees the call first, then the call falls through to can_use_tool (no mode auto-approves it).
    assert any(c.get("tool_name") == "AskUserQuestion" for c in probe.hook_calls)
    assert [n for n, _ in probe.permission_calls] == ["AskUserQuestion"], "bypass must auto-approve everything else"
    final = [m for m in seen if m.get("type") == "result"][-1]
    assert "Blue" in (final.get("result") or "")
