import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.speech import FakeTranscriber
from yordamchi.core.errors import CallTimedOut, QuotaExhausted
from yordamchi.ports.speech import Transcriber

LANGUAGES = ("uz-UZ", "ru-RU", "en-US")


class TranscriberContract:
    @pytest.fixture
    def transcriber(self) -> Transcriber:
        raise NotImplementedError

    async def test_returns_text_with_its_provenance(self, transcriber: Transcriber) -> None:
        transcript = await transcriber.transcribe(
            b"OggS...", mime="audio/ogg", languages=LANGUAGES, vocabulary=("BonviHR",), model="m-1", timeout=8.0
        )
        assert isinstance(transcript.text, str)
        assert transcript.provider
        assert transcript.model == "m-1"


class TestFakeTranscriber(TranscriberContract):
    @pytest.fixture
    def transcriber(self) -> Transcriber:
        return FakeTranscriber()

    async def test_records_hints_and_vocabulary(self) -> None:
        transcriber = FakeTranscriber()
        await transcriber.transcribe(
            b"abc", mime="audio/ogg", languages=LANGUAGES, vocabulary=("HRMentor",), model="m", timeout=8.0
        )
        assert transcriber.calls[0].languages == LANGUAGES
        assert transcriber.calls[0].vocabulary == ("HRMentor",)

    async def test_scripted_errors_and_timeouts(self) -> None:
        transcriber = FakeTranscriber(clock=FakeClock())
        transcriber.script(QuotaExhausted(provider="google"))
        with pytest.raises(QuotaExhausted):
            await transcriber.transcribe(b"a", mime="audio/ogg", languages=(), vocabulary=(), model="m", timeout=8.0)
        transcriber.script("kech", latency_s=9.0)
        with pytest.raises(CallTimedOut):
            await transcriber.transcribe(b"a", mime="audio/ogg", languages=(), vocabulary=(), model="m", timeout=0.0)
