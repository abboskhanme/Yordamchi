"""Spike S2 (SPEC §11, T03): does `interrupt()` kill a running Bash command within 2 s?

The command writes its own pid, so no `ps` is needed. Findings in docs/SPIKES.md. Opt in with `pytest -m live`.
"""

import asyncio
import os
import shutil
import signal
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
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

# A foreground `sleep 30 && …` is refused by the CLI's own sleep guard ("Blocked: sleep 30 followed by …"), so the
# long-running command is a Python sleep that records its pid.
PLAIN = 'python3 -c \'import os,time; open("pid.txt","w").write(str(os.getpid())); time.sleep(30)\' && echo done'
IGNORES_TERM = (
    "python3 -c 'import os,signal,time; [signal.signal(s, signal.SIG_IGN) for s in "
    '(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)]; open("pid.txt","w").write(str(os.getpid())); '
    "time.sleep(30)' && echo done"
)


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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(condition: Callable[[], bool], *, limit_s: float) -> None:
    """Polls a process-level fact (a pid, a file) that no event signals."""
    deadline = time.monotonic() + limit_s
    while not condition() and time.monotonic() < deadline:  # noqa: ASYNC110 - nothing to await on
        await asyncio.sleep(0.02)


async def _one(msg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield msg


async def _interrupt_running_command(
    cwd: Path, command: str, *, cancel_queued: bool = False
) -> tuple[float, dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Returns (seconds until the command's process died, interrupt receipt, raw messages, pid facts)."""
    options = ClaudeAgentOptions(
        cli_path=str(CLI),
        cwd=str(cwd),
        model="haiku",
        setting_sources=[],
        permission_mode="bypassPermissions",
        tools=["Bash"],
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )
    pid_file = cwd / "pid.txt"
    seen: list[dict[str, Any]] = []
    facts: dict[str, int] = {}
    died_after = -1.0
    receipt: dict[str, Any] = {}
    async with ClaudeSDKClient(options=options) as client:
        cli_pid = _cli_pid(client)
        facts.update(cli_pid=cli_pid, cli_pgid=os.getpgid(cli_pid), own_pgid=os.getpgrp())
        await client.query(f"Run exactly this bash command in the foreground, unchanged: `{command}`")
        stream = _raw(client).__aiter__()
        async with asyncio.timeout(120):
            while True:
                msg = await stream.__anext__()
                seen.append(msg)
                if msg.get("subtype") == "task_started":
                    break
            await _until(lambda: pid_file.exists() and bool(pid_file.read_text().strip()), limit_s=60)
        target = int(pid_file.read_text())
        facts.update(target_pid=target, target_pgid=os.getpgid(target))
        if cancel_queued:
            queued = {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "message": {"role": "user", "content": "Say QUEUED-RAN."},
                "parent_tool_use_id": None,
                "session_id": "default",
            }
            await client.query(_one(queued))
            await asyncio.sleep(0.3)
        # `ClaudeSDKClient.interrupt()` discards the receipt; the control request returns it.
        request: dict[str, object] = {"subtype": "interrupt"}
        if cancel_queued:
            request["cancel_queued"] = True
        started = time.monotonic()
        query = client._query
        assert query is not None
        receipt = await query._send_control_request(request)
        await _until(lambda: not _alive(target), limit_s=10)
        died_after = time.monotonic() - started if not _alive(target) else -1.0
        async with asyncio.timeout(30):
            while True:
                msg = await stream.__anext__()
                seen.append(msg)
                if msg.get("type") == "result":
                    break
        if _alive(target):
            with suppress(ProcessLookupError):
                os.kill(target, signal.SIGKILL)
    return died_after, receipt, seen, facts


@pytest.mark.usefixtures("clean_env")
async def test_interrupt_kills_the_running_command_within_2s(tmp_path: Path) -> None:
    died_after, receipt, seen, facts = await _interrupt_running_command(tmp_path, PLAIN)

    assert 0 <= died_after < 2.0, f"command still alive {died_after=}"
    assert receipt == {"still_queued": []}
    result = next(m for m in seen if m.get("type") == "result")
    assert result["subtype"] == "error_during_execution"
    assert result["is_error"] is True
    assert result["terminal_reason"] == "aborted_tools"
    # The Bash shell leads its own process group; the CLI shares the caller's group (the SDK does not call
    # setsid). Killing "the group" therefore means the tool's group, never os.getpgid(cli_pid).
    assert facts["cli_pgid"] == facts["own_pgid"]
    assert facts["target_pgid"] != facts["cli_pgid"]


@pytest.mark.usefixtures("clean_env")
async def test_interrupt_escalates_past_an_ignored_sigterm(tmp_path: Path) -> None:
    died_after, _, _, _ = await _interrupt_running_command(tmp_path, IGNORES_TERM)

    # Observed 1.54 s on 2.1.309: the CLI escalates to SIGKILL after about 1.5 s.
    assert 0 <= died_after < 2.5, f"TERM-ignoring command still alive {died_after=}"


@pytest.mark.usefixtures("clean_env")
async def test_interrupt_cancel_queued_drops_the_queued_follow_up(tmp_path: Path) -> None:
    died_after, receipt, seen, _ = await _interrupt_running_command(tmp_path, PLAIN, cancel_queued=True)

    assert 0 <= died_after < 2.0
    assert receipt.get("still_queued") == []
    assert len(receipt.get("cancelled") or []) == 1
    states = [m["state"] for m in seen if m.get("type") == "command_lifecycle"]
    assert "cancelled" in states
    assert "started" not in states
