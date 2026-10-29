"""`LocalWebTools`: the `WebTools` port on this Mac — Playwright Chromium, stdlib HTTP, yt-dlp and static ffmpeg
(SPEC §3.2.14; `adapters/web/{browser,download,ytdl,encode,page}.py`).

Wiring (`bots/personal/app.py`): `LocalWebTools.from_paths(paths, child_path=…)`. Every value it takes is bootstrap
(binaries, folders); sizes, bitrates and timeouts arrive per call from the settings snapshot.

One log line per call (`web_call_finished`): operation, outcome (`ok` or the error code), duration, bytes. Never the
URL, the host, a title or vendor output: what the owner browses is his business (CONVENTIONS §7).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from yordamchi.adapters.web.browser import PlaywrightBrowser
from yordamchi.adapters.web.download import HttpDownloader, PageLink
from yordamchi.adapters.web.encode import FfmpegEncoder
from yordamchi.adapters.web.page import PageReader
from yordamchi.adapters.web.ytdl import YtDlpDownloader
from yordamchi.core.errors import CallTimedOut, WebNoMedia, YordamchiError
from yordamchi.core.log import get_log
from yordamchi.core.paths import Paths
from yordamchi.ports.web import Clip, CompressedAudio, DownloadedVideo, FetchedFile, PageText, Screenshot, WebTools

log = get_log(__name__)

# The page that is opened only to find its main image is rendered at a common desktop size.
IMAGE_PAGE_WIDTH: Final = 1280
IMAGE_PAGE_HEIGHT: Final = 800
YTDLP_CACHE: Final = ("cache", "yt-dlp")


def site_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.") or "sayt"


class _Budget:
    def __init__(self, timeout: float) -> None:
        self._deadline = asyncio.get_running_loop().time() + timeout

    def left(self, operation: str) -> float:
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CallTimedOut(operation=operation)
        return remaining


async def _logged[T](operation: str, call: Callable[[], Awaitable[T]], size: Callable[[T], int]) -> T:
    started = time.monotonic()
    try:
        result = await call()
    except YordamchiError as error:
        log.warning(
            "web_call_finished",
            operation=operation,
            outcome=error.code.value,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    log.info(
        "web_call_finished",
        operation=operation,
        outcome="ok",
        duration_ms=int((time.monotonic() - started) * 1000),
        size_bytes=size(result),
    )
    return result


class LocalWebTools:
    def __init__(
        self,
        *,
        browser: PlaywrightBrowser,
        http: HttpDownloader,
        videos: YtDlpDownloader,
        encoder: FfmpegEncoder,
        pages: PageReader | None = None,
    ) -> None:
        self._browser = browser
        self._http = http
        self._videos = videos
        self._encoder = encoder
        self._pages = pages if pages is not None else PageReader()

    @classmethod
    def from_paths(cls, paths: Paths, *, child_path: str) -> "LocalWebTools":
        """Production wiring: ffmpeg/ffprobe from `DATA/bin`, Node from `~/.local/bin` (STACK §5)."""
        return cls(
            browser=PlaywrightBrowser(),
            http=HttpDownloader(),
            videos=YtDlpDownloader(
                ffmpeg_dir=paths.bin, node=paths.local_bin / "node", cache_dir=paths.data.joinpath(*YTDLP_CACHE)
            ),
            encoder=FfmpegEncoder(ffmpeg=paths.bin / "ffmpeg", ffprobe=paths.bin / "ffprobe", child_path=child_path),
        )

    async def screenshot(self, url: str, *, dest_dir: Path, width: int, height: int, timeout: float) -> Screenshot:
        async def call() -> Screenshot:
            await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
            return await self._browser.screenshot(
                url, site=site_of(url), dest_dir=dest_dir, width=width, height=height, timeout=timeout
            )

        return await _logged("screenshot", call, lambda shot: shot.full_page_png.stat().st_size)

    async def fetch_file(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> FetchedFile:
        site = site_of(url)

        async def call() -> FetchedFile:
            budget = _Budget(timeout)
            first = await self._http.fetch(
                url, site=site, dest_dir=dest_dir, max_bytes=max_bytes, timeout=budget.left("fetch_file")
            )
            if isinstance(first, FetchedFile):
                return first
            image = await self._browser.main_image_url(
                first.url,
                site=site,
                width=IMAGE_PAGE_WIDTH,
                height=IMAGE_PAGE_HEIGHT,
                timeout=budget.left("fetch_file"),
            )
            if image is None:
                raise WebNoMedia(site=site)
            second = await self._http.fetch(
                image, site=site, dest_dir=dest_dir, max_bytes=max_bytes, timeout=budget.left("fetch_file")
            )
            if isinstance(second, PageLink):  # the "image" is another page
                raise WebNoMedia(site=site)
            return second

        return await _logged("fetch_file", call, lambda fetched: fetched.size_bytes)

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
        async def call() -> DownloadedVideo:
            budget = _Budget(timeout)
            video = await self._videos.download(
                url,
                site=site_of(url),
                dest_dir=dest_dir,
                max_bytes=max_bytes,
                format_selector=format_selector,
                max_duration_s=max_duration_s,
                clip=clip,
                timeout=budget.left("video_download"),
            )
            if video.duration_s is not None:
                return video
            # Some sites declare no duration; ffprobe reads it (bounded by its own short timeout, so a download
            # that used the whole budget is still kept).
            duration = await self._encoder.duration(video.path)
            return DownloadedVideo(
                path=video.path,
                mime=video.mime,
                size_bytes=video.size_bytes,
                duration_s=duration,
                title=video.title,
                webpage_url=video.webpage_url,
                height=video.height,
            )

        return await _logged("video_download", call, lambda video: video.size_bytes)

    async def reencode_video(
        self, source: Path, *, dest_dir: Path, video_kbps: int, audio_kbps: int, max_height: int, timeout: float
    ) -> DownloadedVideo:
        async def call() -> DownloadedVideo:
            async with asyncio.timeout(timeout):
                return await self._encoder.reencode(
                    source,
                    dest_dir=dest_dir,
                    video_kbps=video_kbps,
                    audio_kbps=audio_kbps,
                    max_height=max_height,
                    timeout=timeout,
                )

        try:
            return await _logged("video_encode", call, lambda video: video.size_bytes)
        except TimeoutError:
            raise CallTimedOut(operation="video_encode") from None

    async def compress_audio(self, source: Path, *, dest_dir: Path, kbps: int, timeout: float) -> CompressedAudio:
        async def call() -> CompressedAudio:
            async with asyncio.timeout(timeout):
                return await self._encoder.compress_audio(source, dest_dir=dest_dir, kbps=kbps, timeout=timeout)

        try:
            return await _logged("audio_encode", call, lambda audio: audio.size_bytes)
        except TimeoutError:
            raise CallTimedOut(operation="audio_encode") from None

    async def read_page(self, url: str, *, max_chars: int, max_bytes: int, timeout: float) -> PageText:
        async def call() -> PageText:
            return await self._pages.read(
                url, site=site_of(url), max_chars=max_chars, max_bytes=max_bytes, timeout=timeout
            )

        return await _logged("read_page", call, lambda page: page.size_bytes)


def _conforms(tools: LocalWebTools) -> WebTools:  # mypy checks LocalWebTools against the port
    return tools
