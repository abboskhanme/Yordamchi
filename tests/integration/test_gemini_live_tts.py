"""`GeminiSynthesizer` on a Live model (`gemini-3.8-live`, owner request 2026-11-01), offline.

A fake `client.aio.live` answers like the real session of the 2026-11-01 spike: `model_turn` parts with raw 24 kHz PCM
(`audio/pcm;rate=24000`), then `turn_complete`, and the WebSocket closes it uses for failures. The fallback model
(`tts.google_fallback_model`, the Interactions API) runs on the synthetic SSE transport of `test_gemini_tts.py`, so
one client carries both. Covers the request shape, the duration guard, the fallback, segmenting and the error mapping.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from google import genai

from tests.integration.stt_harness import (
    GOOGLE_KEY,
    LiveEvent,
    LiveServer,
    LiveSessionLog,
    live_audio,
    live_closed,
    live_key,
    live_turn_complete,
)
from tests.integration.tts_harness import (
    COMPLETED,
    CREATED,
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
from yordamchi import prompts
from yordamchi.adapters.gemini import live_tts
from yordamchi.adapters.gemini.stt import ClientFactory
from yordamchi.adapters.gemini.tts import GeminiSynthesizer, falls_back
from yordamchi.core import retry
from yordamchi.core.errors import AuthFailed, NotInstalled, ProviderUnavailable, QuotaExhausted
from yordamchi.ports.speech import SpeechAudio

MODEL = "gemini-3.8-live"
FALLBACK = "gemini-3.8-flash-tts"
STYLE = "tabiiy o'zbek talaffuzi, iliq, aniq"
TEXT = "Salom! Bugun havo yaxshi."  # 4 words: expected 2 s, accepted 0.6-8 s
PROMPT = "You are a text-to-speech engine. Read the user's text aloud exactly as written."


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


def both(live: LiveServer, google: GoogleTtsServer) -> ClientFactory:
    """A client whose `aio.live` is the fake Live server and whose `aio.interactions` is the SSE transport's."""

    def factory(key: str) -> genai.Client:
        live.keys.append(key)
        real = google.client_factory(key)
        aio = SimpleNamespace(live=live, interactions=real.aio.interactions, aclose=real.aio.aclose)
        return cast(genai.Client, SimpleNamespace(aio=aio, real=real))  # `real` stays referenced while in use

    return factory


def adapter(live: LiveServer, google: GoogleTtsServer, ffmpeg: Path | None) -> GeminiSynthesizer:
    return GeminiSynthesizer(
        api_key=GOOGLE_KEY, ffmpeg=ffmpeg, client_factory=both(live, google), live_instruction=PROMPT
    )


async def speak(
    synthesizer: GeminiSynthesizer,
    *,
    text: str = TEXT,
    segment_words: int = 0,
    fallback: str = FALLBACK,
    timeout: float = 8.0,
) -> SpeechAudio:
    try:
        return await synthesizer.synthesize(
            text,
            voice="Kore",
            style=STYLE,
            language_code="uz-UZ",
            model=MODEL,
            segment_words=segment_words,
            timeout=timeout,
            fallback_model=fallback,
        )
    finally:
        await synthesizer.aclose()


def spoken(pcm: bytes) -> list[LiveEvent]:
    """A session that reads: the PCM in two chunks, then the end of the turn."""
    half = len(pcm) // 2
    return [live_audio(pcm[:half]), live_audio(pcm[half:]), live_turn_complete()]


def sse_audio(pcm: bytes) -> SseReply:
    return SseReply([CREATED, audio_event(pcm), COMPLETED])


def sent_text(session: LiveSessionLog) -> str:
    [turn] = session.sent
    content = cast(dict[str, list[dict[str, str]]], turn["turns"])
    return content["parts"][0]["text"]


# --- request shape ---------------------------------------------------------------------------------------------------
async def test_the_text_is_read_in_one_live_turn_with_the_voice_and_instruction(
    google: GoogleTtsServer, ffmpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pcm = pcm_silence(1500)
    live = LiveServer()
    live.script(spoken(pcm))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.gemini"):
        audio = await speak(adapter(live, google, ffmpeg))

    [session] = live.sessions
    assert session.model == MODEL
    assert session.config == {
        "response_modalities": ["AUDIO"],
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Kore"}}},
        "system_instruction": f"{PROMPT}\n\nHow to speak (never words to say): {STYLE}. Language: uz-UZ.",
    }
    assert session.sent == [{"turns": {"role": "user", "parts": [{"text": TEXT}]}, "turn_complete": True}]
    assert live.keys == [GOOGLE_KEY]
    assert google.requests == []  # the fallback model was never needed

    assert ffmpeg_stdin(ffmpeg) == pcm
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    assert (audio.provider, audio.model, audio.mime, audio.duration_ms) == ("google", MODEL, "audio/ogg", 1500)
    assert audio.units == {"characters": len(TEXT), "requests": 1}

    lines = [f"{record.getMessage()} {getattr(record, 'yd_fields', {})}" for record in caplog.records]
    assert any(line.startswith("tts_audio_started") and "'provider_path': 'live'" in line for line in lines)
    assert any(line.startswith("tts_call_finished") and "'provider_path': 'live'" in line for line in lines)
    assert all(GOOGLE_KEY not in line and "havo" not in line for line in lines)


async def test_the_call_ends_on_turn_complete_not_on_the_idle_close(google: GoogleTtsServer, ffmpeg: Path) -> None:
    """The fake session never closes by itself (a real one stays open); the note must not wait for it."""
    live = LiveServer()
    live.script(spoken(pcm_silence(1000)))
    async with asyncio.timeout(3):
        audio = await speak(adapter(live, google, ffmpeg))
    assert audio.model == MODEL


async def test_segments_are_separate_sessions_joined_in_order(google: GoogleTtsServer, ffmpeg: Path) -> None:
    first, second = b"\x01\x00" * 24_000, b"\x02\x00" * 24_000  # 1 s each

    async def by_text(session: LiveSessionLog) -> list[LiveEvent]:
        if "Birinchi" in sent_text(session):
            await asyncio.sleep(0.2)  # the first segment answers last
            return spoken(first)
        return spoken(second)

    live = LiveServer(responder=by_text)
    await speak(adapter(live, google, ffmpeg), text="Birinchi gap shu. Ikkinchi gap esa bu.", segment_words=3)
    assert sorted(sent_text(session) for session in live.sessions) == ["Birinchi gap shu.", "Ikkinchi gap esa bu."]
    assert ffmpeg_stdin(ffmpeg) == first + second


# --- the fallback model -----------------------------------------------------------------------------------------------
async def test_a_live_close_falls_back_to_the_interactions_model(google: GoogleTtsServer, ffmpeg: Path) -> None:
    live = LiveServer()
    live.script([live_closed(1008, "Request contains an invalid argument.")])
    pcm = pcm_silence(800)
    google.script(sse_audio(pcm))
    audio = await speak(adapter(live, google, ffmpeg))

    assert len(live.sessions) == 1  # a non-retryable close is not retried on the same model
    [body] = google.bodies()
    assert body["model"] == FALLBACK
    assert body["generation_config"] == {"speech_config": [{"voice": "Kore"}]}
    assert audio.model == FALLBACK
    assert ffmpeg_stdin(ffmpeg) == pcm


@pytest.mark.parametrize(
    ("audio_ms", "why"),
    [(200, "too short: words were skipped"), (9_000, "too long: it answered instead of reading")],
)
async def test_audio_of_the_wrong_length_falls_back(
    google: GoogleTtsServer, ffmpeg: Path, audio_ms: int, why: str
) -> None:
    live = LiveServer()
    live.script(spoken(pcm_silence(audio_ms)))
    pcm = pcm_silence(700)
    google.script(sse_audio(pcm))
    audio = await speak(adapter(live, google, ffmpeg))
    assert audio.model == FALLBACK, why
    assert len(live.sessions) == 1
    assert len(google.requests) == 1
    assert ffmpeg_stdin(ffmpeg) == pcm


async def test_both_models_failing_surfaces_the_fallback_error(google: GoogleTtsServer, ffmpeg: Path) -> None:
    live = LiveServer()
    live.script([live_closed(1008, "Request contains an invalid argument.")])
    google.script(SseReply([CREATED, COMPLETED]))  # no audio: retryable, three tries
    with pytest.raises(ProviderUnavailable):
        await speak(adapter(live, google, ffmpeg))
    assert len(google.requests) == 3


async def test_no_fallback_model_surfaces_the_live_error(google: GoogleTtsServer, ffmpeg: Path) -> None:
    live = LiveServer()
    live.script([live_closed(1008, "Quota exceeded for metric: generate_requests_per_model_per_day, limit: 0")])
    with pytest.raises(QuotaExhausted):
        await speak(adapter(live, google, ffmpeg), fallback="")
    assert google.requests == []


async def test_an_invalid_key_is_auth_failed_without_a_fallback(google: GoogleTtsServer, ffmpeg: Path) -> None:
    live = LiveServer()
    live.script([live_closed(1008, "API key not valid. Please pass a valid API key.")])
    google.script(sse_audio(pcm_silence(500)))
    with pytest.raises(AuthFailed):
        await speak(adapter(live, google, ffmpeg))
    assert len(live.sessions) == 1
    assert google.requests == []


async def test_without_ffmpeg_no_session_opens(google: GoogleTtsServer) -> None:
    live = LiveServer()
    live.script(spoken(pcm_silence(1000)))
    with pytest.raises(NotInstalled):
        await speak(adapter(live, google, None))
    assert live.sessions == []
    assert google.requests == []


# --- the rules -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("error", "fallback", "expected"),
    [
        (ProviderUnavailable(provider="google", retryable=False), FALLBACK, True),
        (QuotaExhausted(provider="google"), FALLBACK, True),
        (ProviderUnavailable(provider="google"), "", False),
        (ProviderUnavailable(provider="google"), "  ", False),
        (ProviderUnavailable(provider="google"), f"models/{MODEL}", False),  # the same model again
        (AuthFailed(provider="google"), FALLBACK, False),
        (NotInstalled(binary="ffmpeg"), FALLBACK, False),
        (ProviderUnavailable(provider="ffmpeg", retryable=False), FALLBACK, False),  # every model's audio fails there
    ],
)
def test_which_failures_fall_back(error: ProviderUnavailable, fallback: str, expected: bool) -> None:
    assert falls_back(error, model=MODEL, fallback_model=fallback) is expected


def test_duration_bounds_grow_with_the_words() -> None:
    assert live_tts.duration_bounds_ms(TEXT) == (2_000, 600, 8_000)
    expected, shortest, longest = live_tts.duration_bounds_ms(" ".join(["so'z"] * 60))
    assert (expected, shortest, longest) == (30_000, 9_000, 92_000)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_GOOGLE_API_KEY, ffmpeg in DATA/bin) ------------------
@pytest.mark.live
async def test_live_model_reads_uzbek_into_an_ogg_opus_note() -> None:
    key = live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY")
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")
    synthesizer = GeminiSynthesizer(
        api_key=key, ffmpeg=ffmpeg, live_instruction=prompts.render("personal/voice_live_system")
    )
    audio = await speak(synthesizer, text="Salom! Bugun havo juda yaxshi, sayrga chiqamizmi?", fallback="", timeout=30)
    assert audio.data.startswith(b"OggS")
    assert audio.model == MODEL
    assert audio.duration_ms is not None
    assert audio.duration_ms > 1000
