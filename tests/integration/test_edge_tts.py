"""`EdgeSynthesizer` with a scripted `edge_tts.Communicate` (no websocket) and a fake ffmpeg; plus the `live` check,
which needs no key (Edge is free) but does need the network and a real ffmpeg.

Covers the voice per call, MP3 into ffmpeg as it arrives, the S8 segments, and the error mapping of edge-tts and
its aiohttp transport (recognised without importing aiohttp in the adapter, C5).
"""

from pathlib import Path

import aiohttp
import pytest
from edge_tts import exceptions as edge_errors
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tests.integration.stt_harness import make_ogg_opus
from tests.integration.tts_harness import EdgeScript, FakeEdge, fake_ffmpeg, ffmpeg_argv, ffmpeg_stdin, real_ffmpeg
from yordamchi.adapters.edge_tts.tts import EdgeSynthesizer, translate_error
from yordamchi.core import retry
from yordamchi.core.errors import CallTimedOut, ExternalError, NotInstalled, ProviderUnavailable, RateLimited
from yordamchi.ports.speech import SpeechAudio

VOICE = "uz-UZ-MadinaNeural"
TEXT = "Salom! Bugun havo yaxshi."
MP3_FRAME = b"\xff\xfb\x90\x64" + bytes(412)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
def ffmpeg(tmp_path: Path) -> Path:
    return fake_ffmpeg(tmp_path / "bin")


@pytest.fixture
def edge() -> FakeEdge:
    return FakeEdge()


async def speak(
    synthesizer: EdgeSynthesizer, *, text: str = TEXT, segment_words: int = 0, timeout: float = 15.0
) -> SpeechAudio:
    return await synthesizer.synthesize(
        text,
        voice=VOICE,
        style="iliq",
        language_code="uz-UZ",
        model="edge-tts",
        segment_words=segment_words,
        timeout=timeout,
    )


def handshake_error(status: int) -> aiohttp.WSServerHandshakeError:
    url = URL("wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1")
    info = aiohttp.RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
    return aiohttp.WSServerHandshakeError(request_info=info, history=(), status=status, message="handshake")


# --- the happy path ------------------------------------------------------------------------------------------------
async def test_voice_and_timeouts_come_from_the_call_and_mp3_reaches_ffmpeg(edge: FakeEdge, ffmpeg: Path) -> None:
    boundary = {"type": "SentenceBoundary", "offset": 0, "duration": 1, "text": "Salom!"}
    edge.script(EdgeScript(chunks=[MP3_FRAME, boundary, MP3_FRAME]))
    audio = await EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge).synthesize(
        TEXT,
        voice="uz-UZ-SardorNeural",
        style="ignored",
        language_code="ignored",
        model="edge-tts",
        segment_words=0,
        timeout=7.5,
    )
    [call] = edge.calls
    assert (call.text, call.voice, call.connect_timeout_s, call.receive_timeout_s) == (
        TEXT,
        "uz-UZ-SardorNeural",
        8,
        8,
    )
    assert ffmpeg_stdin(ffmpeg) == MP3_FRAME * 2  # boundary metadata is not audio
    argv = ffmpeg_argv(ffmpeg)
    assert argv[argv.index("-f") : argv.index("-i")] == ["-f", "mp3"]
    assert (audio.provider, audio.model, audio.mime, audio.duration_ms) == ("edge", "edge-tts", "audio/ogg", 1500)
    assert audio.data == make_ogg_opus(1500)
    assert audio.units == {"characters": len(TEXT), "requests": 1}


async def test_segments_go_as_separate_streams_joined_in_order(edge: FakeEdge, ffmpeg: Path) -> None:
    first, second = b"\xff\xfb\x01" + bytes(100), b"\xff\xfb\x02" + bytes(100)
    edge.script(EdgeScript(chunks=[first], gap_s=0.2), EdgeScript(chunks=[second]))
    audio = await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge), text="Bir ikki. Uch to'rt.", segment_words=2)
    assert [call.text for call in edge.calls] == ["Bir ikki.", "Uch to'rt."]
    assert ffmpeg_stdin(ffmpeg) == first + second
    assert audio.units["requests"] == 2


async def test_the_stream_yields_the_finished_note(edge: FakeEdge, ffmpeg: Path) -> None:
    stream = EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge).synthesize_stream(
        TEXT, voice=VOICE, style="", language_code="", model="edge-tts", segment_words=0, idle_timeout=5
    )
    assert b"".join([chunk async for chunk in stream]) == make_ogg_opus(1500)


# --- failures --------------------------------------------------------------------------------------------------------
async def test_without_ffmpeg_edge_is_not_called(edge: FakeEdge) -> None:
    with pytest.raises(NotInstalled):
        await speak(EdgeSynthesizer(ffmpeg=None, communicate=edge))
    assert edge.calls == []


@pytest.mark.parametrize(
    ("error", "expected", "retryable", "calls"),
    [
        (edge_errors.NoAudioReceived("no audio"), ProviderUnavailable, False, 1),
        (ValueError("Invalid voice 'x'"), ProviderUnavailable, False, 1),
        (edge_errors.WebSocketError("closed"), ProviderUnavailable, True, 3),
        (edge_errors.UnexpectedResponse("odd"), ProviderUnavailable, True, 3),
        (ConnectionResetError("reset"), ProviderUnavailable, True, 3),
        (handshake_error(429), RateLimited, True, 3),
        (handshake_error(403), ProviderUnavailable, True, 3),
        (handshake_error(400), ProviderUnavailable, False, 1),
        (handshake_error(503), ProviderUnavailable, True, 3),
    ],
)
async def test_failures_before_audio_map_to_core_errors_and_only_retryable_ones_retry(
    edge: FakeEdge,
    ffmpeg: Path,
    error: Exception,
    expected: type[ExternalError],
    retryable: bool,
    calls: int,
) -> None:
    edge.script(EdgeScript(chunks=[], error=error))
    with pytest.raises(expected) as caught:
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge))
    assert caught.value.retryable is retryable
    assert len(edge.calls) == calls


async def test_a_bad_voice_name_at_construction_is_not_retried(edge: FakeEdge, ffmpeg: Path) -> None:
    edge.script(ValueError("Invalid voice 'nope'"))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge))
    assert caught.value.retryable is False
    assert len(edge.calls) == 1


async def test_a_stream_without_audio_is_not_retried(edge: FakeEdge, ffmpeg: Path) -> None:
    edge.script(EdgeScript(chunks=[{"type": "SentenceBoundary", "offset": 0, "duration": 1, "text": "x"}]))
    with pytest.raises(ProviderUnavailable) as caught:
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge))
    assert caught.value.retryable is False


async def test_a_failure_after_audio_started_fails_the_note_without_retry(edge: FakeEdge, ffmpeg: Path) -> None:
    edge.script(EdgeScript(chunks=[MP3_FRAME], error=edge_errors.WebSocketError("dropped")))
    with pytest.raises(ProviderUnavailable):
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge))
    assert len(edge.calls) == 1


async def test_a_stalled_stream_times_out_within_the_call_timeout(edge: FakeEdge, ffmpeg: Path) -> None:
    edge.script(EdgeScript(chunks=[MP3_FRAME, MP3_FRAME], gap_s=5))
    with pytest.raises(CallTimedOut):
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge), timeout=0.5)


def test_an_error_that_is_not_edges_is_not_translated() -> None:
    assert translate_error(KeyError("bug")) is None
    assert isinstance(translate_error(TimeoutError()), CallTimedOut)


async def test_an_unknown_error_propagates_unchanged(edge: FakeEdge, ffmpeg: Path) -> None:
    edge.script(EdgeScript(chunks=[], error=KeyError("a bug, not a provider failure")))
    with pytest.raises(KeyError):
        await speak(EdgeSynthesizer(ffmpeg=ffmpeg, communicate=edge))


# --- live (opt-in: `pytest -m live`; no key needed, but the network and DATA/bin/ffmpeg are) -----------------------
@pytest.mark.live
async def test_live_edge_speaks_uzbek_into_an_ogg_opus_note() -> None:
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")
    audio = await speak(EdgeSynthesizer(ffmpeg=ffmpeg), timeout=30)
    assert audio.data.startswith(b"OggS")
    assert audio.duration_ms is not None
    assert audio.duration_ms > 1000
