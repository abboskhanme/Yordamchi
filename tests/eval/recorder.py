"""Recorded model answers for the eval suites (CONVENTIONS §11; SPEC §8).

Replay: `FakeAssistant(fixtures=load_fixtures(directory))` answers each ask from `<directory>/<fixture_key>.json`
(`tests/fakes/assistant.py`). The key covers the system prompt, the input, the model, the schema and the instructions
(for the settings kind: the whole settings table with its current values), so a changed prompt, a new setting or a
changed case is a stale key and fails loudly.

Record: a suite's `live` test wraps the real `ClaudeAssistant` (`live_claude`) in a `Recorder`. Every ask goes through
to the model; with `--record-llm` the answer is written to that file, tagged with the case that asked. `live_claude`
closes every client when its block ends, and `kill_leftover_children` then signals any process this test process
still has below it, so no `claude` child outlives the run.
"""

import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tests.fakes.assistant import fixture_key
from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH
from yordamchi.adapters.claude import proctree
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.core import proc
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)

FIXTURES_ROOT: Final = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
# A cold `claude` spawn plus one answer. The settings ask's own bound (`timeouts.settings_s`) is for the bot, which
# keeps its client warm; a recording run starts cold and may rotate the client between cases.
LIVE_ASK_TIMEOUT_S: Final = 120.0
LIVE_CLOSE_TIMEOUT_S: Final = 15.0
FAST_TIER: Final = "haiku"  # ASSUMPTIONS 2026-10-23: the small/fast tier is the Haiku class


def fixture_dir(suite: str) -> Path:
    return FIXTURES_ROOT / suite


def recorded_cases(directory: Path) -> list[str]:
    """The case id of every fixture file in `directory`, sorted (a case recorded twice shows twice)."""
    cases = [str(json.loads(path.read_text(encoding="utf-8"))["case"]) for path in directory.glob("*.json")]
    return sorted(cases)


def drop_fixtures(directory: Path, cases: Iterable[str]) -> int:
    """Delete the fixtures of `cases` before they are recorded again (their keys may change). Returns how many."""
    wanted = set(cases)
    dropped = 0
    for path in sorted(directory.glob("*.json")):
        if json.loads(path.read_text(encoding="utf-8"))["case"] in wanted:
            path.unlink()
            dropped += 1
    return dropped


def write_fixture(directory: Path, request: AssistantRequest, reply: AssistantReply, *, case: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "case": case,
        "duration_ms": reply.duration_ms,
        "model": reply.model,
        "provider": reply.provider,
        "source": source,
        "structured": reply.structured,
        "text": reply.text,
    }
    path = directory / f"{fixture_key(request)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@dataclass
class Recorder:
    """An `Assistant` that passes every call to `inner` and, when `write`, records each answer for `case`."""

    inner: Assistant
    directory: Path
    write: bool
    source: str
    case: str = ""
    asked: list[tuple[str, AssistantRequest, AssistantReply]] = field(default_factory=list)

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return await self.inner.probe(timeout=timeout)

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        await self.inner.warm(profile, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        reply = await self.inner.ask(request, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))
        self.asked.append((self.case, request, reply))
        if self.write:
            write_fixture(self.directory, request, reply, case=self.case, source=self.source)
        return reply

    async def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        async for item in self.inner.stream(request, idle_timeout=idle_timeout):
            yield item

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        await self.inner.reset(kind, conversation=conversation, timeout=timeout)


@contextlib.asynccontextmanager
async def live_claude(sessions_dir: Path) -> AsyncIterator[ClaudeAssistant]:
    """The production adapter on the owner's `~/.local/bin/claude`. The caller scrubs the environment first
    (`tests.fakes.claude_client.scrub_live_env`), as `bots/*/app.py` does."""
    adapter = ClaudeAssistant(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH, sessions_dir=sessions_dir)
    try:
        yield adapter
    finally:
        await adapter.close(timeout=LIVE_CLOSE_TIMEOUT_S)


async def _children() -> set[int]:
    rows = await proctree.list_processes(env=proc.clean_env(path="/bin:/usr/bin"), timeout=10)
    return {row.pid for row in proctree.descendants(rows, os.getpid())}


async def kill_leftover_children() -> list[int]:
    """SIGKILL every process still below this test process (a `claude` child a close missed, and its tools).
    Returns their pids; the caller asserts the list is empty, so a leak is reported, and never left running.
    Listed twice: the `ps` that lists is itself a child, and shows in one listing only."""
    left = sorted(await _children() & await _children())
    if left:
        proctree.send(proctree.KillPlan(groups=(), pids=tuple(left)), signal.SIGKILL, own_pgid=os.getpgid(0))
    return left


def as_assistant(recorder: Recorder) -> Assistant:
    """mypy checks here that a `Recorder` is an `Assistant` (it goes into `deps.assistant`)."""
    return recorder
