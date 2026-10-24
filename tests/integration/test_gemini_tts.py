"""`GeminiSynthesizer` against synthetic Interactions SSE streams (shaped after ai.google.dev speech-generation:
`step.delta` events with base64 `audio/l16` at 24 kHz) through an httpx transport, and a fake ffmpeg.

Covers the request shape (voice, style annotation, audio response, store off, streaming), the encode as deltas
arrive, a WAV delta, and the SPEC §3.2.11 step 6 error mapping, reusing the synthetic Google error fixtures.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.integration.stt_harness import GOOGLE_KEY, live_key, load, make_wav
from tests.integration.tts_harness import (
    COMPLETED,
    CREATED,
    INTERACTIONS,
    GoogleTtsServer,
    SseReply,
    audio_event,
    fake_ffmpeg,
    ffmpeg_argv,
    ffmpeg_stdin,
    google_tts_server,
    pcm_silence,
    real_ffmpeg,
)
from yordamchi.adapters.gemini.tts import GeminiSynthesizer, build_request
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
from yordamchi.ports.speech import SpeechAudio

MODEL = "gemini-3.8-flash-tts"
STYLE = "tabiiy o'zbek talaffuzi, iliq, aniq"
TEXT = "Salom! Bugun havo yaxshi."


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def google() -> AsyncIterator[GoogleTtsServer]:
    async with google_tts_server() as server:
        yield server


@pytest.fixture
def ffmpeg(tmp_path: Path) -> Path:
    return fake_ffmpeg(tmp_path / "bin")


def adapter(google: GoogleTtsServer, ffmpeg: Path | None, key: str = GOOGLE_KEY) -> GeminiSynthesizer:
    return GeminiSynthesizer(api_key=key, ffmpeg=ffmpeg, client_factory=google.client_factory)


async def speak(
    synthesizer: GeminiSynthesizer, *, text: str = TEXT, segment_words: int = 0, timeout: float = 15.0
) -> SpeechAudio:
    return await synthesizer.synthesize(
        text,
        voice="Kore",
        style=STYLE,
        language_code="uz-UZ",
        model=f"models/{MODEL}",
        segment_words=segment_words,
        timeout=timeout,
    )


def audio_stream(*parts: bytes) -> SseReply:
    return SseReply([CREATED, *(audio_event(part) for part in parts), COMPLETED])


# --- request shape and encode --------------------------------------------------------------------------------------
async def test_request_streams_audio_with_voice_and_style_annotation(google: GoogleTtsServer, ffmpeg: Path) -> None:
    pcm = pcm_silence(400)
    google.script(audio_stream(pcm[:3000], pcm[3000:]))
    audio = await speak(adapter(google, ffmpeg))

    [request] = google.requests
    assert request.url.path == INTERACTIONS
    assert request.headers["x-goog-api-key"] == GOOGLE_KEY
    [body] = google.bodies()
    assert body["model"] == MODEL
    # The SDK sends the text part inside one `user_input` step, the shape the speech-generation docs show.
    assert body["input"] == [
        {
            "type": "user_input",
            "content": [
                {
                    "type": "text",
                    "text": TEXT,
                    "annotations": [{"type": "speech_metadata", "style": f"{STYLE}. Language: uz-UZ"}],
                }
            ],
        }
    ]
    assert body["response_format"] == {"type": "audio"}
    assert body["generation_config"] == {"speech_config": [{"voice": "Kore"}]}
    assert (body["store"], body["stream"]) == (False, True)
    assert (audio.provider, audio.model, audio.mime, audio.duration_ms) == (
        "google",
        f"models/{MODEL}",
        "audio/ogg",
        1500,
    )
    assert audio.units == {"characters": len(TEXT), "requests": 1}
    assert ffmpeg_stdin(ffmpeg) == pcm
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "s16le", "-ar", "24000", "-ac", "1"]


def test_an_empty_style_and_language_send_no_annotation() -> None:
    request = build_request("Salom", voice="Puck", style="", language_code="", model=MODEL)
    assert request["input"] == [{"type": "text", "text": "Salom"}]


async def test_a_wav_delta_has_its_header_stripped_and_its_rate_kept(google: GoogleTtsServer, ffmpeg: Path) -> None:
    wav = make_wav(300, rate=16_000)
    google.script(SseReply([audio_event(wav, mime="audio/wav", sample_rate=None), COMPLETED]))
    await speak(adapter(google, ffmpeg))
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "s16le", "-ar", "16000", "-ac", "1"]
    assert ffmpeg_stdin(ffmpeg) == wav[44:]


async def test_segments_are_separate_requests_joined_in_order(google: GoogleTtsServer, ffmpeg: Path) -> None:
    first, second = b"\x01\x00" * 600, b"\x02\x00" * 600

    async def by_text(request: httpx.Request) -> httpx.Response:
        text = request.content.decode()
        part = first if "Birinchi" in text else second
        if part is first:
            await asyncio.sleep(0.2)
        return await google.handle_reply(request, audio_stream(part))

    google.script(by_text)
    await speak(adapter(google, ffmpeg), text="Birinchi gap shu. Ikkinchi gap esa bu.", segment_words=3)
    assert len(google.requests) == 2
    assert ffmpeg_stdin(ffmpeg) == first + second


# --- failures --------------------------------------------------------------------------------------------------------
async def test_without_ffmpeg_nothing_is_sent(google: GoogleTtsServer) -> None:
    google.script(audio_stream(pcm_silence(100)))
    with pytest.raises(NotInstalled):
        await speak(adapter(google, None))
    assert google.requests == []


@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_400_api_key_invalid", AuthFailed, False, 1),
        ("error_403_permission_denied", AuthFailed, False, 1),
        ("error_429_quota_per_day", QuotaExhausted, False, 1),
        ("error_404_model_not_found", ProviderUnavailable, False, 1),
        ("error_500_internal", ProviderUnavailable, True, 3),
        ("error_503_unavailable", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    google: GoogleTtsServer,
    ffmpeg: Path,
    fixture: str,
    expected: type[ExternalError],
    retryable: bool,
    requests: int,
) -> None:
    google.script(load("gemini", fixture))
    with pytest.raises(expected) as caught:
        await speak(adapter(google, ffmpeg))
    assert caught.value.retryable is retryable
    assert len(google.requests) == requests


async def test_a_rate_window_longer_than_the_budget_surfaces(google: GoogleTtsServer, ffmpeg: Path) -> None:
    google.script(load("gemini", "error_429_rate_per_minute"), audio_stream(pcm_silence(100)))
    with pytest.raises(RateLimited):  # its RetryInfo (17 s) is longer than this call's budget
        await speak(adapter(google, ffmpeg), timeout=3)
    assert len(google.requests) == 1


async def test_an_error_event_before_audio_is_retried(google: GoogleTtsServer, ffmpeg: Path) -> None:
    failed = SseReply([CREATED, {"event_type": "error", "error": {"code": "internal", "message": "Internal"}}])
    google.script(failed, audio_stream(pcm_silence(100)))
    audio = await speak(adapter(google, ffmpeg))
    assert audio.duration_ms == 1500
    assert len(google.requests) == 2


async def test_a_stream_without_audio_is_provider_unavailable(google: GoogleTtsServer, ffmpeg: Path) -> None:
    google.script(SseReply([CREATED, COMPLETED]))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(adapter(google, ffmpeg))
    assert caught.value.retryable is True
    assert len(google.requests) == 3


async def test_an_error_event_after_audio_started_fails_the_note(google: GoogleTtsServer, ffmpeg: Path) -> None:
    broken = SseReply([audio_event(pcm_silence(100)), {"event_type": "error", "error": {"message": "boom"}}])
    google.script(broken)
    with pytest.raises(ProviderUnavailable):
        await speak(adapter(google, ffmpeg))
    assert len(google.requests) == 1  # audio had started: no retry


async def test_undecodable_audio_is_not_retried(google: GoogleTtsServer, ffmpeg: Path) -> None:
    bad = {"event_type": "step.delta", "index": 0, "delta": {"type": "audio", "data": "!!not base64!!"}}
    google.script(SseReply([bad]))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(adapter(google, ffmpeg))
    assert caught.value.retryable is False


async def test_a_network_timeout_is_call_timed_out(google: GoogleTtsServer, ffmpeg: Path) -> None:
    google.script(httpx.ReadTimeout("slow"))
    with pytest.raises(CallTimedOut):
        await speak(adapter(google, ffmpeg), timeout=2)


async def test_a_new_vault_key_gets_a_new_client(google: GoogleTtsServer, ffmpeg: Path) -> None:
    google.script(audio_stream(pcm_silence(100)))
    keys = iter([GOOGLE_KEY, GOOGLE_KEY, "AIza" + "SyntheticKeyForOfflineTests_6543210"])

    async def current_key() -> str:
        return next(keys)

    synthesizer = GeminiSynthesizer(api_key=current_key, ffmpeg=ffmpeg, client_factory=google.client_factory)
    for _ in range(3):
        await speak(synthesizer)
    assert len(google.clients) == 2
    await synthesizer.aclose()


async def test_logs_carry_outcome_but_no_key_or_text(
    google: GoogleTtsServer, ffmpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    google.script(load("gemini", "error_503_unavailable"), audio_stream(pcm_silence(100)))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.gemini.tts"):
        await speak(adapter(google, ffmpeg))
    assert [record.getMessage() for record in caplog.records] == [
        "tts_attempt_failed",
        "tts_audio_started",
        "tts_call_finished",
    ]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert all(GOOGLE_KEY not in line and "havo" not in line for line in fields)
    encoded = base64.b64encode(pcm_silence(100)).decode()
    assert all(encoded[:40] not in line for line in fields)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_GOOGLE_API_KEY, ffmpeg in DATA/bin) ------------------
@pytest.mark.live
async def test_live_gemini_speaks_uzbek_into_an_ogg_opus_note() -> None:
    key = live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY")
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")
    synthesizer = GeminiSynthesizer(api_key=key, ffmpeg=ffmpeg)
    try:
        audio = await speak(synthesizer, timeout=30)
    finally:
        await synthesizer.aclose()
    assert audio.data.startswith(b"OggS")
    assert audio.duration_ms is not None
    assert audio.duration_ms > 1000
