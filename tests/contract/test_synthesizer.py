import pytest

from tests.fakes.speech import FakeSynthesizer
from yordamchi.core.errors import RateLimited
from yordamchi.ports.speech import SpeechAudio, Synthesizer


async def _speak(synthesizer: Synthesizer) -> SpeechAudio:
    return await synthesizer.synthesize(
        "Salom", voice="Kore", style="iliq", language_code="uz-UZ", model="tts-1", timeout=15.0
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
            "Salom", voice="Kore", style="iliq", language_code="uz-UZ", model="tts-1", idle_timeout=15.0
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
