import pytest

from tests.fakes.assistant import FakeAssistant, fixture_key
from yordamchi.core.errors import RateLimited
from yordamchi.ports.assistant import Assistant, AssistantDelta, AssistantReply, AssistantRequest

TIMEOUT = 5.0
REQUEST = AssistantRequest(
    kind="report", system_prompt="system text", prompt="HRMentor qanday?", model="sonnet", settings_version=3
)


class AssistantContract:
    @pytest.fixture
    def assistant(self) -> Assistant:
        raise NotImplementedError

    async def test_probe_reports_a_status(self, assistant: Assistant) -> None:
        assert (await assistant.probe(timeout=TIMEOUT)).installed

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


class TestFakeAssistant(AssistantContract):
    @pytest.fixture
    def assistant(self) -> Assistant:
        return FakeAssistant()

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
