"""`GeminiTranscriber` on a Live model (`gemini-3.5-transcribe-live`, owner decision 2026-10-25), offline.

A fake `client.aio.live` replays what the Live API sent in a real session on 2026-10-25 (interim captions, one final
`input_transcription`, `generation_complete`) and the WebSocket closes it uses for failures (1008 with a reason, 1011).
Covers the request shape (PCM from ffmpeg, the server's VAD off, hints in `input_audio_transcription`), the end of a
call, and the SPEC §3.2.11 error mapping on this path.
"""

import asyncio
from pathlib import Path

import pytest

from tests.integration.stt_harness import (
    GOOGLE_KEY,
    LANGUAGES,
    LiveServer,
    fake_ffmpeg,
    ffmpeg_argv,
    live_closed,
    live_done,
    live_final,
    live_interim,
    live_key,
    make_ogg_opus,
)
from yordamchi.adapters.gemini import live_stt
from yordamchi.adapters.gemini.stt import GeminiTranscriber
from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    NotInstalled,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from yordamchi.ports.speech import Transcript

MODEL = "gemini-3.5-transcribe-live"
VOCABULARY = ("BonviHR", "HRMentor", "BonviHR", "  ", "Yordamchi")
SPOKEN = "Привет, это тест голосового сообщения. Please deploy the Yordamchi bot to the server tomorrow."
PCM = bytes(range(256)) * 250  # 64,000 bytes = 2 s of 16 kHz 16-bit mono


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
def ffmpeg(tmp_path: Path) -> Path:
    return fake_ffmpeg(tmp_path / "bin", output=PCM)


def adapter(server: LiveServer, ffmpeg: Path | None) -> GeminiTranscriber:
    return GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=server.client_factory, ffmpeg=ffmpeg)


async def transcribe(transcriber: GeminiTranscriber, *, timeout: float = 8.0, mode: str = "smart") -> Transcript:
    try:
        return await transcriber.transcribe(
            make_ogg_opus(2000),
            mime="audio/ogg",
            languages=LANGUAGES,
            vocabulary=VOCABULARY,
            model=MODEL,
            mode=mode,
            fallback_model="google/gemini-3.5-transcribe",
            timeout=timeout,
        )
    finally:
        await transcriber.aclose()


# --- which models go live --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model", "live"),
    [
        ("gemini-3.5-transcribe-live", True),
        ("models/gemini-3.8-live", True),
        ("gemini-3.1-flash-live-preview", True),
        ("gemini-3.5-transcribe", False),
        ("gemini-3.8-flash", False),
        ("gemini-olive-1", False),  # "live" inside a word is not a Live model
    ],
)
def test_a_model_with_a_live_part_goes_over_the_live_api(model: str, live: bool) -> None:
    assert live_stt.is_live_model(model) is live


# --- request shape ---------------------------------------------------------------------------------------------------
async def test_the_note_goes_as_pcm_in_one_activity_with_the_hints(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_interim("Привет."), live_interim("Привет, это"), live_final(SPOKEN), live_done()])
    transcript = await transcribe(adapter(server, ffmpeg))

    assert ffmpeg_argv(ffmpeg)[-7:] == ["-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    [session] = server.sessions
    assert session.model == MODEL
    assert session.config == {
        "response_modalities": ["TEXT"],
        "input_audio_transcription": {
            "language_codes": ["uz-UZ", "ru-RU", "en-US"],
            "custom_vocabulary": ["BonviHR", "HRMentor", "Yordamchi"],
            "mode": "SMART",
        },
        "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
    }
    assert session.sent[0] == {"activity_start": {}}
    assert session.sent[-1] == {"activity_end": {}}
    chunks = session.sent[1:-1]
    assert [chunk["audio"] for chunk in chunks] == [
        {"data": PCM[:32_000], "mime_type": "audio/pcm;rate=16000"},
        {"data": PCM[32_000:], "mime_type": "audio/pcm;rate=16000"},
    ]
    assert server.keys == [GOOGLE_KEY]

    assert transcript.text == SPOKEN  # interim captions are not the transcript
    assert (transcript.provider, transcript.provider_path, transcript.model) == ("google", "live", MODEL)
    assert transcript.audio_ms == 2000
    assert transcript.latency_ms >= 0


async def test_the_call_ends_on_generation_complete_not_on_the_idle_close(ffmpeg: Path) -> None:
    """The fake session never closes by itself (a real one closes ~60 s later); the call must not wait for it."""
    server = LiveServer()
    server.script([live_final("birinchi qism."), live_final("ikkinchi qism."), live_done()])
    async with asyncio.timeout(2):
        transcript = await transcribe(adapter(server, ffmpeg))
    assert transcript.text == "birinchi qism. ikkinchi qism."


async def test_no_speech_is_an_empty_transcript(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_done()])
    assert (await transcribe(adapter(server, ffmpeg))).text == ""


async def test_no_hints_and_no_mode_leave_the_transcription_config_empty(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_final("salom"), live_done()])
    transcriber = adapter(server, ffmpeg)
    await transcriber.transcribe(
        make_ogg_opus(500),
        mime="audio/ogg",
        languages=(" ",),
        vocabulary=(),
        model=MODEL,
        mode="",
        fallback_model="",
        timeout=8,
    )
    await transcriber.aclose()
    assert server.sessions[0].config["input_audio_transcription"] == {}


# --- failures --------------------------------------------------------------------------------------------------------
async def test_an_invalid_key_is_auth_failed_and_not_retried(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_closed(1008, "API key not valid. Please pass a valid API key.")])
    with pytest.raises(AuthFailed):
        await transcribe(adapter(server, ffmpeg))
    assert len(server.sessions) == 1


async def test_an_unknown_model_is_not_retried(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_closed(1008, "models/gemini-9-live is not found for API version v1beta")])
    with pytest.raises(ProviderUnavailable) as caught:
        await transcribe(adapter(server, ffmpeg))
    assert caught.value.retryable is False
    assert len(server.sessions) == 1


async def test_an_internal_error_is_retried_once_more(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_closed(1011, "Internal error encountered.")], [live_final(SPOKEN), live_done()])
    transcript = await transcribe(adapter(server, ffmpeg))
    assert transcript.text == SPOKEN
    assert len(server.sessions) == 2


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        ("You exceeded your current quota, please check your plan and billing details.", RateLimited),
        ("Quota exceeded for metric: generate_requests_per_model_per_day, limit: 0", QuotaExhausted),
    ],
)
async def test_quota_closes_are_rate_limit_or_quota(ffmpeg: Path, reason: str, error: type[Exception]) -> None:
    server = LiveServer()
    server.script([live_closed(1008, reason)])
    with pytest.raises(error):
        await transcribe(adapter(server, ffmpeg), timeout=0.5)


async def test_a_session_that_hangs_is_call_timed_out(ffmpeg: Path) -> None:
    server = LiveServer()
    server.script([live_interim("Привет")])  # no final, no generation_complete
    with pytest.raises(CallTimedOut):
        await transcribe(adapter(server, ffmpeg), timeout=0.3)


async def test_a_dropped_connection_is_retryable(ffmpeg: Path) -> None:
    server = LiveServer()
    server.connect_error = OSError("Network is unreachable")
    with pytest.raises(ProviderUnavailable) as caught:
        await transcribe(adapter(server, ffmpeg), timeout=0.5)
    assert caught.value.retryable is True


async def test_without_ffmpeg_a_live_model_is_not_installed(tmp_path: Path) -> None:
    server = LiveServer()
    server.script([live_done()])
    with pytest.raises(NotInstalled):
        await transcribe(adapter(server, None))
    with pytest.raises(NotInstalled):
        await transcribe(adapter(server, tmp_path / "missing" / "ffmpeg"))
    assert server.sessions == []


async def test_audio_ffmpeg_cannot_read_is_not_retried(tmp_path: Path) -> None:
    server = LiveServer()
    server.script([live_done()])
    with pytest.raises(ProviderUnavailable) as caught:
        await transcribe(adapter(server, fake_ffmpeg(tmp_path / "bin", fail=True)))
    assert caught.value.retryable is False
    assert server.sessions == []


async def test_logs_carry_no_key_and_no_transcript(ffmpeg: Path, caplog: pytest.LogCaptureFixture) -> None:
    server = LiveServer()
    server.script([live_final(SPOKEN), live_done()])
    with caplog.at_level("INFO", logger="yordamchi.adapters.gemini.live_stt"):
        await transcribe(adapter(server, ffmpeg))
    lines = [f"{record.getMessage()} {getattr(record, 'yd_fields', {})}" for record in caplog.records]
    assert any(line.startswith("stt_call_finished") and "'provider_path': 'live'" in line for line in lines)
    assert all(GOOGLE_KEY not in line and SPOKEN not in line for line in lines)


# --- live (opt-in: `pytest -m live`, key in YORDAMCHI_LIVE_GOOGLE_API_KEY, ffmpeg in YORDAMCHI_LIVE_FFMPEG) ---------
@pytest.mark.live
async def test_live_model_validates_and_transcribes_silence() -> None:
    key = live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY")
    ffmpeg = Path(live_key("YORDAMCHI_LIVE_FFMPEG"))
    transcriber = GeminiTranscriber(api_key=key, ffmpeg=ffmpeg)
    try:
        check = await transcriber.validate_key(key, models=(MODEL,), timeout=10)
        assert check.ok
        transcript = await transcriber.transcribe(
            make_ogg_opus(1000),
            mime="audio/ogg",
            languages=LANGUAGES,
            vocabulary=("BonviHR",),
            model=MODEL,
            mode="smart",
            fallback_model="",
            timeout=15,
        )
        assert isinstance(transcript.text, str)
        assert transcript.provider_path == "live"
    finally:
        await transcriber.aclose()
