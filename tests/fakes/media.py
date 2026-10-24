"""`FakeMediaAnalyzer`: scripted analyses and provider errors."""

from collections import deque

from yordamchi.ports.media import MediaAnalysis, MediaAnalyzer, VideoSource


class FakeMediaAnalyzer:
    def __init__(self, *, provider: str = "google") -> None:
        self.provider = provider
        self.steps: deque[MediaAnalysis | Exception] = deque()
        self.calls: list[tuple[VideoSource, str, str]] = []

    def script(self, result: MediaAnalysis | Exception) -> None:
        self.steps.append(result)

    async def analyze_video(self, source: VideoSource, *, prompt: str, model: str, timeout: float) -> MediaAnalysis:
        self.calls.append((source, prompt, model))
        step = self.steps.popleft() if self.steps else MediaAnalysis(text="video", provider=self.provider, model=model)
        if isinstance(step, Exception):
            raise step
        return step


_: MediaAnalyzer = FakeMediaAnalyzer()
