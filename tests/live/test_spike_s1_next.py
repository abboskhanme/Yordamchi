"""Spike S1 (SPEC §11, T03): `priority` passthrough on the Python `ClaudeSDKClient` streaming input.

Findings recorded in docs/SPIKES.md. Runs against the real `~/.local/bin/claude`; opt in with `pytest -m live`.
"""

import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from yordamchi.core import proc

CLI = Path("~/.local/bin/claude").expanduser()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which(str(CLI)) is None, reason="claude CLI not installed at ~/.local/bin/claude"),
]


def _raw(client: ClaudeSDKClient) -> AsyncIterator[dict[str, Any]]:
    """Raw stream-json messages: the SDK parser drops some types (`command_lifecycle`)."""
    query = client._query
    assert query is not None, "not connected"
    stream: AsyncIterator[dict[str, Any]] = query.receive_messages()
    return stream


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK merges `env` over the inherited environment and strips only CLAUDECODE (STACK §4.4)."""
    inherited = dict(os.environ)  # noqa: TID251 - read-only snapshot; the SDK child inherits this process env
    if proc.refused_credentials(inherited):
        pytest.skip("a refused credential source is set in this environment")
    for name in proc.scrub_environ(dict(inherited)):
        monkeypatch.delenv(name)
    for name in ("CLAUDE_PID", "CLAUDE_EFFORT"):  # set by a parent Claude Code session; not in the scrub list yet
        monkeypatch.delenv(name, raising=False)


def _options(cwd: Path) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(cwd),
        model="haiku",
        setting_sources=[],
        permission_mode="bypassPermissions",
        tools=["Bash"],
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )


def _user(text: str, **extra: object) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "default",
        **extra,
    }


async def _one(msg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield msg


async def _drive(cwd: Path, sleep_s: int, follow: dict[str, Any], quiet_s: float) -> list[dict[str, Any]]:
    """Start a turn that runs `sleep`, send `follow` 1.5 s after the Bash tool_use, collect raw messages until
    `quiet_s` of silence after the last result. Raw messages, because the SDK parser drops `command_lifecycle`."""
    seen: list[dict[str, Any]] = []
    async with ClaudeSDKClient(options=_options(cwd)) as client:
        await client.query(f"Run exactly this bash command: `sleep {sleep_s} && echo first-done`, then report it.")
        stream = _raw(client).__aiter__()
        sent = False
        results = 0
        while True:
            try:
                msg = await asyncio.wait_for(stream.__anext__(), timeout=quiet_s if results else 120)
            except TimeoutError:
                break
            seen.append(msg)
            if not sent and _calls_bash(msg):
                await asyncio.sleep(1.5)
                await client.query(_one(follow))
                sent = True
            if msg.get("type") == "result":
                results += 1
    assert sent, "the model never called Bash"
    return seen


def _calls_bash(msg: dict[str, Any]) -> bool:
    content = msg["message"]["content"] if msg.get("type") == "assistant" else []
    return any(b.get("type") == "tool_use" and b.get("name") == "Bash" for b in content)


def _results(seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in seen if m.get("type") == "result"]


def _lifecycle(seen: list[dict[str, Any]], command_uuid: str) -> list[str]:
    return [m["state"] for m in seen if m.get("type") == "command_lifecycle" and m.get("command_uuid") == command_uuid]


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("priority", ["next", None])
async def test_next_follow_up_merges_into_the_running_turn(tmp_path: Path, priority: str | None) -> None:
    extra = {"priority": priority} if priority else {}
    follow = _user("Change: once the command finishes, your whole final answer must be the word PINEAPPLE.", **extra)

    seen = await _drive(tmp_path, 8, follow, quiet_s=15)

    results = _results(seen)
    assert len(results) == 1, "a merged follow-up must not produce a second result"
    assert results[0]["terminal_reason"] == "completed"
    assert _lifecycle(seen, str(follow["uuid"])) == ["queued", "started", "completed"]
    started = next(i for i, m in enumerate(seen) if m.get("type") == "command_lifecycle" and m["state"] == "started")
    assert started < seen.index(results[0]), "the follow-up was taken inside the running turn"
    assert "PINEAPPLE" in (results[0].get("result") or "")


@pytest.mark.usefixtures("clean_env")
async def test_now_with_human_origin_backgrounds_the_shell_and_redirects(tmp_path: Path) -> None:
    follow = _user(
        "Change of plan: forget the sleep command. Reply with the single word REDIRECTED.",
        priority="now",
        origin={"kind": "human"},
    )

    seen = await _drive(tmp_path, 12, follow, quiet_s=20)

    backgrounded = [
        m for m in seen if m.get("subtype") == "task_updated" and (m.get("patch") or {}).get("is_backgrounded") is True
    ]
    assert backgrounded, "the running Bash call was not moved to the background"
    results = _results(seen)
    # 1) the interrupted turn: empty text, aborted; 2) the redirect; 3) a turn the CLI starts on its own when the
    # backgrounded shell finishes (task_notification). Yordamchi must not deliver 1) and must expect 3).
    assert results[0]["terminal_reason"] == "aborted_tools"
    assert not results[0].get("result")
    assert "REDIRECTED" in (results[1].get("result") or "")
    assert any(m.get("subtype") == "task_notification" for m in seen)
    assert len(results) >= 2
