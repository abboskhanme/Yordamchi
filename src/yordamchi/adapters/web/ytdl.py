"""`YtDlpDownloader`: the first video at a link, via yt-dlp 2026.10.21 + yt-dlp-ejs 0.8.0 (UC-32; STACK §2).

- **YouTube's JS challenges** need yt-dlp-ejs plus a JS runtime. Only deno is enabled by default, so the existing
  Node (`~/.local/bin/node`, ≥ 22) is enabled through `js_runtimes` (STACK §2; yt-dlp wiki "EJS"). No remote
  component is fetched: the scripts come from the pinned yt-dlp-ejs wheel.
- **ffmpeg** (merging video and audio, cutting a clip) is the static build in `DATA/bin` (`ffmpeg_location`).
- **No login, ever**: no cookie file, no browser cookies, no `.netrc`. Login walls and paywalls are final errors.
- **One extraction**: metadata first (`download=False`), then the checks the caller asked for (live, too long),
  then `process_ie_result(download=True)` on the same info, so a video that cannot fit costs no download.
- **Bounded**: yt-dlp is synchronous, so it runs in a worker thread (CONVENTIONS §6). The caller's `timeout` bounds
  the await; a progress hook stops the worker at its next chunk once the call is over, and stops any download that
  grows past `max_bytes` (yt-dlp's own `max_filesize` only sees a declared size).

yt-dlp's output goes to an in-memory sink, never to the log: it quotes URLs and titles. Errors: `translate.py`.
"""

import asyncio
import math
import mimetypes
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import yt_dlp  # type: ignore[import-untyped]
from yt_dlp.utils import DownloadCancelled, YoutubeDLError, download_range_func  # type: ignore[import-untyped]

from yordamchi.adapters.web import translate
from yordamchi.core.errors import (
    CallTimedOut,
    ExternalError,
    VideoIsLive,
    VideoNotFound,
    VideoTooLong,
    WebContentTooLarge,
)
from yordamchi.core.retry import retrying
from yordamchi.ports.web import Clip, DownloadedVideo

MIB: Final = 1024 * 1024
ATTEMPTS: Final = 2
SOCKET_TIMEOUT_MAX_S: Final = 30.0
OUTPUT_STEM: Final = "video"
# Highest resolution the selector allows first, then Telegram-friendly codecs (H.264/AAC in MP4 play inline).
FORMAT_SORT: Final = ("res", "vcodec:h264", "acodec:aac", "ext:mp4:m4a")
LIVE_STATUSES: Final = frozenset({"is_live", "is_upcoming", "post_live"})
SINK_MAX_LINES: Final = 200


class YoutubeDLLike(Protocol):
    """The part of `yt_dlp.YoutubeDL` used here (the seam the offline tests fake)."""

    def extract_info(self, url: str, download: bool = ...) -> Any: ...

    def process_ie_result(self, ie_result: Any, download: bool = ...) -> Any: ...


type YdlFactory = Callable[[dict[str, Any]], AbstractContextManager[YoutubeDLLike]]


def real_ydl(params: dict[str, Any]) -> AbstractContextManager[YoutubeDLLike]:
    ydl: AbstractContextManager[YoutubeDLLike] = yt_dlp.YoutubeDL(params)
    return ydl


@dataclass(slots=True)
class _Sink:
    """yt-dlp's `logger`: keeps the last lines in memory for classification; never writes them anywhere."""

    lines: list[str] = field(default_factory=list)

    def _keep(self, message: str) -> None:
        self.lines.append(message)
        del self.lines[:-SINK_MAX_LINES]

    def debug(self, message: str) -> None:
        self._keep(message)

    def info(self, message: str) -> None:
        self._keep(message)

    def warning(self, message: str) -> None:
        self._keep(message)

    def error(self, message: str) -> None:
        self._keep(message)

    def said_too_large(self) -> bool:
        return any("larger than max-filesize" in line for line in self.lines)


class _Stopped(DownloadCancelled):  # type: ignore[misc]  # yt-dlp re-raises DownloadCancelled from its handlers
    """Raised from the progress hook to stop the worker."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _first_video(info: Any) -> Mapping[str, Any] | None:
    """The video itself, or a playlist's first entry (`noplaylist` + `playlist_items=1` leave at most one)."""
    if not isinstance(info, Mapping):
        return None
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = info.get("entries") or []
        first = next(iter(entries), None)
        return _first_video(first)
    return info


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def _output_path(result: Any, dest_dir: Path) -> Path | None:
    """The finished file: yt-dlp's `requested_downloads[].filepath`, else the one `video.*` file in `dest_dir`."""
    if isinstance(result, Mapping):
        for download in result.get("requested_downloads") or []:
            filepath = download.get("filepath") if isinstance(download, Mapping) else None
            if isinstance(filepath, str) and Path(filepath).is_file():
                return Path(filepath)
    candidates = [
        path
        for path in dest_dir.glob(f"{OUTPUT_STEM}.*")
        if path.is_file() and path.suffix not in {".part", ".ytdl"} and ".f" not in path.stem
    ]
    return candidates[0] if len(candidates) == 1 else None


def _video_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed if guessed and guessed.startswith("video/") else "video/mp4"


@dataclass(frozen=True, slots=True)
class _Job:
    url: str
    site: str
    dest_dir: Path
    max_bytes: int
    format_selector: str
    max_duration_s: float | None
    clip: Clip | None
    socket_timeout: float


class YtDlpDownloader:
    """`ffmpeg_dir` holds ffmpeg and ffprobe; `node` is the JS runtime for YouTube (None → yt-dlp looks on PATH);
    `cache_dir` keeps yt-dlp's player cache between calls (None → no cache)."""

    def __init__(
        self, *, ffmpeg_dir: Path, node: Path | None, cache_dir: Path | None = None, factory: YdlFactory = real_ydl
    ) -> None:
        self._ffmpeg_dir = ffmpeg_dir
        self._node = node
        self._cache_dir = cache_dir
        self._factory = factory

    def params(self, job: _Job, sink: _Sink, hook: Callable[[Mapping[str, Any]], None]) -> dict[str, Any]:
        node: dict[str, str] = {"path": str(self._node)} if self._node is not None and self._node.exists() else {}
        params: dict[str, Any] = {
            "format": job.format_selector,
            "format_sort": list(FORMAT_SORT),
            "merge_output_format": "mp4",
            "paths": {"home": str(job.dest_dir), "temp": str(job.dest_dir)},
            "outtmpl": {"default": f"{OUTPUT_STEM}.%(ext)s"},
            "restrictfilenames": True,
            "overwrites": True,
            "noplaylist": True,
            "playlist_items": "1",
            "max_filesize": job.max_bytes,
            "socket_timeout": job.socket_timeout,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "color": "no_color",
            "consoletitle": False,
            "logger": sink,
            "progress_hooks": [hook],
            "ffmpeg_location": str(self._ffmpeg_dir),
            "js_runtimes": {"node": node},
            "remote_components": [],
            "cookiefile": None,
            "usenetrc": False,
            "cachedir": str(self._cache_dir) if self._cache_dir is not None else False,
            "live_from_start": False,
            "warn_when_outdated": False,
        }
        if job.clip is not None:
            params["download_ranges"] = download_range_func(None, [(job.clip.start_s, job.clip.end_s)])
            params["force_keyframes_at_cuts"] = True
        return params

    async def download(
        self,
        url: str,
        *,
        site: str,
        dest_dir: Path,
        max_bytes: int,
        format_selector: str,
        max_duration_s: float | None,
        clip: Clip | None,
        timeout: float,
    ) -> DownloadedVideo:
        job = _Job(
            url=url,
            site=site,
            dest_dir=dest_dir,
            max_bytes=max_bytes,
            format_selector=format_selector,
            max_duration_s=max_duration_s,
            clip=clip,
            socket_timeout=max(1.0, min(SOCKET_TIMEOUT_MAX_S, timeout / 4)),
        )
        abort = threading.Event()

        async def attempt() -> DownloadedVideo:
            return await asyncio.to_thread(self._run, job, abort)

        try:
            return await retrying(attempt, timeout=timeout, operation="video_download", attempts=ATTEMPTS)
        finally:
            abort.set()

    def _run(self, job: _Job, abort: threading.Event) -> DownloadedVideo:
        sink = _Sink()

        def hook(progress: Mapping[str, Any]) -> None:
            if abort.is_set():
                raise _Stopped("aborted")
            done = progress.get("downloaded_bytes")
            if isinstance(done, int) and done > job.max_bytes:
                raise _Stopped("too_large")

        job.dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self._factory(self.params(job, sink, hook)) as ydl:
                info = _first_video(ydl.extract_info(job.url, download=False))
                if info is None:
                    raise VideoNotFound(site=job.site)
                self._check(info, job)
                result = ydl.process_ie_result(dict(info), download=True)
        except _Stopped as stopped:
            if stopped.reason == "too_large":
                raise WebContentTooLarge(limit_mb=job.max_bytes // MIB) from None
            raise CallTimedOut(operation="video_download") from None
        except ExternalError:
            raise
        except YoutubeDLError as error:
            raise translate.from_ytdlp(error, site=job.site) from None
        path = _output_path(result, job.dest_dir)
        if path is None:
            if sink.said_too_large():
                raise WebContentTooLarge(limit_mb=job.max_bytes // MIB)
            raise VideoNotFound(site=job.site)
        path.chmod(0o600)
        merged: Mapping[str, Any] = result if isinstance(result, Mapping) else info
        height = merged.get("height")
        webpage_url = info.get("webpage_url")
        title = info.get("title")
        return DownloadedVideo(
            path=path,
            mime=_video_mime(path),
            size_bytes=path.stat().st_size,
            duration_s=job.clip.length_s if job.clip is not None else _number(info.get("duration")),
            title=title.strip()[:200] if isinstance(title, str) else "",
            webpage_url=webpage_url if isinstance(webpage_url, str) else None,
            height=int(height) if isinstance(height, int) else None,
        )

    @staticmethod
    def _check(info: Mapping[str, Any], job: _Job) -> None:
        """The caller's refusals, on metadata alone: a live stream, a video longer than `max_duration_s`."""
        if info.get("is_live") is True or info.get("live_status") in LIVE_STATUSES:
            raise VideoIsLive(site=job.site)
        duration = _number(info.get("duration"))
        if (
            job.clip is None
            and job.max_duration_s is not None
            and duration is not None
            and duration > job.max_duration_s
        ):
            link = info.get("webpage_url")
            raise VideoTooLong(minutes=math.ceil(duration / 60), link=link if isinstance(link, str) else None)
