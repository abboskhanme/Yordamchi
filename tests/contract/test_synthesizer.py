from pathlib import Path

import pytest

from tests.fakes.speech import FakeSynthesizer
from tests.integration.tts_harness import FakeEdge, fake_ffmpeg
from yordamchi.adapters.edge_tts.tts import EdgeSynthesizer
from yordamchi.adapters.gemini.tts import GeminiSynthesizer
from yordamchi.adapters.openrouter.tts import OpenRouterSynthesizer
from yordamchi.core.errors import RateLimited
from yordamchi.ports.speech import SpeechAudio, Synthesizer


async def _speak(synthesizer: Synthesizer) -> SpeechAudio:
    return await synthesizer.synthesize(
        "Salom",
        voice="Kore",
        style="iliq",
        language_code="uz-UZ",
        model="tts-1",
        segment_words=0,
        timeout=15.0,
    )


class SynthesizerContract:
    @pytest.fixture
    def synthesizer(self) -> Synthesizer:
        raise NotImplementedError

    async def test_synthesize_returns_audio(self, synthesizer: Synthesizer) -> None:
        audio = await _speak(synthesizer)
        assert audio.data
        assert audio.mime.startswith("audio/")

    async def test_stream_yields_non_empty_chunks(self, synthesizer: Synthesizer) -> None:
        stream = synthesizer.synthesize_stream(
            "Salom",
            voice="Kore",
            style="iliq",
            language_code="uz-UZ",
            model="tts-1",
            segment_words=0,
            idle_timeout=15.0,
        )
        chunks = [chunk async for chunk in stream]
        assert chunks
        assert all(chunks)


class TestFakeSynthesizer(SynthesizerContract):
    @pytest.fixture
    def synthesizer(self) -> Synthesizer:
        return FakeSynthesizer()

    async def test_scripted_rate_limit(self) -> None:
        synthesizer = FakeSynthesizer()
        synthesizer.script(RateLimited(provider="google", retry_after=3.0))
        with pytest.raises(RateLimited) as caught:
            await _speak(synthesizer)
        assert caught.value.retry_after == 3.0


class TestEdgeSynthesizer(SynthesizerContract):
    """The real adapter offline: a scripted edge-tts stream and a fake ffmpeg (the live run is in
    tests/integration/test_edge_tts.py)."""

    @pytest.fixture
    def synthesizer(self, tmp_path: Path) -> Synthesizer:
        return EdgeSynthesizer(ffmpeg=fake_ffmpeg(tmp_path / "bin"), communicate=FakeEdge())

    async def test_returns_an_ogg_voice_note(self, synthesizer: Synthesizer) -> None:
        audio = await _speak(synthesizer)
        assert (audio.mime, audio.provider) == ("audio/ogg", "edge")
        assert audio.data.startswith(b"OggS")


def test_every_tts_adapter_is_a_synthesizer() -> None:
    """mypy checks the signatures; constructing them touches no network."""
    adapters: list[Synthesizer] = [
        OpenRouterSynthesizer(api_key="", ffmpeg=None),
        GeminiSynthesizer(api_key="", ffmpeg=None),
        EdgeSynthesizer(ffmpeg=None),
    ]
    assert len(adapters) == 3
