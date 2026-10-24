"""`FakeWebTools`: writes small placeholder files into `dest_dir` and records each request."""

from pathlib import Path

from yordamchi.ports.web import DownloadedVideo, FetchedFile, Screenshot, WebTools


class FakeWebTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, Exception] = {}

    def _call(self, method: str, url: str) -> None:
        self.calls.append((method, url))
        if url in self.failures:
            raise self.failures[url]

    async def screenshot(self, url: str, *, dest_dir: Path, width: int, height: int, timeout: float) -> Screenshot:
        self._call("screenshot", url)
        viewport, full = dest_dir / "viewport.png", dest_dir / "full.png"
        viewport.write_bytes(b"\x89PNG viewport")
        full.write_bytes(b"\x89PNG full page")
        return Screenshot(viewport_png=viewport, full_page_png=full, title="Fake page")

    async def fetch_file(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> FetchedFile:
        self._call("fetch_file", url)
        path = dest_dir / "file.bin"
        path.write_bytes(b"fake file")
        return FetchedFile(path=path, mime="application/octet-stream", size_bytes=9, source_url=url)

    async def download_video(self, url: str, *, dest_dir: Path, max_bytes: int, timeout: float) -> DownloadedVideo:
        self._call("download_video", url)
        path = dest_dir / "video.mp4"
        path.write_bytes(b"fake mp4")
        return DownloadedVideo(path=path, mime="video/mp4", size_bytes=8, duration_s=1.0, title="Fake video")


_: WebTools = FakeWebTools()
