"""Recorded model answers for the eval suites (CONVENTIONS §11; SPEC §8).

Replay: `FakeAssistant(fixtures=load_fixtures(directory), key=…)` answers each ask from `<directory>/<key>.json`
(`tests/fakes/assistant.py`).

Keys. Routing keeps `fixture_key` (the system prompt, the input, the model, the schema and the instructions). The
`settings` and `injection` suites use `owner_text_key(suite)`: the suite, the prompt version (the sha256 of the fixed
system prompt), the kind, the owner's own text and the output schema, and **not** the settings table their asks carry
as instructions. Every task that declares a setting changes that table, and keying on it made every recording stale
at each merge. This deviates on purpose from CONVENTIONS §11 ("a stale key fails"): a changed prompt, schema or
owner text still finds no recording and fails loudly, but a grown table does not. Instead each fixture stores the
sha256 of the table it was recorded against and the setting keys that table listed (`table_sha256`, `table_keys`),
and each suite's `test_settings_table_drift` prints which recordings predate the current table and which settings
were added or removed since. Rule: both suites are re-recorded in full at the P1 DoD gate (T47).

Record: a suite's `live` test wraps the real `ClaudeAssistant` (`live_claude`) in a `Recorder`. Every ask goes through
to the model; with `--record-llm` the answer is written to that file, tagged with the case that asked. `live_claude`
closes every client when its block ends, and `kill_leftover_children` then signals any process this test process
still has below it, so no `claude` child outlives the run.
"""

import contextlib
import hashlib
import json
import os
import signal
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tests.fakes.assistant import FixtureKey, fixture_key
from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH
from yordamchi.adapters.claude import proctree
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.core import proc
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)
from yordamchi.shared.settings.registry import KEY_PATTERN

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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def owner_text_key(suite: str) -> FixtureKey:
    """The replay key of the `settings` and `injection` suites (module docstring): the settings table is left out."""

    def key(request: AssistantRequest) -> str:
        material = json.dumps(
            {
                "suite": suite,
                "prompt_version": _sha256(request.system_prompt),
                "kind": request.kind,
                "owner_text": request.prompt,
                "schema": request.output_schema,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return _sha256(material)

    return key


def table_keys(instructions: str) -> list[str]:
    """The setting keys a settings table lists, one per row (`| key | type | … |`), in table order."""
    keys: list[str] = []
    for line in instructions.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and cells and KEY_PATTERN.fullmatch(cells[0]):
            keys.append(cells[0])
    return keys


def table_of(request: AssistantRequest) -> dict[str, JsonValue]:
    """What a fixture keeps of the settings table its ask carried: its sha256 and the keys it listed."""
    listed: list[JsonValue] = list(table_keys(request.instructions))
    return {"table_sha256": _sha256(request.instructions), "table_keys": listed}


def write_fixture(
    directory: Path,
    request: AssistantRequest,
    reply: AssistantReply,
    *,
    case: str,
    source: str,
    key: FixtureKey = fixture_key,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, JsonValue] = {
        "case": case,
        "duration_ms": reply.duration_ms,
        "model": reply.model,
        "provider": reply.provider,
        "source": source,
        "structured": reply.structured,
        "text": reply.text,
    }
    if key is not fixture_key:  # a suite keyed without the table records the table it saw (module docstring)
        data.update(table_of(request))
    path = directory / f"{key(request)}.json"
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
    key: FixtureKey = fixture_key
    asked: list[tuple[str, AssistantRequest, AssistantReply]] = field(default_factory=list)

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return await self.inner.probe(timeout=timeout)

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        await self.inner.warm(profile, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        reply = await self.inner.ask(request, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))
        self.asked.append((self.case, request, reply))
        if self.write:
            write_fixture(self.directory, request, reply, case=self.case, source=self.source, key=self.key)
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


# --- drift: recordings against an older settings table ----------------------------------------------------------
def fixture_tables(directory: Path) -> dict[str, Mapping[str, JsonValue]]:
    """Per case: the table metadata its fixture stored (`table_sha256`, `table_keys`)."""
    found: dict[str, Mapping[str, JsonValue]] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found[str(data["case"])] = {"table_sha256": data.get("table_sha256"), "table_keys": data.get("table_keys")}
    return found


def drift_report(
    suite: str, directory: Path, asks: Mapping[str, AssistantRequest], current_keys: Sequence[str]
) -> list[str]:
    """One line per recording made against another table than the case's replay ask carries now, naming the settings
    added and removed since; empty when every recording matches. `asks` is the replay ask of each recorded case."""
    lines: list[str] = []
    for case, stored in sorted(fixture_tables(directory).items()):
        request = asks.get(case)
        if request is None or stored.get("table_sha256") == table_of(request)["table_sha256"]:
            continue
        seen = stored.get("table_keys")
        before = {str(key) for key in seen} if isinstance(seen, list) else set()
        added, removed = sorted(set(current_keys) - before), sorted(before - set(current_keys))
        lines.append(f"[{suite}] {case}: recorded against an older settings table; added {added}, removed {removed}")
    return lines


def as_assistant(recorder: Recorder) -> Assistant:
    """mypy checks here that a `Recorder` is an `Assistant` (it goes into `deps.assistant`)."""
    return recorder
