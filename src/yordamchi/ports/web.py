"""WebTools: screenshots, file fetches, video downloads and page text for the personal bot (SPEC §3.2.14; P3).

Every call works in a clean, logged-out context: no owner cookies, no stored login (ASSUMPTIONS 2026-10-23). Files
are written into `dest_dir` (one fresh folder per request, chosen by the caller) with mode 0600.

The size-fit policy for videos is the caller's (`personal/artefacts/domain.py`): it picks the yt-dlp format selector,
the longest duration worth downloading and, after the download, whether and how to re-encode. The adapter only
carries those decisions out.

`compress_audio` (UC-37) turns an audio file the owner sent into the one format the speech readers share.

`read_page` (UC-31, T73) returns a page's readable text for `personal/media`, which hands it to the assistant as
data. Everything in a `PageText` is web content: untrusted, never an instruction.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Screenshot:
    viewport_png: Path
    full_page_png: Path
    title: str


@dataclass(frozen=True, slots=True)
class FetchedFile:
    path: Path
    mime: str
    size_bytes: int
    source_url: str


@dataclass(frozen=True, slots=True)
class DownloadedVideo:
    path: Path
    mime: str
    size_bytes: int
    duration_s: float | None
    title: str
    # The video's own page when the link was a page that embeds it (yt-dlp `webpage_url`); None when unknown.
    webpage_url: str | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class Clip:
    """A section of a video, in seconds from its start (UC-32 F: "a shorter clip")."""

    start_s: float
    end_s: float

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True, slots=True)
class CompressedAudio:
    """An audio file re-encoded for a speech reader (UC-37): mono 16 kHz MP3."""

    path: Path
    mime: str  # "audio/mpeg"
    size_bytes: int
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class PageText:
    """The readable text of one web page (title, description, then the main text in reading order)."""

    url: str  # after redirects
    title: str = field(repr=False)
    text: str = field(repr=False)
    truncated: bool  # the text was cut at the caller's `max_chars`
    size_bytes: int  # the HTML that was read


class WebTools(Protocol):
    async def screenshot(self, url: str, *, dest_dir: Path, width: int, height: int, timeout: float) -> Screenshot:
        """A new browser context per call (clean profile): a viewport PNG and a full-page PNG."""
        ...

    async def fetch_file(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> FetchedFile:
        """A direct file, or the main image (`og:image`, else the largest `<img>`) when `url` is a page."""
        ...

    async def download_video(
        self,
        url: str,
        *,
        dest_dir: Path,
        max_bytes: int,
        format_selector: str,
        max_duration_s: float | None,
        clip: Clip | None,
        timeout: float,
    ) -> DownloadedVideo:
        """yt-dlp download of the first video at `url`. `max_bytes` caps the download itself (the send cap is the
        caller's business); a video longer than `max_duration_s` is refused before any byte is fetched."""
        ...

    async def reencode_video(
        self, source: Path, *, dest_dir: Path, video_kbps: int, audio_kbps: int, max_height: int, timeout: float
    ) -> DownloadedVideo:
        """H.264/AAC MP4 at the given bitrates, scaled down to `max_height` (never up)."""
        ...

    async def compress_audio(self, source: Path, *, dest_dir: Path, kbps: int, timeout: float) -> CompressedAudio:
        """Any audio (or a video's sound) as mono 16 kHz MP3 at `kbps`: one format every speech reader takes, and a
        smaller request (UC-37). A file ffmpeg cannot decode raises `AudioEncodeFailed`."""
        ...

    async def read_page(self, url: str, *, max_chars: int, max_bytes: int, timeout: float) -> PageText:
        """The page's readable text, at most `max_chars`, from at most `max_bytes` of HTML (plain text is taken as it
        is). A login wall or paywall raises `WebLoginRequired` / `WebPaywalled`; a link to a file (PDF, image, video)
        raises `PageNotReadable`, and a page without readable text (a JavaScript app) raises `PageNoText`."""
        ...
