"""WebTools: screenshots, file fetches and video downloads for the personal bot (SPEC §3.2.14; P3)."""

from dataclasses import dataclass
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


class WebTools(Protocol):
    async def screenshot(self, url: str, *, dest_dir: Path, width: int, height: int, timeout: float) -> Screenshot:
        """A new browser context per call (clean profile)."""
        ...

    async def fetch_file(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> FetchedFile: ...

    async def download_video(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> DownloadedVideo: ...
