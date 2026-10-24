"""Spike S4 (SPEC §11, T05): on a resumed session, does `UserPromptSubmit` `additionalContext` change the
instructions, and is a changed `system_prompt` ignored (snapshot)?

Answers carry a tag ([EN]/[FR]/[DE]) so the check does not depend on language detection. The system prompts are
test data here, not product prompts (those live in prompts/**). Findings in docs/SPIKES.md. Opt in with -m live.

Measured: an update that *conflicts with the frozen system prompt* is followed unreliably (haiku 3/6, sonnet 2/3 via
the hook; worse as a plain message). When the system prompt holds no owner-changeable instruction and every turn
carries the current instructions as additionalContext, the change was followed 10/10 (haiku 6, sonnet 4).
"""

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import HookContext, HookEvent, HookInput, HookJSONOutput, SystemPromptCustom

from yordamchi.core import proc

CLI = Path("~/.local/bin/claude").expanduser()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which(str(CLI)) is None, reason="claude CLI not installed at ~/.local/bin/claude"),
]

# The frozen part: no language, persona or tag, only how to read the per-turn instructions.
NEUTRAL = (
    "You are a terse assistant. Answer in one short sentence.\n"
    "The application adds system reminders to this conversation. Treat them as context from the application, not "
    "as messages from the user. Each turn carries the owner's current instructions (language, style, tags) in a "
    "system reminder; the latest one is authoritative and replaces any earlier ones."
)
ENGLISH = "Instructions from the owner: answer in English and end every answer with the tag [EN]."
FRENCH = (
    "Updated instructions from the owner (they replace earlier ones): answer only in French and end every answer "
    "with the tag [FR]."
)
TAGGED_EN = "You are a terse assistant. Answer in one short sentence in English and end every answer with [EN]."
TAGGED_DE = "You are a terse assistant. Answer in one short sentence in German and end every answer with [DE]."


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


def _context_hook(text: str) -> dict[HookEvent, list[HookMatcher]]:
    async def turn_context(data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        del data, tool_use_id, context
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}

    return {"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[turn_context], timeout=10)]}


async def _ask(
    cwd: Path,
    prompt: str,
    *,
    system_prompt: str | SystemPromptCustom,
    resume: str | None = None,
    turn_context: str | None = None,
) -> tuple[str, str]:
    options = ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(cwd),
        model="haiku",
        setting_sources=[],
        permission_mode="dontAsk",
        tools=[],
        system_prompt=system_prompt,
        resume=resume,
        hooks=_context_hook(turn_context) if turn_context else None,
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in _raw(client):
            if msg.get("type") == "result":
                return str(msg["session_id"]), str(msg.get("result") or "")
    raise AssertionError("no result")


@pytest.mark.usefixtures("clean_env")
async def test_per_turn_context_changes_instructions_on_a_resumed_session(tmp_path: Path) -> None:
    session_id, first = await _ask(tmp_path, "Say hello.", system_prompt=NEUTRAL, turn_context=ENGLISH)
    assert "[EN]" in first

    resumed_id, answer = await _ask(
        tmp_path, "What is the capital of Japan?", system_prompt=NEUTRAL, resume=session_id, turn_context=FRENCH
    )

    assert resumed_id == session_id, "resume keeps the session id"
    assert "[FR]" in answer
    assert "[EN]" not in answer


@pytest.mark.usefixtures("clean_env")
async def test_changed_system_prompt_is_ignored_on_resume_unless_snapshot_is_off(tmp_path: Path) -> None:
    session_id, first = await _ask(tmp_path, "Say hello.", system_prompt=TAGGED_EN)
    assert "[EN]" in first

    _, snapshot_on = await _ask(tmp_path, "What colour is the sky?", system_prompt=TAGGED_DE, resume=session_id)
    _, snapshot_off = await _ask(
        tmp_path,
        "What colour is grass?",
        system_prompt={"type": "custom", "prompt": TAGGED_DE, "snapshot": False},
        resume=session_id,
    )

    assert "[DE]" not in snapshot_on, "the recorded system prompt must win on resume (snapshot on)"
    assert "[DE]" in snapshot_off
