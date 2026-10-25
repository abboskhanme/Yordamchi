from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fakes.assistant import FakeAssistant, fixture_key
from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH, scrub_live_env
from tests.integration.test_claude_assistant import Factory, _asks, fake_cli
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.core.errors import RateLimited
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.assistant import Assistant, AssistantDelta, AssistantReply, AssistantRequest

TIMEOUT = 5.0
# A probe runs a fake `claude` script written fresh into tmp_path; macOS scans a new executable on its first run
# (XProtect), which took over 5 s while a VM loaded the Mac (2026-10-25). The bound is the harness's, not a latency
# claim: the production probe timeout is a setting.
PROBE_TIMEOUT = 30.0
LIVE_TIMEOUT = 60.0
REQUEST = AssistantRequest(
    kind="report", system_prompt="system text", prompt="HRMentor qanday?", model="sonnet", settings_version=3
)
ROUTING_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "project": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "candidates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["project", "confidence", "candidates"],
    "additionalProperties": False,
}
ROUTING = AssistantRequest(
    kind="routing",
    system_prompt="Route the message to a project; answer only through the structured output.",
    prompt="nomzodlar formasi ishlamayapti",
    model="haiku",
    settings_version=3,
    output_schema=ROUTING_SCHEMA,
    instructions="Current instructions from the owner: the projects are BonviHR (hiring) and HRMentor (mentoring).",
)


class AssistantContract:
    @pytest.fixture
    def assistant(self) -> Assistant:
        raise NotImplementedError

    async def test_probe_reports_a_status(self, assistant: Assistant) -> None:
        assert (await assistant.probe(timeout=PROBE_TIMEOUT)).installed

    async def test_ask_returns_text_and_names_the_provider(self, assistant: Assistant) -> None:
        reply = await assistant.ask(REQUEST, timeout=TIMEOUT)
        assert reply.text
        assert reply.provider

    async def test_stream_yields_deltas_then_exactly_one_reply(self, assistant: Assistant) -> None:
        items = [item async for item in assistant.stream(REQUEST, idle_timeout=TIMEOUT)]
        assert isinstance(items[-1], AssistantReply)
        assert all(isinstance(item, AssistantDelta) for item in items[:-1])

    async def test_reset_is_accepted(self, assistant: Assistant) -> None:
        await assistant.reset("qa", conversation=None, timeout=TIMEOUT)

    async def test_warm_then_ask(self, assistant: Assistant) -> None:
        await assistant.warm(REQUEST.profile, timeout=TIMEOUT)
        reply = await assistant.ask(REQUEST, timeout=TIMEOUT)
        assert reply.text
        assert reply.prompt_version == REQUEST.prompt_version

    async def test_a_structured_request_gets_a_structured_value(self, assistant: Assistant) -> None:
        reply = await assistant.ask(ROUTING, timeout=TIMEOUT)
        assert isinstance(reply.structured, dict)
        assert {"project", "confidence", "candidates"} <= set(reply.structured)


class TestFakeAssistant(AssistantContract):
    @pytest.fixture
    def assistant(self) -> Assistant:
        fake = FakeAssistant()
        fake.reply_with("routing", {"project": "BonviHR", "confidence": 0.9, "candidates": ["BonviHR"]})
        return fake

    async def test_a_queued_dict_is_a_structured_reply(self) -> None:
        fake = FakeAssistant()
        fake.reply_with("routing", {"project": None, "confidence": 0.3, "candidates": ["A", "B"]})
        reply = await fake.ask(ROUTING, timeout=TIMEOUT)
        assert reply.structured == {"project": None, "confidence": 0.3, "candidates": ["A", "B"]}

    def test_fixture_keys_change_with_the_instructions(self) -> None:
        changed = AssistantRequest(
            kind="report",
            system_prompt="system text",
            prompt="HRMentor qanday?",
            model="sonnet",
            settings_version=3,
            instructions="Current instructions from the owner: answer in English.",
        )
        assert fixture_key(REQUEST) != fixture_key(changed)

    async def test_queued_replies_and_errors_play_in_order(self) -> None:
        assistant = FakeAssistant()
        assistant.reply_with("report", "birinchi", RateLimited(provider="claude"))
        assert (await assistant.ask(REQUEST, timeout=TIMEOUT)).text == "birinchi"
        with pytest.raises(RateLimited):
            await assistant.ask(REQUEST, timeout=TIMEOUT)
        assert len(assistant.requests) == 2

    async def test_a_missing_recording_fails_loudly(self) -> None:
        assistant = FakeAssistant(fixtures={})
        with pytest.raises(LookupError, match="record-llm"):
            await assistant.ask(REQUEST, timeout=TIMEOUT)

    def test_fixture_keys_change_with_the_prompt(self) -> None:
        changed = AssistantRequest(
            kind="report", system_prompt="system text v2", prompt="HRMentor qanday?", model="sonnet", settings_version=3
        )
        assert fixture_key(REQUEST) != fixture_key(changed)


class TestClaudeAssistantReplayed(AssistantContract):
    """The production adapter, offline: clients replay `tests/fixtures/claude/streams/assistant_*.jsonl`."""

    @pytest.fixture
    async def assistant(self, tmp_path: Path) -> AsyncIterator[Assistant]:
        factory = Factory(
            _asks("assistant_report", 4),
            by_kind={"routing": _asks("assistant_routing", 4), "report": _asks("assistant_report", 4)},
        )
        adapter = ClaudeAssistant(
            cli_path=fake_cli(tmp_path),
            search_path="/usr/bin:/bin",
            sessions_dir=tmp_path / "sessions",
            client_factory=factory,
        )
        yield adapter
        await adapter.close(timeout=TIMEOUT)


@pytest.mark.live
@pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
class TestClaudeAssistant(AssistantContract):
    """The same contract on the real `~/.local/bin/claude`; `pytest -m live tests/contract/test_assistant.py`."""

    @pytest.fixture
    async def assistant(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Assistant]:
        scrub_live_env(monkeypatch)
        adapter = ClaudeAssistant(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH, sessions_dir=tmp_path / "sessions")
        yield adapter
        await adapter.close(timeout=15)

    @pytest.fixture(autouse=True)
    def _live_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cold spawn takes seconds; the contract's 5 s bound is for the fakes."""
        monkeypatch.setattr(f"{__name__}.TIMEOUT", LIVE_TIMEOUT)
