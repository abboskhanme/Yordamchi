"""`FakeMediaAnalyzer`: scripted analyses and provider errors. Each call is recorded with its instruction, prompt,
model and timeout, so tests can check what reached the reader."""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from yordamchi.ports.media import MediaAnalysis, MediaAnalyzer, VideoSource

DEFAULT_ANSWER = '{"summary": "A short video.", "key_points": ["One point."], "opinion_hook": ""}'


@dataclass(frozen=True)
class VideoCall:
    source: VideoSource
    instruction: str
    prompt: str
    model: str
    timeout: float


@dataclass(frozen=True)
class AudioCall:
    path: Path
    mime: str
    instruction: str
    prompt: str
    model: str
    timeout: float


class FakeMediaAnalyzer:
    def __init__(self, *, provider: str = "google") -> None:
        self.provider = provider
        self.steps: deque[MediaAnalysis | Exception] = deque()
        self.calls: list[VideoCall] = []
        self.audio_calls: list[AudioCall] = []
        self.transcript = "[00:00] Speaker 1: Hello."

    def script(self, *results: MediaAnalysis | Exception) -> None:
        self.steps.extend(results)

    def answer(self, text: str, *, cost_usd_micros: int = 0) -> MediaAnalysis:
        """A scripted answer from this provider (not queued; pass it to `script`)."""
        return MediaAnalysis(
            text=text, provider=self.provider, model="fake-video", cost_usd_micros=cost_usd_micros, latency_ms=900
        )

    async def analyze_video(
        self, source: VideoSource, *, instruction: str, prompt: str, model: str, timeout: float
    ) -> MediaAnalysis:
        self.calls.append(VideoCall(source, instruction, prompt, model, timeout))
        step = self.steps.popleft() if self.steps else None
        if isinstance(step, Exception):
            raise step
        if step is None:
            return MediaAnalysis(text=DEFAULT_ANSWER, provider=self.provider, model=model)
        return step

    async def analyze_audio(
        self, path: Path, *, mime: str, instruction: str, prompt: str, model: str, timeout: float
    ) -> MediaAnalysis:
        self.audio_calls.append(AudioCall(path, mime, instruction, prompt, model, timeout))
        step = self.steps.popleft() if self.steps else None
        if isinstance(step, Exception):
            raise step
        if step is None:
            return MediaAnalysis(text=self.transcript, provider=self.provider, model=model)
        return step


_: MediaAnalyzer = FakeMediaAnalyzer()
