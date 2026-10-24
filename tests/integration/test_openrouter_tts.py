"""`OpenRouterSynthesizer` against a local aiohttp server answering with synthetic audio, and a fake ffmpeg.

Covers SPEC §3.2.11 step 5 (`/audio/speech`, Gemini TTS answers `pcm` only), the encode to OGG/Opus as the body
arrives, the S8 sentence segments, and step 6's error mapping (401/402/429/5xx, a 200 carrying an error).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.stt_harness import GOOGLE_KEY, OPENROUTER_KEY, Recording, live_key, load, make_ogg_opus
from tests.integration.tts_harness import (
    OPENROUTER_TTS_BASE,
    AudioReply,
    OpenRouterSpeechServer,
    fake_ffmpeg,
    ffmpeg_argv,
    ffmpeg_stdin,
    openrouter_speech_server,
    pcm_silence,
    real_ffmpeg,
)
from yordamchi.adapters.openrouter.tts import OpenRouterSynthesizer, audio_input_of, speech_payload
from yordamchi.adapters.tts_common import MP3_INPUT, pcm_input
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

MODEL = "google/gemini-3.8-flash-tts"
STYLE = "tabiiy o'zbek talaffuzi, iliq, aniq"
TEXT = "Salom! Bugun havo yaxshi. Ertaga yomg'ir yog'adi."


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def server() -> AsyncIterator[OpenRouterSpeechServer]:
    async with openrouter_speech_server() as running:
        yield running


@pytest.fixture
def ffmpeg(tmp_path: Path) -> Path:
    return fake_ffmpeg(tmp_path / "bin")


def adapter(server: OpenRouterSpeechServer, ffmpeg: Path | None, key: str = OPENROUTER_KEY) -> OpenRouterSynthesizer:
    return OpenRouterSynthesizer(api_key=key, ffmpeg=ffmpeg, base_url=OPENROUTER_TTS_BASE, session=server.session)


async def speak(
    synthesizer: OpenRouterSynthesizer, *, text: str = TEXT, segment_words: int = 0, timeout: float = 15.0
) -> SpeechAudio:
    return await synthesizer.synthesize(
        text,
        voice="Kore",
        style=STYLE,
        language_code="uz-UZ",
        model=MODEL,
        segment_words=segment_words,
        timeout=timeout,
    )


# --- request shape and encode --------------------------------------------------------------------------------------
async def test_speech_request_asks_for_pcm_with_style_and_language_as_instructions(
    server: OpenRouterSpeechServer, ffmpeg: Path
) -> None:
    pcm = pcm_silence(500)
    server.script(AudioReply(chunks=[pcm[:4000], pcm[4000:]], gap_s=0.01))
    audio = await speak(adapter(server, ffmpeg))

    [seen] = server.seen
    assert seen.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert seen.json == {
        "model": MODEL,
        "input": TEXT,
        "voice": "Kore",
        "response_format": "pcm",
        "instructions": f"{STYLE}. Language: uz-UZ",
    }
    assert (audio.mime, audio.provider, audio.model, audio.duration_ms) == ("audio/ogg", "openrouter", MODEL, 1500)
    assert audio.data == make_ogg_opus(1500)
    assert audio.units == {"characters": len(TEXT), "requests": 1}
    assert audio.cost_usd_micros is None  # the body is audio: no usage.cost on this path
    assert ffmpeg_stdin(ffmpeg) == pcm  # every byte reached the encoder, in order


async def test_pcm_is_encoded_as_24_khz_mono_into_ogg_opus_voip(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(200)]))
    await speak(adapter(server, ffmpeg))
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    assert argv[argv.index("-c:a") :] == [
        "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", "-application", "voip", "-f", "ogg", "pipe:1",
    ]  # fmt: skip


async def test_an_mp3_answer_is_decoded_as_mp3(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[b"\xff\xfb\x90\x64" + bytes(400)], content_type="audio/mpeg"))
    await speak(adapter(server, ffmpeg))
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "mp3"]


def test_content_types_map_to_encoder_inputs() -> None:
    assert audio_input_of("audio/pcm") == pcm_input()
    assert audio_input_of("audio/mpeg; charset=binary") == MP3_INPUT
    assert audio_input_of("audio/wav").container == "wav"
    with pytest.raises(ProviderUnavailable) as caught:
        audio_input_of("text/html")
    assert caught.value.retryable is False


def test_an_empty_style_sends_no_instructions() -> None:
    payload = speech_payload("Salom", voice="Puck", style="", language_code="", model=MODEL)
    assert "instructions" not in payload


async def test_segments_are_requested_concurrently_and_encoded_in_order(
    server: OpenRouterSpeechServer, ffmpeg: Path
) -> None:
    first, second = b"\x01\x00" * 800, b"\x02\x00" * 800
    server.script(AudioReply(chunks=[first], delay_s=0.3), AudioReply(chunks=[second], delay_s=0.1))
    audio = await speak(adapter(server, ffmpeg), text="Bir ikki uch. To'rt besh olti.", segment_words=3)

    assert [seen.json["input"] for seen in server.seen] == ["Bir ikki uch.", "To'rt besh olti."]
    assert ffmpeg_stdin(ffmpeg) == first + second  # the slow first piece still comes first
    assert audio.units["requests"] == 2
    assert server.max_in_flight == 2  # in parallel, not one after the other


# --- failures --------------------------------------------------------------------------------------------------------
async def test_without_ffmpeg_nothing_is_sent(server: OpenRouterSpeechServer, tmp_path: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)]))
    for missing in (None, tmp_path / "nope" / "ffmpeg"):
        with pytest.raises(NotInstalled):
            await speak(adapter(server, missing))
    assert server.seen == []


async def test_an_encoder_failure_is_not_retried_and_names_ffmpeg(
    server: OpenRouterSpeechServer, tmp_path: Path
) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)]))
    broken = fake_ffmpeg(tmp_path / "bin", fail=True)
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(adapter(server, broken))
    assert (caught.value.retryable, caught.value.params["provider"]) == (False, "ffmpeg")
    assert len(server.seen) == 1


@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_401", AuthFailed, False, 1),
        ("error_402_credits", QuotaExhausted, False, 1),
        ("error_429_free_per_day", QuotaExhausted, False, 1),
        ("error_403_moderation", ProviderUnavailable, False, 1),
        ("error_502", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    server: OpenRouterSpeechServer,
    ffmpeg: Path,
    fixture: str,
    expected: type[ExternalError],
    retryable: bool,
    requests: int,
) -> None:
    server.script(load("openrouter", fixture))
    with pytest.raises(expected) as caught:
        await speak(adapter(server, ffmpeg))
    assert caught.value.retryable is retryable
    assert len(server.seen) == requests


async def test_a_bad_request_is_not_retried(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    unauthorized = load("openrouter", "error_401")
    server.script(Recording(400, unauthorized.headers, {"error": {"code": 400, "message": "Unsupported format: mp3"}}))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(adapter(server, ffmpeg))
    assert caught.value.retryable is False
    assert len(server.seen) == 1


async def test_a_5xx_then_audio_succeeds_on_retry(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(load("openrouter", "error_502"), AudioReply(chunks=[pcm_silence(100)]))
    audio = await speak(adapter(server, ffmpeg))
    assert audio.duration_ms == 1500
    assert len(server.seen) == 2


async def test_rate_limit_longer_than_the_budget_surfaces_with_retry_after(
    server: OpenRouterSpeechServer, ffmpeg: Path
) -> None:
    server.script(load("openrouter", "error_429_rate"))
    with pytest.raises(RateLimited) as caught:
        await speak(adapter(server, ffmpeg), timeout=5)
    assert caught.value.retry_after == 30.0
    assert len(server.seen) == 1


async def test_a_200_carrying_a_json_error_is_mapped_by_its_code(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    no_credits = load("openrouter", "error_402_credits")
    server.script(Recording(200, {"content-type": "application/json"}, no_credits.body))
    with pytest.raises(QuotaExhausted):
        await speak(adapter(server, ffmpeg))


async def test_a_200_without_audio_is_retried_then_unavailable(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[]))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(adapter(server, ffmpeg))
    assert caught.value.retryable is True
    assert len(server.seen) == 3


async def test_a_slow_first_byte_times_out_within_the_call_timeout(
    server: OpenRouterSpeechServer, ffmpeg: Path
) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)], delay_s=30))
    started = time.monotonic()
    with pytest.raises(CallTimedOut):
        await speak(adapter(server, ffmpeg), timeout=0.5)
    assert time.monotonic() - started < 2.0


async def test_a_body_that_stalls_after_audio_started_times_out(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100), pcm_silence(100)], gap_s=5))
    started = time.monotonic()
    with pytest.raises(CallTimedOut):
        await speak(adapter(server, ffmpeg), timeout=1.0)
    assert time.monotonic() - started < 3.0


async def test_an_unreachable_server_is_retryable_provider_unavailable(ffmpeg: Path, tmp_path: Path) -> None:
    import aiohttp

    session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(tmp_path / "nobody.sock")))
    try:
        synthesizer = OpenRouterSynthesizer(
            api_key=OPENROUTER_KEY, ffmpeg=ffmpeg, base_url=OPENROUTER_TTS_BASE, session=session
        )
        with pytest.raises(ProviderUnavailable) as caught:
            await speak(synthesizer)
        assert caught.value.retryable is True
    finally:
        await session.close()


async def test_an_empty_key_fails_auth_without_a_request(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)]))
    with pytest.raises(AuthFailed):
        await speak(adapter(server, ffmpeg, key=""))
    assert server.seen == []


async def test_the_key_is_read_per_call_from_the_vault_callable(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)]))
    keys = iter([OPENROUTER_KEY, "sk-or-v1-" + "f" * 64])

    async def current_key() -> str:
        return next(keys)

    synthesizer = OpenRouterSynthesizer(
        api_key=current_key, ffmpeg=ffmpeg, base_url=OPENROUTER_TTS_BASE, session=server.session
    )
    await speak(synthesizer)
    await speak(synthesizer)
    assert [seen.headers["Authorization"][-4:] for seen in server.seen] == [OPENROUTER_KEY[-4:], "ffff"]


async def test_the_stream_yields_the_finished_note(server: OpenRouterSpeechServer, ffmpeg: Path) -> None:
    server.script(AudioReply(chunks=[pcm_silence(100)]))
    stream = adapter(server, ffmpeg).synthesize_stream(
        TEXT, voice="Kore", style=STYLE, language_code="uz-UZ", model=MODEL, segment_words=0, idle_timeout=5
    )
    assert b"".join([chunk async for chunk in stream]) == make_ogg_opus(1500)


async def test_the_adapter_owns_and_closes_its_session_when_none_is_given(ffmpeg: Path) -> None:
    synthesizer = OpenRouterSynthesizer(api_key=OPENROUTER_KEY, ffmpeg=ffmpeg)
    session = await synthesizer._http()
    await synthesizer.aclose()
    assert session.closed


async def test_logs_carry_outcome_and_duration_but_no_key_or_text(
    server: OpenRouterSpeechServer, ffmpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server.script(load("openrouter", "error_502"), AudioReply(chunks=[pcm_silence(100)]))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.openrouter.tts"):
        await speak(adapter(server, ffmpeg))
    assert [record.getMessage() for record in caplog.records] == [
        "tts_attempt_failed",
        "tts_audio_started",
        "tts_call_finished",
    ]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert "gen-synthetic-tts-0001" in fields[1]
    assert "'duration_ms'" in fields[2]
    assert all(OPENROUTER_KEY not in line and "havo" not in line and GOOGLE_KEY not in line for line in fields)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_OPENROUTER_API_KEY, ffmpeg in DATA/bin) ---------------
@pytest.mark.live
async def test_live_openrouter_speaks_uzbek_into_an_ogg_opus_note() -> None:
    key = live_key("YORDAMCHI_LIVE_OPENROUTER_API_KEY")
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")
    synthesizer = OpenRouterSynthesizer(api_key=key, ffmpeg=ffmpeg)
    try:
        audio = await asyncio.wait_for(speak(synthesizer, timeout=30), timeout=35)
    finally:
        await synthesizer.aclose()
    assert audio.data.startswith(b"OggS")
    assert audio.duration_ms is not None
    assert audio.duration_ms > 1000
