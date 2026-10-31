"""`FakeWebTools`: writes placeholder files (0600) into `dest_dir` and records each request.

Scriptable for the artefacts tests: the size, duration and mime of a "downloaded" video (written as a sparse file, so
a 300 MB video costs nothing), the successive sizes re-encodes produce, and a failure per URL or per method.
"""

from collections.abc import Mapping
from pathlib import Path

from yordamchi.ports.web import (
    ApiAnswer,
    Clip,
    CompressedAudio,
    DownloadedVideo,
    FetchedFile,
    PageText,
    Screenshot,
    WebTools,
)


def _write(path: Path, data: bytes = b"", *, size: int | None = None) -> Path:
    path.write_bytes(data)
    if size is not None:
        with path.open("r+b") as handle:
            handle.truncate(size)
    path.chmod(0o600)
    return path


class FakeWebTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.failures: dict[str, Exception] = {}  # by URL (any method) or by method name
        self.title = "Fake page"
        self.file_mime = "application/octet-stream"
        self.file_name = "file.bin"
        self.file_bytes = b"fake file"
        self.video_size = 8
        self.video_duration_s: float | None = 1.0
        self.video_mime = "video/mp4"
        self.video_webpage_url: str | None = None
        self.encode_sizes: list[int] = []  # one per re-encode; empty → half the source
        self.page_title = "Fake page"
        self.page_text = "A fake page about one thing. " * 10
        self.audio_duration_s: float | None = 60.0
        self.api_status = 200
        self.api_mime = "application/json"
        self.api_text = '{"ok": true}'
        self.api_headers: list[dict[str, str]] = []  # the headers of each `api_get`, in order

    def _call(self, method: str, url: str, **kwargs: object) -> None:
        self.calls.append((method, url))
        self.kwargs.append(kwargs)
        failure = self.failures.get(url) or self.failures.get(method)
        if failure is not None:
            raise failure

    async def screenshot(self, url: str, *, dest_dir: Path, width: int, height: int, timeout: float) -> Screenshot:
        self._call("screenshot", url, width=width, height=height, timeout=timeout)
        viewport = _write(dest_dir / "viewport.png", b"\x89PNG viewport")
        full = _write(dest_dir / "full.png", b"\x89PNG full page")
        return Screenshot(viewport_png=viewport, full_page_png=full, title=self.title)

    async def fetch_file(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> FetchedFile:
        self._call("fetch_file", url, max_bytes=max_bytes, timeout=timeout)
        path = _write(dest_dir / self.file_name, self.file_bytes)
        return FetchedFile(path=path, mime=self.file_mime, size_bytes=len(self.file_bytes), source_url=url)

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
        self._call(
            "download_video",
            url,
            max_bytes=max_bytes,
            format_selector=format_selector,
            max_duration_s=max_duration_s,
            clip=clip,
            timeout=timeout,
        )
        path = _write(dest_dir / "video.mp4", b"fake mp4", size=self.video_size)
        return DownloadedVideo(
            path=path,
            mime=self.video_mime,
            size_bytes=self.video_size,
            duration_s=clip.length_s if clip is not None else self.video_duration_s,
            title="Fake video",
            webpage_url=self.video_webpage_url,
            height=720,
        )

    async def reencode_video(
        self, source: Path, *, dest_dir: Path, video_kbps: int, audio_kbps: int, max_height: int, timeout: float
    ) -> DownloadedVideo:
        self._call(
            "reencode_video",
            str(source),
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            max_height=max_height,
            timeout=timeout,
        )
        size = self.encode_sizes.pop(0) if self.encode_sizes else self.video_size // 2
        path = _write(dest_dir / f"video-{video_kbps}k.mp4", b"fake mp4", size=size)
        return DownloadedVideo(
            path=path,
            mime="video/mp4",
            size_bytes=size,
            duration_s=self.video_duration_s,
            title="Fake video",
            height=max_height,
        )

    async def compress_audio(self, source: Path, *, dest_dir: Path, kbps: int, timeout: float) -> CompressedAudio:
        self._call("compress_audio", str(source), kbps=kbps, timeout=timeout)
        data = b"fake mp3"
        path = _write(dest_dir / f"audio-{kbps}k.mp3", data)
        return CompressedAudio(path=path, mime="audio/mpeg", size_bytes=len(data), duration_s=self.audio_duration_s)

    async def read_page(self, url: str, *, max_chars: int, max_bytes: int, timeout: float) -> PageText:
        self._call("read_page", url, max_chars=max_chars, max_bytes=max_bytes, timeout=timeout)
        text = self.page_text[:max_chars]
        return PageText(
            url=url,
            title=self.page_title,
            text=text,
            truncated=len(self.page_text) > max_chars,
            size_bytes=len(self.page_text.encode("utf-8")),
        )

    async def api_get(self, url: str, *, headers: Mapping[str, str], max_bytes: int, timeout: float) -> ApiAnswer:
        self._call("api_get", url, max_bytes=max_bytes, timeout=timeout)
        self.api_headers.append(dict(headers))
        data = self.api_text.encode()
        return ApiAnswer(
            status=self.api_status,
            mime=self.api_mime,
            text=data[:max_bytes].decode(errors="replace"),
            truncated=len(data) > max_bytes,
        )


_: WebTools = FakeWebTools()
