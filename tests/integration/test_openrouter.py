"""`OpenRouterTranscriber` against a local aiohttp server replaying synthetic payloads (`tests/fixtures/openrouter/`).

Covers SPEC §3.2.11 step 5 (chat-completions path with hints, WAV via ffmpeg, the hint-less fallback) and step 6
(401/403 auth, 402 credits, 429 rate vs daily cap, 5xx, 200-with-error, timeouts), plus key validation.
"""

import base64
import logging
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from tests.integration.stt_harness import (
    GOOGLE_KEY,
    LANGUAGES,
    OPENROUTER_BASE,
    OPENROUTER_KEY,
    OpenRouterServer,
    Recording,
    fake_ffmpeg,
    ffmpeg_argv,
    live_key,
    load,
    make_ogg_opus,
    make_wav,
    openrouter_server,
    slow,
    streamed_wav,
)
from yordamchi.adapters.openrouter.stt import FALLBACK_TRANSCRIPTION_MODEL, OpenRouterTranscriber
from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    NotInstalled,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from yordamchi.ports.speech import Transcript

CHAT = "/api/v1/chat/completions"
TRANSCRIPTIONS = "/api/v1/audio/transcriptions"
KEY = "/api/v1/key"
MODEL = "google/gemini-3.8-flash"
VOCABULARY = ("BonviHR", "HRMentor")


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def server() -> AsyncIterator[OpenRouterServer]:
    async with openrouter_server() as running:
        yield running


@pytest.fixture
def ffmpeg(tmp_path: Path) -> Path:
    return fake_ffmpeg(tmp_path / "bin", output=streamed_wav(1500))


def adapter(server: OpenRouterServer, ffmpeg: Path | None, key: str = OPENROUTER_KEY) -> OpenRouterTranscriber:
    return OpenRouterTranscriber(api_key=key, ffmpeg=ffmpeg, base_url=OPENROUTER_BASE, session=server.session)


async def transcribe(transcriber: OpenRouterTranscriber, *, timeout: float = 8.0) -> Transcript:
    return await transcriber.transcribe(
        make_ogg_opus(1500), mime="audio/ogg", languages=LANGUAGES, vocabulary=VOCABULARY, model=MODEL, timeout=timeout
    )


# --- request shape -------------------------------------------------------------------------------------------------
async def test_chat_path_sends_wav_with_hints_in_the_prompt(server: OpenRouterServer, ffmpeg: Path) -> None:
    server.script(CHAT, load("openrouter", "chat_ok"))
    transcript = await transcribe(adapter(server, ffmpeg))

    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-f") + 1] == "wav"

    [request] = server.seen
    assert request.method == "POST"
    assert request.path == CHAT
    assert request.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    body = request.json
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["reasoning"] == {"effort": "minimal", "exclude": True}
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "Never translate" in system["content"]
    assert "Russian in Cyrillic" in system["content"]
    text_part, audio_part = user["content"]
    assert text_part["text"] == (
        "Transcribe this audio. Expected languages: uz-UZ, ru-RU, en-US. Spell these terms exactly: BonviHR, HRMentor."
    )
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["format"] == "wav"
    wav = base64.b64decode(audio_part["input_audio"]["data"])
    assert wav[:4] == b"RIFF"
    assert int.from_bytes(wav[4:8], "little") == len(wav) - 8  # ffmpeg's placeholder sizes were fixed

    assert transcript.text.startswith("Bugun BonviHR")
    assert transcript.provider == "openrouter"
    assert transcript.provider_path == "chat_completions"
    assert transcript.model == MODEL
    assert transcript.audio_ms == 1500


async def test_wav_input_needs_no_ffmpeg(server: OpenRouterServer) -> None:
    server.script(CHAT, load("openrouter", "chat_ok"))
    wav = make_wav(700)
    transcript = await adapter(server, None).transcribe(
        wav, mime="audio/wav", languages=(), vocabulary=(), model=MODEL, timeout=8
    )
    [request] = server.seen
    text_part, audio_part = request.json["messages"][1]["content"]
    assert text_part["text"] == "Transcribe this audio."
    assert base64.b64decode(audio_part["input_audio"]["data"]) == wav
    assert transcript.audio_ms == 700


async def test_ogg_without_ffmpeg_is_not_installed_and_sends_nothing(server: OpenRouterServer, tmp_path: Path) -> None:
    server.script(CHAT, load("openrouter", "chat_ok"))
    for missing in (None, tmp_path / "no-such-ffmpeg"):
        with pytest.raises(NotInstalled) as caught:
            await transcribe(adapter(server, missing))
        assert caught.value.params == {"binary": "ffmpeg"}
    assert server.seen == []


async def test_unreadable_audio_fails_without_retry(server: OpenRouterServer, tmp_path: Path) -> None:
    server.script(CHAT, load("openrouter", "chat_ok"))
    broken = fake_ffmpeg(tmp_path / "bin", fail=True)
    with pytest.raises(ProviderUnavailable) as caught:
        await transcribe(adapter(server, broken))
    assert caught.value.retryable is False
    assert caught.value.params == {"provider": "ffmpeg"}
    assert server.seen == []


async def test_a_route_without_audio_falls_back_to_the_transcriptions_endpoint(
    server: OpenRouterServer, ffmpeg: Path
) -> None:
    server.script(CHAT, load("openrouter", "error_404_no_audio_endpoint"))
    server.script(TRANSCRIPTIONS, load("openrouter", "transcriptions_ok"))
    transcript = await transcribe(adapter(server, ffmpeg))
    assert [request.path for request in server.seen] == [CHAT, TRANSCRIPTIONS]
    fallback = server.seen[1].json
    assert fallback["model"] == FALLBACK_TRANSCRIPTION_MODEL
    assert fallback["input_audio"]["format"] == "wav"
    assert set(fallback) == {"model", "input_audio"}  # the endpoint takes no hints
    assert transcript.provider_path == "audio_transcriptions"
    assert transcript.model == FALLBACK_TRANSCRIPTION_MODEL
    assert transcript.text.startswith("Bugun")


async def test_a_model_rejecting_reasoning_is_asked_again_without_it_and_remembered(
    server: OpenRouterServer, ffmpeg: Path
) -> None:
    server.script(CHAT, load("openrouter", "error_400_reasoning"), load("openrouter", "chat_ok"))
    transcriber = adapter(server, ffmpeg)
    await transcribe(transcriber)
    await transcribe(transcriber)
    assert ["reasoning" in request.json for request in server.seen] == [True, False, False]
    assert server.requests_to(TRANSCRIPTIONS) == []


# --- error mapping (SPEC §3.2.11 step 6) ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_401", AuthFailed, False, 1),
        ("error_402_credits", QuotaExhausted, False, 1),
        ("error_403_moderation", ProviderUnavailable, False, 1),
        ("error_429_free_per_day", QuotaExhausted, False, 1),
        ("error_502", ProviderUnavailable, True, 3),
        ("chat_200_error", ProviderUnavailable, True, 3),
        ("chat_finish_error", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    server: OpenRouterServer, ffmpeg: Path, fixture: str, expected: type[ExternalError], retryable: bool, requests: int
) -> None:
    server.script(CHAT, load("openrouter", fixture))
    with pytest.raises(expected) as caught:
        await transcribe(adapter(server, ffmpeg))
    assert caught.value.retryable is retryable
    assert len(server.requests_to(CHAT)) == requests
    assert server.requests_to(TRANSCRIPTIONS) == []


async def test_a_plain_403_is_an_auth_failure(server: OpenRouterServer, ffmpeg: Path) -> None:
    unauthorized = load("openrouter", "error_401")
    server.script(CHAT, Recording(403, unauthorized.headers, {"error": {"code": 403, "message": "Key disabled"}}))
    with pytest.raises(AuthFailed):
        await transcribe(adapter(server, ffmpeg))


async def test_rate_limit_longer_than_the_budget_surfaces_with_retry_after(
    server: OpenRouterServer, ffmpeg: Path
) -> None:
    server.script(CHAT, load("openrouter", "error_429_rate"))
    with pytest.raises(RateLimited) as caught:
        await transcribe(adapter(server, ffmpeg), timeout=5)
    assert caught.value.retry_after == 30.0
    assert len(server.requests_to(CHAT)) == 1


async def test_in_flight_budget_402_waits_for_retry_after_and_retries(server: OpenRouterServer, ffmpeg: Path) -> None:
    server.script(CHAT, load("openrouter", "error_402_in_flight"), load("openrouter", "chat_ok"))
    transcript = await transcribe(adapter(server, ffmpeg))
    assert transcript.text.startswith("Bugun")
    assert len(server.requests_to(CHAT)) == 2


async def test_a_slow_answer_times_out_within_the_call_timeout(server: OpenRouterServer, ffmpeg: Path) -> None:
    async def hang() -> Recording:
        return await slow(30, load("openrouter", "chat_ok"))

    server.script(CHAT, hang)
    started = time.monotonic()
    with pytest.raises(CallTimedOut):
        await transcribe(adapter(server, ffmpeg), timeout=0.5)
    assert time.monotonic() - started < 2.0


async def test_an_unreachable_server_is_retryable_provider_unavailable(ffmpeg: Path, tmp_path: Path) -> None:
    import aiohttp

    session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(tmp_path / "nobody.sock")))
    try:
        transcriber = OpenRouterTranscriber(
            api_key=OPENROUTER_KEY, ffmpeg=ffmpeg, base_url=OPENROUTER_BASE, session=session
        )
        with pytest.raises(ProviderUnavailable) as caught:
            await transcribe(transcriber)
        assert caught.value.retryable is True
    finally:
        await session.close()


async def test_an_empty_key_fails_auth_without_a_request(server: OpenRouterServer, ffmpeg: Path) -> None:
    server.script(CHAT, load("openrouter", "chat_ok"))
    with pytest.raises(AuthFailed):
        await transcribe(adapter(server, ffmpeg, key=""))
    assert server.seen == []


# --- key validation (SPEC §3.2.11 step 2) --------------------------------------------------------------------------
async def test_validate_key_reads_limits_and_model_endpoints(server: OpenRouterServer) -> None:
    server.script(KEY, load("openrouter", "key_ok"))
    server.script("/api/v1/models/google/gemini-3.8-flash/endpoints", load("openrouter", "endpoints_ok"))
    server.script("/api/v1/models/google/gemini-3.8-flash-tts/endpoints", load("openrouter", "endpoints_empty"))
    server.script("/api/v1/models/google/gemini-0-gone/endpoints", load("openrouter", "endpoints_404"))
    check = await adapter(server, None).validate_key(
        OPENROUTER_KEY,
        models=("google/gemini-3.8-flash", "google/gemini-3.8-flash-tts", "google/gemini-0-gone", "no-slash"),
        timeout=10,
    )
    assert check.provider == "openrouter"
    assert check.available_models == ("google/gemini-3.8-flash",)
    assert check.missing_models == ("google/gemini-3.8-flash-tts", "google/gemini-0-gone", "no-slash")
    assert check.is_free_tier is False
    assert check.limit_usd == Decimal(40)
    assert check.usage_usd == Decimal("2.5")
    assert check.limit_remaining_usd == Decimal("37.5")
    assert {request.headers["Authorization"] for request in server.seen} == {f"Bearer {OPENROUTER_KEY}"}
    assert all(request.method == "GET" for request in server.seen)


async def test_validate_key_reports_a_free_tier_key_without_a_limit(server: OpenRouterServer) -> None:
    server.script(KEY, load("openrouter", "key_free"))
    check = await adapter(server, None).validate_key(OPENROUTER_KEY, models=(), timeout=10)
    assert check.is_free_tier is True
    assert check.limit_usd is None
    assert check.ok


async def test_validate_key_maps_a_rejected_key_to_auth_failed(server: OpenRouterServer) -> None:
    server.script(KEY, load("openrouter", "error_401"))
    with pytest.raises(AuthFailed):
        await adapter(server, None).validate_key(OPENROUTER_KEY, models=(MODEL,), timeout=10)
    assert [request.path for request in server.seen] == [KEY]


async def test_validate_key_never_sends_another_vendors_key(server: OpenRouterServer) -> None:
    server.script(KEY, load("openrouter", "key_ok"))
    for key in (GOOGLE_KEY, "sk-or-v1-short", "hello"):
        with pytest.raises(AuthFailed):
            await adapter(server, None).validate_key(key, models=(MODEL,), timeout=10)
    assert server.seen == []


# --- lifecycle and logging -----------------------------------------------------------------------------------------
async def test_the_adapter_owns_and_closes_its_session_when_none_is_given(ffmpeg: Path) -> None:
    transcriber = OpenRouterTranscriber(api_key=OPENROUTER_KEY, ffmpeg=ffmpeg)
    session = await transcriber._http()
    await transcriber.aclose()
    assert session.closed


async def test_logs_carry_outcome_and_duration_but_no_key_or_transcript(
    server: OpenRouterServer, ffmpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server.script(CHAT, load("openrouter", "error_502"), load("openrouter", "chat_ok"))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.openrouter.stt"):
        transcript = await transcribe(adapter(server, ffmpeg))
    assert [record.getMessage() for record in caplog.records] == ["stt_attempt_failed", "stt_call_finished"]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert "'status': 502" in fields[0]
    assert "gen-synthetic-0001" in fields[1]
    assert all(OPENROUTER_KEY not in line and transcript.text not in line for line in fields)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_OPENROUTER_API_KEY) -------------------------------------
@pytest.mark.live
async def test_live_openrouter_key_validates_and_transcribes_silence() -> None:
    key = live_key("YORDAMCHI_LIVE_OPENROUTER_API_KEY")
    transcriber = OpenRouterTranscriber(api_key=key, ffmpeg=None)
    try:
        check = await transcriber.validate_key(key, models=(MODEL,), timeout=10)
        assert check.ok
        transcript = await transcriber.transcribe(
            make_wav(1000), mime="audio/wav", languages=LANGUAGES, vocabulary=("BonviHR",), model=MODEL, timeout=20
        )
        assert isinstance(transcript.text, str)
    finally:
        await transcriber.aclose()
