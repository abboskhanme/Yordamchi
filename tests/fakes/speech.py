"""`FakeTranscriber` and `FakeSynthesizer`: scripted results, latency and provider errors (429, 402, timeout).

Latency runs on the given clock (a `FakeClock` in unit tests). A latency longer than the call's `timeout` raises
`CallTimedOut`, as the real adapters do. `FakeTranscriber.validate_key` answers from its own script: by default every
model is available.
"""

from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from yordamchi.core.errors import CallTimedOut
from yordamchi.ports.clock import Clock
from yordamchi.ports.speech import SpeechAudio, SpeechKeyCheck, Synthesizer, Transcriber, Transcript


@dataclass(frozen=True, slots=True)
class Step[T]:
    result: T | Exception
    latency_s: float = 0.0


async def _wait(clock: Clock | None, latency_s: float, timeout: float, operation: str) -> None:
    if latency_s > timeout:
        if clock is not None:
            await clock.sleep(timeout)
        raise CallTimedOut(operation=operation)
    if clock is not None and latency_s > 0:
        await clock.sleep(latency_s)


@dataclass(frozen=True, slots=True)
class TranscribeCall:
    audio_bytes: int
    mime: str
    languages: tuple[str, ...]
    vocabulary: tuple[str, ...]
    model: str
    timeout: float
    mode: str
    fallback_model: str


@dataclass(frozen=True, slots=True)
class KeyCheckCall:
    key: str = field(repr=False)
    models: tuple[str, ...]
    timeout: float


class FakeTranscriber:
    def __init__(self, *, provider: str = "google", clock: Clock | None = None, default_text: str = "salom") -> None:
        self.provider = provider
        self.clock = clock
        self.default_text = default_text
        self.steps: deque[Step[Transcript]] = deque()
        self.calls: list[TranscribeCall] = []
        self.key_steps: deque[Step[SpeechKeyCheck]] = deque()
        self.key_calls: list[KeyCheckCall] = []

    def script(self, result: Transcript | str | Exception, *, latency_s: float = 0.0) -> None:
        value = self._transcript(result, model="") if isinstance(result, str) else result
        self.steps.append(Step(value, latency_s))

    def script_key_check(self, result: SpeechKeyCheck | Exception, *, latency_s: float = 0.0) -> None:
        self.key_steps.append(Step(result, latency_s))

    def _transcript(self, text: str, *, model: str) -> Transcript:
        return Transcript(
            text=text, provider=self.provider, provider_path="fake", model=model, audio_ms=1000, latency_ms=1
        )

    async def transcribe(
        self,
        audio: bytes,
        *,
        mime: str,
        languages: Sequence[str],
        vocabulary: Sequence[str],
        model: str,
        mode: str,
        fallback_model: str,
        timeout: float,
    ) -> Transcript:
        self.calls.append(
            TranscribeCall(len(audio), mime, tuple(languages), tuple(vocabulary), model, timeout, mode, fallback_model)
        )
        step = self.steps.popleft() if self.steps else Step(self._transcript(self.default_text, model=model))
        await _wait(self.clock, step.latency_s, timeout, "stt")
        if isinstance(step.result, Exception):
            raise step.result
        return step.result

    async def validate_key(self, key: str, *, models: Sequence[str], timeout: float) -> SpeechKeyCheck:
        self.key_calls.append(KeyCheckCall(key, tuple(models), timeout))
        everything = SpeechKeyCheck(provider=self.provider, available_models=tuple(models), missing_models=())
        step = self.key_steps.popleft() if self.key_steps else Step(everything)
        await _wait(self.clock, step.latency_s, timeout, "speech_key_check")
        if isinstance(step.result, Exception):
            raise step.result
        return step.result


class FakeSynthesizer:
    def __init__(self, *, provider: str = "google", clock: Clock | None = None) -> None:
        self.provider = provider
        self.clock = clock
        self.steps: deque[Step[SpeechAudio]] = deque()
        self.calls: list[tuple[str, str, str]] = []

    def script(self, result: SpeechAudio | Exception, *, latency_s: float = 0.0) -> None:
        self.steps.append(Step(result, latency_s))

    def _audio(self, model: str) -> SpeechAudio:
        return SpeechAudio(
            data=b"OggS fake opus", mime="audio/ogg", provider=self.provider, model=model, duration_ms=900
        )

    async def synthesize(
        self, text: str, *, voice: str, style: str, language_code: str, model: str, timeout: float
    ) -> SpeechAudio:
        self.calls.append((text, voice, model))
        step = self.steps.popleft() if self.steps else Step(self._audio(model))
        await _wait(self.clock, step.latency_s, timeout, "tts")
        if isinstance(step.result, Exception):
            raise step.result
        return step.result

    async def synthesize_stream(
        self, text: str, *, voice: str, style: str, language_code: str, model: str, idle_timeout: float
    ) -> AsyncIterator[bytes]:
        audio = await self.synthesize(
            text, voice=voice, style=style, language_code=language_code, model=model, timeout=idle_timeout
        )
        middle = len(audio.data) // 2
        yield audio.data[:middle]
        yield audio.data[middle:]


_transcriber: Transcriber = FakeTranscriber()
_synthesizer: Synthesizer = FakeSynthesizer()
