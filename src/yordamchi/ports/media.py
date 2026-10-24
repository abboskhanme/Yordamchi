"""MediaAnalyzer: video and YouTube understanding (SPEC §3.1 new port; P3). Google, then OpenRouter."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VideoSource:
    """Exactly one of a local file (≤ 20 MB) or a URL (YouTube or a direct link)."""

    path: Path | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.url is None):
            msg = "a video source is either a path or a URL"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MediaAnalysis:
    text: str
    provider: str
    model: str
    cost_usd_micros: int = 0


class MediaAnalyzer(Protocol):
    async def analyze_video(self, source: VideoSource, *, prompt: str, model: str, timeout: float) -> MediaAnalysis: ...
