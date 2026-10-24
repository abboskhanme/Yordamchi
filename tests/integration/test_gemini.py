"""`GeminiTranscriber` against replayed Interactions API payloads (synthetic; see `tests/fixtures/gemini/`).

Covers UC-05 (hints, vocabulary, no translation instruction for general models) and the SPEC §3.2.11 error mapping:
key failures, rate limit vs quota, timeouts, 5xx, and that only `core.retry` retries.
"""

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.integration.stt_harness import (
    GOOGLE_KEY,
    GOOGLE_KEY_2,
    LANGUAGES,
    OPENROUTER_KEY,
    GoogleServer,
    Recording,
    google_server,
    live_key,
    load,
    make_ogg_opus,
    make_wav,
    slow,
)
from yordamchi.adapters.gemini.stt import GeminiTranscriber
from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)

VOCABULARY = ("BonviHR", "HRMentor", "BonviHR", "  ", "Yordamchi")
INTERACTIONS = "/v1beta/interactions"


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff timing is core.retry's own test; here only the attempt count matters."""
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def google() -> AsyncIterator[GoogleServer]:
    async with google_server() as server:
        yield server


def adapter(google: GoogleServer, key: str = GOOGLE_KEY) -> GeminiTranscriber:
    return GeminiTranscriber(api_key=key, client_factory=google.client_factory)


async def transcribe(
    transcriber: GeminiTranscriber, *, model: str = "gemini-3.5-transcribe", timeout: float = 8.0
) -> object:
    return await transcriber.transcribe(
        make_ogg_opus(2000), mime="audio/ogg", languages=LANGUAGES, vocabulary=VOCABULARY, model=model, timeout=timeout
    )


# --- request shape -------------------------------------------------------------------------------------------------
async def test_transcribe_model_gets_hints_in_transcription_config(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_transcribe_ok"))
    audio = make_ogg_opus(2000)
    transcriber = adapter(google)
    transcript = await transcriber.transcribe(
        audio, mime="audio/ogg", languages=LANGUAGES, vocabulary=VOCABULARY, model="gemini-3.5-transcribe", timeout=8
    )
    await transcriber.aclose()

    [request] = google.requests
    assert request.url.path == INTERACTIONS
    assert request.headers["x-goog-api-key"] == GOOGLE_KEY
    [body] = google.bodies()
    assert body["model"] == "gemini-3.5-transcribe"
    assert body["store"] is False
    assert body["generation_config"] == {
        "transcription_config": {
            "mode": "smart",
            "language_codes": ["uz-UZ", "ru-RU", "en-US"],
            "custom_vocabulary": ["BonviHR", "HRMentor", "Yordamchi"],
        }
    }
    [turn] = body["input"]
    [part] = turn["content"]
    assert part["type"] == "audio"
    assert part["mime_type"] == "audio/ogg"  # Telegram OGG/Opus goes in as it is
    assert base64.b64decode(part["data"]) == audio
    assert "system_instruction" not in body

    assert transcript.text == load("gemini", "interaction_transcribe_ok").body["steps"][0]["content"][0]["text"]
    assert "потом проверим логи" in transcript.text  # Cyrillic kept, nothing translated
    assert transcript.provider == "google"
    assert transcript.provider_path == "interactions"
    assert transcript.model == "gemini-3.5-transcribe"
    assert transcript.audio_ms == 2000
    assert transcript.latency_ms >= 0


async def test_vocabulary_is_capped_at_the_api_limit_of_1000(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_transcribe_ok"))
    terms = [f"Term{index}" for index in range(1200)]
    await adapter(google).transcribe(
        b"OggS", mime="audio/ogg", languages=(), vocabulary=terms, model="models/gemini-3.5-transcribe", timeout=8
    )
    [body] = google.bodies()
    config = body["generation_config"]["transcription_config"]
    assert config["custom_vocabulary"] == terms[:1000]
    assert "language_codes" not in config  # no hints → the model detects the language
    assert body["model"] == "gemini-3.5-transcribe"


async def test_general_model_gets_the_instruction_hints_and_minimal_thinking(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_general_ok"))
    transcript = await adapter(google).transcribe(
        make_wav(500),
        mime="audio/x-wav",
        languages=LANGUAGES,
        vocabulary=[f"T{index}" for index in range(300)],
        model="gemini-3.8-flash",
        timeout=8,
    )
    [body] = google.bodies()
    assert body["generation_config"] == {"thinking_level": "minimal"}
    assert body["store"] is False
    instruction = body["system_instruction"]
    assert "Never translate" in instruction
    assert "Latin script (oʻ, gʻ, sh, ch)" in instruction
    assert "Russian in Cyrillic" in instruction
    text_part, audio_part = body["input"][0]["content"]
    assert "Expected languages: uz-UZ, ru-RU, en-US." in text_part["text"]
    assert "T199." in text_part["text"]
    assert "T200" not in text_part["text"]  # prompt-based paths take the first 200 terms
    assert audio_part["mime_type"] == "audio/wav"
    # The thought step is ignored and the «…» the model wrapped around the text is removed.
    assert transcript.text.startswith("Bugun BonviHR")
    assert transcript.text.endswith("oʻtamiz.")
    assert transcript.audio_ms == 500


async def test_rejected_thinking_level_falls_back_to_low_and_is_remembered(google: GoogleServer) -> None:
    google.script(load("gemini", "error_400_thinking_level"), load("gemini", "interaction_general_ok"))
    transcriber = adapter(google)
    await transcribe(transcriber, model="gemini-3.8-flash")
    await transcribe(transcriber, model="gemini-3.8-flash")
    levels = [body["generation_config"]["thinking_level"] for body in google.bodies()]
    assert levels == ["minimal", "low", "low"]


# --- error mapping (SPEC §3.2.11) ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_400_api_key_invalid", AuthFailed, False, 1),
        ("error_403_permission_denied", AuthFailed, False, 1),
        ("error_404_model_not_found", ProviderUnavailable, False, 1),
        ("error_429_quota_per_day", QuotaExhausted, False, 1),
        ("error_500_internal", ProviderUnavailable, True, 3),
        ("error_503_unavailable", ProviderUnavailable, True, 3),
        ("interaction_failed", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    google: GoogleServer, fixture: str, expected: type[ExternalError], retryable: bool, requests: int
) -> None:
    google.script(load("gemini", fixture))
    with pytest.raises(expected) as caught:
        await transcribe(adapter(google))
    assert caught.value.retryable is retryable
    # 3 = core.retry's attempts; the SDK's own retries (3 more per attempt by default) are off.
    assert len(google.requests) == requests


async def test_rate_limit_longer_than_the_budget_surfaces_with_its_retry_delay(google: GoogleServer) -> None:
    google.script(load("gemini", "error_429_rate_per_minute"))
    with pytest.raises(RateLimited) as caught:
        await transcribe(adapter(google), timeout=3)
    assert caught.value.retry_after == 7.0  # RetryInfo.retryDelay "7s"
    assert len(google.requests) == 1


async def test_rate_limit_falls_back_to_the_retry_after_header(google: GoogleServer) -> None:
    def drop_retry_info(body: dict[str, object]) -> None:
        error = body["error"]
        assert isinstance(error, dict)
        error["details"] = []

    google.script(load("gemini", "error_429_rate_per_minute").with_body(drop_retry_info).with_headers(retry_after="12"))
    with pytest.raises(RateLimited) as caught:
        await transcribe(adapter(google), timeout=3)
    assert caught.value.retry_after == 12.0


async def test_short_rate_limit_is_waited_out_and_retried(google: GoogleServer) -> None:
    def zero_delay(body: dict[str, object]) -> None:
        error = body["error"]
        assert isinstance(error, dict)
        details = error["details"]
        assert isinstance(details, list)
        details[-1]["retryDelay"] = "0s"

    google.script(
        load("gemini", "error_429_rate_per_minute").with_body(zero_delay), load("gemini", "interaction_transcribe_ok")
    )
    transcript = await transcribe(adapter(google))
    assert getattr(transcript, "text", "").startswith("Bugun")
    assert len(google.requests) == 2


async def test_network_errors_are_retried_then_surface_as_provider_unavailable(google: GoogleServer) -> None:
    google.script(httpx.ConnectError("connection refused"))
    with pytest.raises(ProviderUnavailable) as caught:
        await transcribe(adapter(google))
    assert caught.value.retryable is True
    assert len(google.requests) == 3


async def test_a_slow_answer_times_out_within_the_call_timeout(google: GoogleServer) -> None:
    async def hang(request: httpx.Request) -> Recording:
        del request
        return await slow(30, load("gemini", "interaction_transcribe_ok"))

    google.script(hang)
    started = time.monotonic()
    with pytest.raises(CallTimedOut):
        await transcribe(adapter(google), timeout=0.3)
    assert time.monotonic() - started < 2.0


async def test_sdk_client_side_timeout_maps_to_call_timed_out(google: GoogleServer) -> None:
    google.script(httpx.ReadTimeout("read timed out"))
    with pytest.raises(CallTimedOut):
        await transcribe(adapter(google), timeout=0.5)


# --- keys ----------------------------------------------------------------------------------------------------------
async def test_key_source_is_read_per_call_and_a_new_key_gets_a_new_client(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_transcribe_ok"))
    keys = iter([GOOGLE_KEY, GOOGLE_KEY, GOOGLE_KEY_2])

    async def current_key() -> str:
        return next(keys)

    transcriber = GeminiTranscriber(api_key=current_key, client_factory=google.client_factory)
    for _ in range(3):
        await transcribe(transcriber)
    await transcriber.aclose()
    assert [request.headers["x-goog-api-key"] for request in google.requests] == [GOOGLE_KEY, GOOGLE_KEY, GOOGLE_KEY_2]
    assert len(google.clients) == 2


async def test_an_empty_key_fails_auth_without_a_request(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_transcribe_ok"))
    with pytest.raises(AuthFailed):
        await transcribe(adapter(google, key="  "))
    assert google.requests == []


async def test_validate_key_lists_available_and_missing_models(google: GoogleServer) -> None:
    google.script(load("gemini", "model_get_ok"), load("gemini", "error_404_model_not_found"))
    check = await adapter(google).validate_key(
        f" {GOOGLE_KEY} ", models=("gemini-3.5-transcribe", "gemini-9-nonexistent"), timeout=10
    )
    assert check.provider == "google"
    assert check.available_models == ("gemini-3.5-transcribe",)
    assert check.missing_models == ("gemini-9-nonexistent",)
    assert not check.ok
    assert check.is_free_tier is None  # Google does not say
    assert [request.url.path for request in google.requests] == [
        "/v1beta/models/gemini-3.5-transcribe",
        "/v1beta/models/gemini-9-nonexistent",
    ]
    assert {request.headers["x-goog-api-key"] for request in google.requests} == {GOOGLE_KEY}


async def test_validate_key_maps_an_invalid_key_to_auth_failed(google: GoogleServer) -> None:
    google.script(load("gemini", "error_400_api_key_invalid"))
    with pytest.raises(AuthFailed):
        await adapter(google).validate_key(GOOGLE_KEY, models=("gemini-3.5-transcribe",), timeout=10)
    assert len(google.requests) == 1


async def test_validate_key_maps_quota_and_network_failures(google: GoogleServer) -> None:
    google.script(load("gemini", "error_429_quota_per_day"))
    with pytest.raises(QuotaExhausted):
        await adapter(google).validate_key(GOOGLE_KEY, models=("gemini-3.5-transcribe",), timeout=10)
    google.replies.clear()
    google.script(httpx.ConnectError("no route"))
    with pytest.raises(ProviderUnavailable):
        await adapter(google).validate_key(GOOGLE_KEY, models=("gemini-3.5-transcribe",), timeout=10)


async def test_validate_key_never_sends_another_vendors_key_to_google(google: GoogleServer) -> None:
    google.script(load("gemini", "model_get_ok"))
    for key in (OPENROUTER_KEY, "not a key", GOOGLE_KEY + " and more"):
        with pytest.raises(AuthFailed):
            await adapter(google).validate_key(key, models=("gemini-3.5-transcribe",), timeout=10)
    assert google.requests == []


# --- logging (CONVENTIONS §7: ids, codes, durations; never content) ------------------------------------------------
async def test_logs_carry_outcome_and_duration_but_no_key_or_transcript(
    google: GoogleServer, caplog: pytest.LogCaptureFixture
) -> None:
    google.script(load("gemini", "error_503_unavailable"), load("gemini", "interaction_transcribe_ok"))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.gemini.stt"):
        transcript = await transcribe(adapter(google))
    events = [record.getMessage() for record in caplog.records]
    assert events == ["stt_attempt_failed", "stt_call_finished"]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert "duration_ms" in fields[1]
    assert "interactions/synthetic-transcribe-0001" in fields[1]  # the request id
    text = getattr(transcript, "text", "")
    assert all(GOOGLE_KEY not in line and text not in line for line in fields)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_GOOGLE_API_KEY) -----------------------------------------
@pytest.mark.live
async def test_live_google_key_validates_and_transcribes_silence() -> None:
    key = live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY")
    transcriber = GeminiTranscriber(api_key=key)
    try:
        check = await transcriber.validate_key(key, models=("gemini-3.5-transcribe",), timeout=10)
        assert check.ok
        transcript = await transcriber.transcribe(
            make_wav(1000),
            mime="audio/wav",
            languages=LANGUAGES,
            vocabulary=("BonviHR",),
            model="gemini-3.5-transcribe",
            timeout=15,
        )
        assert isinstance(transcript.text, str)
        assert transcript.provider_path == "interactions"
    finally:
        await transcriber.aclose()
        await asyncio.sleep(0)
