"""`GeminiAssistant`, the "zaxira model" (Q4; T72), on replayed Interactions API payloads (`tests/fixtures/gemini/`,
synthetic, shared with the STT adapter): the request it sends, the answer and usage it returns, and the error mapping
(only `core.retry` retries)."""

from collections.abc import AsyncIterator

import pytest

from tests.integration.stt_harness import GOOGLE_KEY, GoogleServer, google_server, load
from yordamchi.adapters.gemini.assistant import GeminiAssistant
from yordamchi.core import retry
from yordamchi.core.errors import AuthFailed, ExternalError, ProviderUnavailable, QuotaExhausted, RateLimited
from yordamchi.ports.assistant import AssistantDelta, AssistantReply, AssistantRequest

TIMEOUT = 5.0
INTERACTIONS = "/v1beta/interactions"
SYSTEM = "Fixed fallback role (test data)."
INSTRUCTIONS = "Current instructions from the owner: answer in Uzbek."


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def google() -> AsyncIterator[GoogleServer]:
    async with google_server() as server:
        yield server


def _request(**overrides: object) -> AssistantRequest:
    values: dict[str, object] = {
        "kind": "qa",
        "system_prompt": SYSTEM,
        "prompt": "Kvant kompyuter nima?",
        "model": "models/gemini-3.8-flash",
        "settings_version": 1,
        "instructions": INSTRUCTIONS,
        "prompt_version": 4,
    }
    values.update(overrides)
    return AssistantRequest(**values)  # type: ignore[arg-type]


def _adapter(google: GoogleServer, key: str = GOOGLE_KEY) -> GeminiAssistant:
    return GeminiAssistant(api_key=key, client_factory=google.client_factory)


async def test_an_answer_carries_the_text_model_and_usage(google: GoogleServer) -> None:
    google.script(
        load("gemini", "interaction_general_ok").with_body(
            lambda body: body.update({"usage": {"total_input_tokens": 120, "total_output_tokens": 40}})
        )
    )
    reply = await _adapter(google).ask(_request(), timeout=TIMEOUT)
    assert reply.provider == "google"
    assert reply.model == "gemini-3.8-flash"
    assert "BonviHR" in reply.text
    assert reply.usage == {"input_tokens": 120, "output_tokens": 40}
    assert reply.prompt_version == 4
    assert reply.cost_usd_micros == 0  # Google does not say; the caller records the call as paid API usage

    (body,) = google.bodies()
    assert google.requests[0].url.path == INTERACTIONS
    assert body["model"] == "gemini-3.8-flash"
    assert body["system_instruction"] == f"{SYSTEM}\n\n{INSTRUCTIONS}"  # the fixed part, then this ask's block
    # the SDK sends the text part as one user step
    assert body["input"] == [{"type": "user_input", "content": [{"type": "text", "text": "Kvant kompyuter nima?"}]}]
    assert body["store"] is False
    assert body["generation_config"] == {"thinking_level": "low"}


async def test_a_rejected_thinking_level_is_sent_again_without_it(google: GoogleServer) -> None:
    google.script(load("gemini", "error_400_thinking_level"), load("gemini", "interaction_general_ok"))
    assistant = _adapter(google)
    await assistant.ask(_request(), timeout=TIMEOUT)
    first, second = google.bodies()
    assert "generation_config" in first
    assert "generation_config" not in second
    await assistant.ask(_request(), timeout=TIMEOUT)
    assert "generation_config" not in google.bodies()[-1]  # remembered for the model


@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_400_api_key_invalid", AuthFailed, False, 1),
        ("error_429_quota_per_day", QuotaExhausted, False, 1),
        ("error_503_unavailable", ProviderUnavailable, True, 3),
        ("interaction_failed", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors(
    google: GoogleServer, fixture: str, expected: type[ExternalError], retryable: bool, requests: int
) -> None:
    google.script(load("gemini", fixture))
    with pytest.raises(expected) as caught:
        await _adapter(google).ask(_request(), timeout=TIMEOUT)
    assert caught.value.retryable is retryable
    assert len(google.requests) == requests


async def test_a_rate_limit_is_rate_limited(google: GoogleServer) -> None:
    google.script(load("gemini", "error_429_rate_per_minute"))
    with pytest.raises(RateLimited):
        await _adapter(google).ask(_request(), timeout=TIMEOUT)


async def test_an_empty_answer_is_a_provider_failure(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_general_ok").with_body(lambda body: body.update({"steps": []})))
    with pytest.raises(ProviderUnavailable):
        await _adapter(google).ask(_request(), timeout=TIMEOUT)


async def test_stream_yields_the_text_once_then_the_reply(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_general_ok"))
    items = [item async for item in _adapter(google).stream(_request(), idle_timeout=TIMEOUT)]
    assert isinstance(items[0], AssistantDelta)
    assert isinstance(items[1], AssistantReply)
    assert items[0].text == items[1].text


async def test_probe_reports_a_missing_key_and_nothing_is_sent(google: GoogleServer) -> None:
    assert (await _adapter(google, key=" ").probe(timeout=TIMEOUT)).logged_in is False
    assert (await _adapter(google).probe(timeout=TIMEOUT)).logged_in is True
    with pytest.raises(AuthFailed):
        await _adapter(google, key="").ask(_request(), timeout=TIMEOUT)
    assert google.requests == []


async def test_warm_and_reset_are_no_ops(google: GoogleServer) -> None:
    assistant = _adapter(google)
    await assistant.warm(_request().profile, timeout=TIMEOUT)
    await assistant.reset("qa", conversation=None, timeout=TIMEOUT)
    await assistant.aclose()
    assert google.requests == []
