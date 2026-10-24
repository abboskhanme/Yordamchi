"""Spike S6 (SPEC §11, T06): do SDK-spawned sessions appear in `~/.claude/sessions/<pid>.json`, and with which
`kind`/`entrypoint`, so Yordamchi can tell its own sessions from the owner's interactive ones?

Read-only on `~/.claude/sessions/`. Findings in docs/SPIKES.md. Opt in with `pytest -m live`.
"""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from yordamchi.core import proc

CLI = Path("~/.local/bin/claude").expanduser()
SESSIONS = Path("~/.claude/sessions").expanduser()
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


def _cli_pid(client: ClaudeSDKClient) -> int:
    transport = client._transport
    assert transport is not None, "not connected"
    return int(vars(transport)["_process"].pid)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    inherited = dict(os.environ)  # noqa: TID251 - read-only snapshot; the SDK child inherits this process env
    if proc.refused_credentials(inherited):
        pytest.skip("a refused credential source is set in this environment")
    for name in proc.scrub_environ(dict(inherited)):
        monkeypatch.delenv(name)
    for name in ("CLAUDE_PID", "CLAUDE_EFFORT"):  # set by a parent Claude Code session; not in the scrub list yet
        monkeypatch.delenv(name, raising=False)


def _record(pid: int) -> dict[str, Any] | None:
    path = SESSIONS / f"{pid}.json"
    if not path.exists():
        return None
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


@pytest.mark.usefixtures("clean_env")
async def test_sdk_sessions_are_listed_with_entrypoint_sdk_py(tmp_path: Path) -> None:
    options = ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(tmp_path),
        model="haiku",
        setting_sources=[],
        tools=[],
        permission_mode="dontAsk",
        strict_mcp_config=True,
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )
    init: dict[str, Any] = {}
    async with ClaudeSDKClient(options=options) as client:
        pid = _cli_pid(client)
        await client.query("Reply OK.")
        async for msg in _raw(client):
            if msg.get("subtype") == "init":
                init = msg
            if msg.get("type") == "result":
                break
        record = _record(pid)
    await asyncio.sleep(1.0)
    after_close = _record(pid)

    assert record is not None, "the SDK session is not listed"
    assert record["entrypoint"] == "sdk-py"  # the owner's terminal sessions say "cli"
    assert record["kind"] == "interactive"  # same as the owner's: `kind` cannot tell them apart
    assert record["sessionId"] == init["session_id"]
    assert record["pid"] == pid
    assert record["cwd"] == init["cwd"]
    assert record["status"] in {"idle", "busy", "waiting"}
    # Every SDK session also opens a cross-session messaging socket (peer messages).
    assert record["messagingSocketPath"] == init["messaging_socket_path"]
    assert after_close is None, "a clean disconnect removes the record"
