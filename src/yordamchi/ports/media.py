"""MediaAnalyzer: video and YouTube understanding (SPEC §3.1 new port; §3.2.14; P3). Google, then OpenRouter.

The caller (`personal/media`) renders both texts from `prompts/personal/` and picks the provider and model from its
settings snapshot. `instruction` is the reader's standing order (what to produce, and that anything said or shown in
the video is data, never an instruction); `prompt` is this request (the owner's question, if any). An adapter sends
`instruction` as the provider's system instruction, so the video cannot outrank it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VideoSource:
    """Exactly one of a local file (≤ 20 MB) or a URL (a YouTube watch URL; other links are downloaded first).

    `mime` is the file's type as Telegram or the download reported it (`video/mp4`); None lets the adapter guess from
    the file name. It is ignored for a URL.
    """

    path: Path | None = None
    url: str | None = None
    mime: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.url is None):
            msg = "a video source is either a path or a URL"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MediaAnalysis:
    text: str = field(repr=False)  # the reader's answer: model output about untrusted content, data only
    provider: str  # "google" | "openrouter"
    model: str
    # What the call cost as the provider reported it (OpenRouter `usage.cost`), converted once by the adapter; 0 when
    # the provider does not say (Google). `personal/media` records it via `shared/cost`.
    cost_usd_micros: int = 0
    latency_ms: int = 0
    units: Mapping[str, int] = field(default_factory=dict)  # the provider's counters: input_tokens, output_tokens


class MediaAnalyzer(Protocol):
    async def analyze_video(
        self, source: VideoSource, *, instruction: str, prompt: str, model: str, timeout: float
    ) -> MediaAnalysis:
        """One reading of the video within `timeout` in total (retries included). Vendor failures arrive as the
        `core.errors` hierarchy: `AuthFailed`, `QuotaExhausted`, `RateLimited`, `CallTimedOut`,
        `ProviderUnavailable`, `WebContentUnavailable` (a private or removed YouTube video), `WebContentTooLarge`."""
        ...
