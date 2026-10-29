"""adapters/web offline: every error mapping, and each part driven through its seam — a fake Playwright page, a
fake HTTP opener, a fake `YoutubeDL`, and fake `ffmpeg`/`ffprobe` shell scripts run through `core.proc`.

The yt-dlp messages are recorded in `tests/fixtures/web/ytdlp_errors.json`; the real exception classes of Playwright,
yt-dlp and the stdlib are raised, so a vendor rename breaks a test here first."""

import http.client
import io
import json
import logging
import socket
import ssl
import stat
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
import yt_dlp  # type: ignore[import-untyped]
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import FloatRect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from yt_dlp.networking import Response  # type: ignore[import-untyped]
from yt_dlp.networking.exceptions import HTTPError as YtHttpError  # type: ignore[import-untyped]
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError  # type: ignore[import-untyped]

from yordamchi.adapters.web import download, translate
from yordamchi.adapters.web.browser import FULL_PAGE_MAX_PX, BrowserPage, LoadState, PlaywrightBrowser
from yordamchi.adapters.web.download import Body, HttpDownloader, HttpResponse, PageLink
from yordamchi.adapters.web.encode import FfmpegEncoder, encode_argv, parse_duration, pick_encoder
from yordamchi.adapters.web.tools import LocalWebTools
from yordamchi.adapters.web.ytdl import YoutubeDLLike, YtDlpDownloader
from yordamchi.core.errors import (
    AudioEncodeFailed,
    CallTimedOut,
    ErrorCode,
    ExternalError,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
    VideoEncodeFailed,
    VideoIsLive,
    VideoNotFound,
    VideoTooLong,
    WebContentTooLarge,
    WebGeoBlocked,
    WebLoginRequired,
    WebNoMedia,
    WebNotFound,
    WebUnreachable,
)
from yordamchi.ports.web import Clip, DownloadedVideo, FetchedFile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"
SITE = "example.com"
URL = "https://example.com/page"
MIB = 1024 * 1024


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def _write_png(path: Path) -> None:
    path.write_bytes(b"\x89PNG")


# --- translate: Playwright -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "code", "retryable"),
    [
        ("BrowserType.launch: Executable doesn't exist at /x/chrome-headless-shell", ErrorCode.NOT_INSTALLED, False),
        ("Page.goto: Download is starting", ErrorCode.WEB_NOT_A_PAGE, False),
        ("Page.goto: net::ERR_ABORTED at https://example.com/a.pdf", ErrorCode.WEB_NOT_A_PAGE, False),
        ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nope.example", ErrorCode.WEB_UNREACHABLE, False),
        ("Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:1", ErrorCode.WEB_UNREACHABLE, False),
        ("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://self.signed", ErrorCode.WEB_UNREACHABLE, False),
        ("Page.goto: net::ERR_CONNECTION_RESET at https://example.com", ErrorCode.PROVIDER_UNAVAILABLE, True),
        ("Target page, context or browser has been closed", ErrorCode.PROVIDER_UNAVAILABLE, True),
        ("Something new and strange", ErrorCode.PROVIDER_UNAVAILABLE, False),
    ],
)
def test_playwright_errors_map_to_codes(message: str, code: ErrorCode, retryable: bool) -> None:
    error = translate.from_playwright(PlaywrightError(message), site=SITE, operation="screenshot")
    assert (error.code, error.retryable) == (code, retryable)


def test_a_playwright_timeout_is_a_retryable_call_timeout() -> None:
    error = translate.from_playwright(PlaywrightTimeoutError("Timeout 1ms exceeded"), site=SITE, operation="screenshot")
    assert isinstance(error, CallTimedOut)
    assert error.retryable


# --- translate: HTTP and the network ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ErrorCode.WEB_LOGIN_REQUIRED, False),
        (402, ErrorCode.WEB_PAYWALLED, False),
        (403, ErrorCode.WEB_ACCESS_DENIED, False),
        (404, ErrorCode.WEB_NOT_FOUND, False),
        (410, ErrorCode.WEB_NOT_FOUND, False),
        (451, ErrorCode.WEB_GEO_BLOCKED, False),
        (429, ErrorCode.RATE_LIMITED, True),
        (500, ErrorCode.PROVIDER_UNAVAILABLE, True),
        (503, ErrorCode.PROVIDER_UNAVAILABLE, True),
        (418, ErrorCode.PROVIDER_UNAVAILABLE, False),
    ],
)
def test_http_statuses_map_to_codes_and_only_5xx_429_retry(status: int, code: ErrorCode, retryable: bool) -> None:
    error = translate.from_http_status(status, site=SITE, retry_after=3.0)
    assert (error.code, error.retryable) == (code, retryable)
    if isinstance(error, RateLimited):
        assert error.retry_after == 3.0


def test_retry_after_is_read_and_capped() -> None:
    assert translate.retry_after_s({"retry-after": "7"}) == 7.0
    assert translate.retry_after_s({"retry-after": "100000"}) == translate.RETRY_AFTER_MAX_S
    assert translate.retry_after_s({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert translate.retry_after_s({}) is None


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (URLError(socket.gaierror(8, "nodename nor servname provided")), ErrorCode.WEB_UNREACHABLE, False),
        (URLError(ConnectionRefusedError(61, "refused")), ErrorCode.WEB_UNREACHABLE, False),
        (URLError(ssl.SSLCertVerificationError("certificate verify failed")), ErrorCode.WEB_UNREACHABLE, False),
        (URLError("unknown url type: ftp"), ErrorCode.WEB_UNREACHABLE, False),
        (URLError(TimeoutError("timed out")), ErrorCode.PROVIDER_UNAVAILABLE, True),
        (TimeoutError("timed out"), ErrorCode.PROVIDER_UNAVAILABLE, True),
        (ConnectionResetError(54, "reset"), ErrorCode.PROVIDER_UNAVAILABLE, True),
        (http.client.IncompleteRead(b"x", 10), ErrorCode.PROVIDER_UNAVAILABLE, True),
        (OSError(5, "odd"), ErrorCode.PROVIDER_UNAVAILABLE, False),
    ],
)
def test_network_errors_map_to_codes(error: OSError, code: ErrorCode, retryable: bool) -> None:
    mapped = translate.from_network(error, site=SITE)
    assert (mapped.code, mapped.retryable) == (code, retryable)


# --- translate: yt-dlp ------------------------------------------------------------------------------------------------
def _ytdlp_cases() -> list[tuple[str, str, bool]]:
    data = json.loads((FIXTURES / "ytdlp_errors.json").read_text(encoding="utf-8"))
    return [(case["message"], case["code"], case["retryable"]) for case in data["cases"]]


@pytest.mark.parametrize(("message", "code", "retryable"), _ytdlp_cases())
def test_recorded_ytdlp_messages_map_to_codes(message: str, code: str, retryable: bool) -> None:
    mapped = translate.from_ytdlp(DownloadError(message), site="youtube.com")
    assert (mapped.code.value, mapped.retryable) == (code, retryable)
    assert str(mapped) == code  # no vendor text reaches a log line through str()


def test_ytdlp_exception_types_are_used_when_the_text_says_nothing() -> None:
    def wrapped(cause: BaseException) -> DownloadError:
        return DownloadError("ERROR: failed", (type(cause), cause, None))

    assert isinstance(translate.from_ytdlp(wrapped(GeoRestrictedError("blocked")), site=SITE), WebGeoBlocked)
    assert isinstance(translate.from_ytdlp(wrapped(UnsupportedError("https://x")), site=SITE), VideoNotFound)
    response = Response(io.BytesIO(b""), "https://x", {}, status=401)
    assert isinstance(translate.from_ytdlp(wrapped(YtHttpError(response)), site=SITE), WebLoginRequired)
    extractor = ExtractorError("This video is private", expected=True)
    assert isinstance(translate.from_ytdlp(extractor, site=SITE), WebLoginRequired)


# --- browser ---------------------------------------------------------------------------------------------------------
class FakePage:
    """Scripted Playwright page: `goto_errors` are raised by successive `goto` calls; screenshots write files."""

    def __init__(self, *, height: int = 3000, image: object = "https://cdn.example.com/a.jpg") -> None:
        self.goto_errors: list[Exception] = []
        self.load_errors: list[Exception] = []
        self.height = height
        self.image = image
        self.shots: list[dict[str, object]] = []
        self.gotos = 0

    async def goto(self, url: str, *, timeout: float, wait_until: LoadState) -> object:
        self.gotos += 1
        assert timeout > 0
        assert wait_until == "domcontentloaded"
        if self.goto_errors:
            raise self.goto_errors.pop(0)
        return None

    async def wait_for_load_state(self, state: LoadState, *, timeout: float) -> None:
        if self.load_errors:
            raise self.load_errors.pop(0)

    async def title(self) -> str:
        return "  Example Domain  "

    async def evaluate(self, expression: str) -> object:
        return self.height if "scrollHeight" in expression else self.image

    async def screenshot(
        self, *, path: Path, full_page: bool, clip: FloatRect | None, timeout: float, animations: str
    ) -> bytes:
        self.shots.append({"full_page": full_page, "clip": clip})
        _write_png(path)
        return b"\x89PNG"


def _opener(page: FakePage) -> Callable[[int, int], Any]:
    @asynccontextmanager
    async def open_page(width: int, height: int) -> AsyncIterator[BrowserPage]:
        yield page

    return open_page


async def test_screenshot_writes_both_private_pngs(tmp_path: Path) -> None:
    page = FakePage()
    shot = await PlaywrightBrowser(opener=_opener(page)).screenshot(
        URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
    )
    assert shot.title == "Example Domain"
    assert [s["full_page"] for s in page.shots] == [False, True]
    assert page.shots[1]["clip"] is None
    assert {_mode(shot.viewport_png), _mode(shot.full_page_png)} == {0o600}


async def test_an_endless_page_is_clipped(tmp_path: Path) -> None:
    page = FakePage(height=FULL_PAGE_MAX_PX * 5)
    await PlaywrightBrowser(opener=_opener(page)).screenshot(
        URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
    )
    assert page.shots[1]["clip"] == {"x": 0, "y": 0, "width": 1280, "height": FULL_PAGE_MAX_PX}


async def test_a_page_that_never_settles_is_still_captured(tmp_path: Path) -> None:
    page = FakePage()
    page.load_errors = [PlaywrightTimeoutError("load"), PlaywrightTimeoutError("networkidle")]
    shot = await PlaywrightBrowser(opener=_opener(page)).screenshot(
        URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
    )
    assert shot.full_page_png.exists()


async def test_a_crashed_browser_is_retried_once(tmp_path: Path) -> None:
    page = FakePage()
    page.goto_errors = [PlaywrightError("Target page, context or browser has been closed")]
    await PlaywrightBrowser(opener=_opener(page)).screenshot(
        URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
    )
    assert page.gotos == 2


async def test_an_unknown_host_is_not_retried(tmp_path: Path) -> None:
    page = FakePage()
    page.goto_errors = [PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://nope")] * 3
    with pytest.raises(WebUnreachable):
        await PlaywrightBrowser(opener=_opener(page)).screenshot(
            URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
        )
    assert page.gotos == 1


async def test_a_page_that_keeps_timing_out_gives_up_with_a_timeout(tmp_path: Path) -> None:
    page = FakePage()
    page.goto_errors = [PlaywrightTimeoutError("Timeout exceeded")] * 3
    with pytest.raises(CallTimedOut):
        await PlaywrightBrowser(opener=_opener(page)).screenshot(
            URL, site=SITE, dest_dir=tmp_path, width=1280, height=800, timeout=5.0
        )
    assert page.gotos == 2


async def test_main_image_url_comes_from_the_page() -> None:
    browser = PlaywrightBrowser(opener=_opener(FakePage()))
    found = await browser.main_image_url(URL, site=SITE, width=1280, height=800, timeout=5.0)
    assert found == "https://cdn.example.com/a.jpg"
    none = PlaywrightBrowser(opener=_opener(FakePage(image=None)))
    assert await none.main_image_url(URL, site=SITE, width=1280, height=800, timeout=5.0) is None


# --- HTTP downloads --------------------------------------------------------------------------------------------------
class FailingBody:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def read(self, amt: int, /) -> bytes:
        raise self.error

    def close(self) -> None:
        return None


class FakeOpener:
    """Returns the scripted responses (or raises the scripted errors) in order, and records each URL."""

    def __init__(self, *results: HttpResponse | Exception) -> None:
        self.results = list(results)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> HttpResponse:
        self.urls.append(url)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _response(
    body: bytes | Body = b"", *, status: int = 200, url: str = URL, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    stream: Body = io.BytesIO(body) if isinstance(body, bytes) else body
    return HttpResponse(status=status, headers=dict(headers or {}), url=url, body=stream)


async def _fetch(opener: FakeOpener, dest: Path, *, max_bytes: int = 10 * MIB) -> FetchedFile | PageLink:
    return await HttpDownloader(opener=opener, chunk_bytes=4).fetch(
        URL, site=SITE, dest_dir=dest, max_bytes=max_bytes, timeout=5.0
    )


async def test_a_file_is_streamed_to_a_private_file_named_by_the_server(tmp_path: Path) -> None:
    headers = {"content-type": "application/pdf", "content-disposition": "attachment; filename*=UTF-8''na%C3%AFve.pdf"}
    fetched = await _fetch(FakeOpener(_response(b"%PDF-1.7 hello", headers=headers)), tmp_path)
    assert isinstance(fetched, FetchedFile)
    assert fetched.path == tmp_path / "naïve.pdf"
    assert fetched.path.read_bytes() == b"%PDF-1.7 hello"
    assert (fetched.mime, fetched.size_bytes) == ("application/pdf", 14)
    assert _mode(fetched.path) == 0o600
    assert [name for name in _names(tmp_path) if name.endswith(".part")] == []


async def test_an_html_answer_is_a_page_not_a_file(tmp_path: Path) -> None:
    result = await _fetch(
        FakeOpener(_response(b"<html>", headers={"content-type": "text/html; charset=utf-8"})), tmp_path
    )
    assert result == PageLink(url=URL)
    assert _names(tmp_path) == []


async def test_a_declared_size_over_the_cap_is_refused_before_reading(tmp_path: Path) -> None:
    body = FailingBody(AssertionError("must not be read"))
    response = _response(body, headers={"content-type": "video/mp4", "content-length": str(60 * MIB)})
    with pytest.raises(WebContentTooLarge) as caught:
        await _fetch(FakeOpener(response), tmp_path, max_bytes=50 * MIB)
    assert caught.value.params == {"limit_mb": 50}


async def test_a_body_growing_past_the_cap_is_cut_and_deleted(tmp_path: Path) -> None:
    with pytest.raises(WebContentTooLarge):
        await _fetch(FakeOpener(_response(b"x" * 64, headers={"content-type": "image/png"})), tmp_path, max_bytes=10)
    assert _names(tmp_path) == []


async def test_a_5xx_is_retried_then_succeeds(tmp_path: Path) -> None:
    opener = FakeOpener(_response(status=503), _response(b"ok", headers={"content-type": "text/plain"}))
    fetched = await _fetch(opener, tmp_path)
    assert isinstance(fetched, FetchedFile)
    assert len(opener.urls) == 2


async def test_a_429_honours_retry_after(tmp_path: Path) -> None:
    opener = FakeOpener(_response(status=429, headers={"retry-after": "0"}), _response(b"ok"))
    assert isinstance(await _fetch(opener, tmp_path), FetchedFile)
    assert len(opener.urls) == 2


async def test_a_404_is_final(tmp_path: Path) -> None:
    opener = FakeOpener(_response(status=404), _response(b"never"))
    with pytest.raises(WebNotFound):
        await _fetch(opener, tmp_path)
    assert len(opener.urls) == 1


async def test_dns_failure_is_final(tmp_path: Path) -> None:
    opener = FakeOpener(URLError(socket.gaierror(8, "nodename")), _response(b"never"))
    with pytest.raises(WebUnreachable):
        await _fetch(opener, tmp_path)
    assert len(opener.urls) == 1


async def test_a_reset_mid_body_is_retried_without_leftovers(tmp_path: Path) -> None:
    opener = FakeOpener(_response(FailingBody(ConnectionResetError(54, "reset"))), _response(b"fine"))
    fetched = await _fetch(opener, tmp_path)
    assert isinstance(fetched, FetchedFile)
    assert _names(tmp_path) == ["page"]


async def test_an_empty_body_is_no_media(tmp_path: Path) -> None:
    with pytest.raises(WebNoMedia):
        await _fetch(FakeOpener(_response(b"", headers={"content-type": "image/png"})), tmp_path)


def test_file_names_are_safe() -> None:
    assert download.file_name('attachment; filename="../../etc/passwd"', URL, "") == "passwd"
    assert download.file_name(None, "https://x.uz/a/b/photo", "image/png") == "photo.png"
    assert download.file_name(None, "https://x.uz/", "") == "file"
    assert download.file_name(None, "https://x.uz/.bashrc", "text/plain") == "bashrc.txt"  # no hidden files
    long_name = download.safe_name("a" * 300 + ".jpeg")
    assert len(long_name) == download.NAME_MAX_CHARS
    assert long_name.endswith(".jpeg")
    assert download.file_mime("", "a.pdf") == "application/pdf"
    assert download.file_mime("application/octet-stream", "x") == "application/octet-stream"


def test_only_http_links_are_opened() -> None:
    with pytest.raises(ValueError, match="http"):
        download.urllib_open("file:///etc/passwd", 1.0)


def test_redirects_to_other_schemes_are_not_followed() -> None:
    import urllib.request
    from email.message import Message

    handler = download._SameSchemeRedirect()
    request = urllib.request.Request(URL)
    headers = http.client.HTTPMessage()
    assert isinstance(headers, Message)
    assert handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, "file:///etc/passwd") is None
    followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, "https://example.org/x")
    assert followed is not None


# --- yt-dlp -----------------------------------------------------------------------------------------------------------
class FakeYdl:
    """Plays `YoutubeDL`: `extract_info` returns `info`, `process_ie_result` writes `video.mp4` and calls the hooks."""

    def __init__(self, params: dict[str, Any], script: "YdlScript") -> None:
        self.params = params
        self.script = script

    def extract_info(self, url: str, download: bool = True) -> object:
        assert download is False
        if self.script.extract_error is not None:
            raise self.script.extract_error
        return self.script.info

    def process_ie_result(self, ie_result: dict[str, object], download: bool = True) -> object:
        self.script.processed = True
        for hook in self.params["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": self.script.downloaded})
        if not self.script.write:
            return ie_result
        path = Path(self.params["paths"]["home"]) / "video.mp4"
        path.write_bytes(b"\0" * self.script.size)
        return {**ie_result, "requested_downloads": [{"filepath": str(path)}]}


class YdlScript:
    def __init__(self, info: object, *, size: int = 1000, downloaded: int = 1000) -> None:
        self.info = info
        self.size = size
        self.downloaded = downloaded
        self.write = True
        self.extract_error: Exception | None = None
        self.processed = False
        self.params: dict[str, Any] = {}

    def factory(self, params: dict[str, Any]) -> AbstractContextManager[YoutubeDLLike]:
        self.params = params

        @contextmanager
        def opened() -> Iterator[YoutubeDLLike]:
            yield FakeYdl(params, self)

        return opened()


INFO: dict[str, Any] = {
    "id": "abc",
    "title": "A video",
    "duration": 125,
    "height": 720,
    "webpage_url": "https://www.youtube.com/watch?v=abc",
}


def _ytdl(tmp_path: Path, script: YdlScript, *, node: Path | None = None) -> YtDlpDownloader:
    return YtDlpDownloader(ffmpeg_dir=tmp_path / "bin", node=node, cache_dir=None, factory=script.factory)


async def _video(
    downloader: YtDlpDownloader, dest: Path, *, max_duration_s: float | None = 1400.0, clip: Clip | None = None
) -> DownloadedVideo:
    return await downloader.download(
        "https://www.youtube.com/watch?v=abc",
        site="youtube.com",
        dest_dir=dest,
        max_bytes=500 * MIB,
        format_selector="bv*+ba/b",
        max_duration_s=max_duration_s,
        clip=clip,
        timeout=5.0,
    )


async def test_a_video_is_downloaded_with_a_clean_profile(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n")
    script = YdlScript(dict(INFO))
    video = await _video(_ytdl(tmp_path, script, node=node), tmp_path / "out")
    assert (video.size_bytes, video.duration_s, video.height, video.title) == (1000, 125.0, 720, "A video")
    assert video.webpage_url == INFO["webpage_url"]
    assert _mode(video.path) == 0o600
    params = script.params
    assert params["js_runtimes"] == {"node": {"path": str(node)}}
    assert params["ffmpeg_location"] == str(tmp_path / "bin")
    assert params["cookiefile"] is None
    assert params["usenetrc"] is False
    assert "cookiesfrombrowser" not in params
    assert params["remote_components"] == []
    assert params["noplaylist"] is True
    assert params["max_filesize"] == 500 * MIB
    assert "download_ranges" not in params


def test_the_params_are_accepted_by_the_real_youtubedl(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO))
    downloader = YtDlpDownloader(ffmpeg_dir=tmp_path, node=None, factory=script.factory)
    from yordamchi.adapters.web.ytdl import _Job, _Sink

    job = _Job("https://x", "x", tmp_path, 10, "w", None, Clip(1, 5), 5.0)
    with yt_dlp.YoutubeDL(downloader.params(job, _Sink(), lambda _progress: None)) as ydl:
        assert set(ydl.params["js_runtimes"]) == {"node"}
        assert ydl.params["remote_components"] == set()
        assert ydl.params["force_keyframes_at_cuts"] is True


async def test_a_clip_asks_for_its_range_and_reports_its_length(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO, duration=4000))
    video = await _video(_ytdl(tmp_path, script), tmp_path / "out", max_duration_s=None, clip=Clip(60, 300))
    assert video.duration_s == 240
    assert "download_ranges" in script.params
    assert script.params["force_keyframes_at_cuts"] is True


async def test_a_too_long_video_is_refused_before_downloading(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO, duration=5400))
    with pytest.raises(VideoTooLong) as caught:
        await _video(_ytdl(tmp_path, script), tmp_path / "out")
    assert caught.value.params == {"minutes": 90}
    assert caught.value.link == INFO["webpage_url"]
    assert not script.processed


async def test_a_live_stream_is_refused(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO, live_status="is_live"))
    with pytest.raises(VideoIsLive):
        await _video(_ytdl(tmp_path, script), tmp_path / "out")
    assert not script.processed


async def test_a_playlist_gives_its_first_video(tmp_path: Path) -> None:
    script = YdlScript({"_type": "playlist", "entries": [dict(INFO, title="first"), dict(INFO, title="second")]})
    video = await _video(_ytdl(tmp_path, script), tmp_path / "out")
    assert video.title == "first"


async def test_an_empty_playlist_is_no_video(tmp_path: Path) -> None:
    with pytest.raises(VideoNotFound):
        await _video(_ytdl(tmp_path, YdlScript({"_type": "playlist", "entries": []})), tmp_path / "out")


async def test_a_download_growing_past_the_cap_is_stopped(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO), downloaded=600 * MIB)
    with pytest.raises(WebContentTooLarge) as caught:
        await _video(_ytdl(tmp_path, script), tmp_path / "out")
    assert caught.value.params == {"limit_mb": 500}


async def test_a_declared_size_over_the_cap_is_too_large(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO))
    script.write = False

    def factory(params: dict[str, Any]) -> AbstractContextManager[YoutubeDLLike]:
        params["logger"].debug("[download] File is larger than max-filesize (9 bytes > 1 bytes). Aborting.")
        return YdlScript.factory(script, params)

    downloader = YtDlpDownloader(ffmpeg_dir=tmp_path, node=None, factory=factory)
    with pytest.raises(WebContentTooLarge):
        await _video(downloader, tmp_path / "out")


async def test_no_file_and_no_reason_is_no_video(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO))
    script.write = False
    with pytest.raises(VideoNotFound):
        await _video(_ytdl(tmp_path, script), tmp_path / "out")


async def test_a_login_wall_is_final(tmp_path: Path) -> None:
    script = YdlScript(dict(INFO))
    script.extract_error = DownloadError("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
    with pytest.raises(WebLoginRequired):
        await _video(_ytdl(tmp_path, script), tmp_path / "out")


async def test_a_transient_failure_is_retried(tmp_path: Path) -> None:
    calls: list[int] = []
    script = YdlScript(dict(INFO))

    def factory(params: dict[str, Any]) -> AbstractContextManager[YoutubeDLLike]:
        calls.append(1)
        script.extract_error = DownloadError("ERROR: HTTP Error 503: Service Unavailable") if len(calls) == 1 else None
        return YdlScript.factory(script, params)

    video = await _video(YtDlpDownloader(ffmpeg_dir=tmp_path, node=None, factory=factory), tmp_path / "out")
    assert video.size_bytes == 1000
    assert len(calls) == 2


# --- ffmpeg -----------------------------------------------------------------------------------------------------------
def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)
    return path


def _fake_ffmpeg(bin_dir: Path, *, out_bytes: int = 4096, exit_code: int = 0, encoders: str = "libx264") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    return _script(
        bin_dir / "ffmpeg",
        f'case "$*" in *-encoders*) echo " V....D {encoders}             H.264"; exit 0;; esac\n'
        "for last; do :; done\n"
        f'echo "$@" > "{bin_dir}/argv.txt"\n'
        f'head -c {out_bytes} /dev/zero > "$last"\n'
        f"exit {exit_code}\n",
    )


def _fake_ffprobe(bin_dir: Path, *, prints: str = "12.5") -> Path:
    return _script(bin_dir / "ffprobe", f'echo "{prints}"\n')


def _encoder(bin_dir: Path) -> FfmpegEncoder:
    return FfmpegEncoder(ffmpeg=bin_dir / "ffmpeg", ffprobe=bin_dir / "ffprobe", child_path="/usr/bin:/bin")


async def test_reencode_writes_a_private_h264_mp4(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_ffmpeg(bin_dir)
    _fake_ffprobe(bin_dir)
    source = tmp_path / "video.webm"
    source.write_bytes(b"src")
    video = await _encoder(bin_dir).reencode(
        source, dest_dir=tmp_path, video_kbps=500, audio_kbps=64, max_height=360, timeout=10.0
    )
    assert video.path == tmp_path / "video-500k.mp4"
    assert (video.size_bytes, video.duration_s, video.height, video.mime) == (4096, 12.5, 360, "video/mp4")
    assert _mode(video.path) == 0o600
    argv = (bin_dir / "argv.txt").read_text()
    assert "-c:v libx264 -b:v 500k -maxrate 500k -bufsize 1000k -preset veryfast" in argv
    assert "-b:a 64k" in argv
    assert "scale=-2:'min(360,ih)'" in argv


async def test_reencode_failure_leaves_no_file(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_ffmpeg(bin_dir, exit_code=1)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"src")
    with pytest.raises(VideoEncodeFailed):
        await _encoder(bin_dir).reencode(
            source, dest_dir=tmp_path, video_kbps=500, audio_kbps=64, max_height=360, timeout=10.0
        )
    assert not (tmp_path / "video-500k.mp4").exists()


async def test_a_missing_ffmpeg_is_not_installed(tmp_path: Path) -> None:
    with pytest.raises(NotInstalled) as caught:
        await _encoder(tmp_path / "nowhere").reencode(
            tmp_path / "v.mp4", dest_dir=tmp_path, video_kbps=500, audio_kbps=64, max_height=360, timeout=10.0
        )
    assert caught.value.params == {"binary": "ffmpeg"}


async def test_a_hung_ffmpeg_is_killed_at_the_timeout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "ffmpeg", 'case "$*" in *-encoders*) echo " V....D libx264 x"; exit 0;; esac\nsleep 30\n')
    with pytest.raises(CallTimedOut):
        await _encoder(bin_dir).reencode(
            tmp_path / "v.mp4", dest_dir=tmp_path, video_kbps=500, audio_kbps=64, max_height=360, timeout=0.5
        )


async def test_duration_is_none_when_ffprobe_cannot_tell(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert await _encoder(bin_dir).duration(tmp_path / "x.mp4") is None  # no ffprobe
    _fake_ffprobe(bin_dir, prints="N/A")
    assert await _encoder(bin_dir).duration(tmp_path / "x.mp4") is None


def test_encoder_choice_and_argv() -> None:
    assert pick_encoder(b" V....D libx264  H.264\n V....D h264_videotoolbox  VT\n") == "libx264"
    assert pick_encoder(b" V....D h264_videotoolbox  VT\n") == "h264_videotoolbox"
    argv = encode_argv(
        Path("/f"),
        Path("/in"),
        Path("/out.mp4"),
        encoder="h264_videotoolbox",
        video_kbps=300,
        audio_kbps=64,
        max_height=240,
    )
    assert "-preset" not in argv
    assert argv[-1] == "/out.mp4"
    assert parse_duration(b"61.25\n") == 61.25
    assert parse_duration(b"") is None


# --- LocalWebTools ----------------------------------------------------------------------------------------------------
def _tools(tmp_path: Path, *, page: FakePage, opener: FakeOpener, script: YdlScript | None = None) -> LocalWebTools:
    bin_dir = tmp_path / "bin"
    return LocalWebTools(
        browser=PlaywrightBrowser(opener=_opener(page)),
        http=HttpDownloader(opener=opener),
        videos=YtDlpDownloader(ffmpeg_dir=bin_dir, node=None, factory=(script or YdlScript(dict(INFO))).factory),
        encoder=_encoder(bin_dir),
    )


async def test_a_page_link_fetches_its_main_image(tmp_path: Path) -> None:
    opener = FakeOpener(
        _response(b"<html>", headers={"content-type": "text/html"}),
        _response(b"\xff\xd8jpeg", url="https://cdn.example.com/a.jpg", headers={"content-type": "image/jpeg"}),
    )
    fetched = await _tools(tmp_path, page=FakePage(), opener=opener).fetch_file(
        URL, dest_dir=tmp_path / "out", max_bytes=MIB, timeout=5.0
    )
    assert opener.urls == [URL, "https://cdn.example.com/a.jpg"]
    assert (fetched.path.name, fetched.mime) == ("a.jpg", "image/jpeg")


async def test_a_page_without_an_image_is_no_media(tmp_path: Path) -> None:
    opener = FakeOpener(_response(b"<html>", headers={"content-type": "text/html"}))
    with pytest.raises(WebNoMedia):
        await _tools(tmp_path, page=FakePage(image=None), opener=opener).fetch_file(
            URL, dest_dir=tmp_path / "out", max_bytes=MIB, timeout=5.0
        )


async def test_an_image_that_is_another_page_is_no_media(tmp_path: Path) -> None:
    html = {"content-type": "text/html"}
    opener = FakeOpener(_response(b"<html>", headers=html), _response(b"<html>", headers=html))
    with pytest.raises(WebNoMedia):
        await _tools(tmp_path, page=FakePage(), opener=opener).fetch_file(
            URL, dest_dir=tmp_path / "out", max_bytes=MIB, timeout=5.0
        )


async def test_a_video_without_a_duration_is_probed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ffprobe(bin_dir, prints="33.0")
    script = YdlScript({k: v for k, v in INFO.items() if k != "duration"})
    video = await _tools(tmp_path, page=FakePage(), opener=FakeOpener(), script=script).download_video(
        "https://vimeo.com/1",
        dest_dir=tmp_path / "out",
        max_bytes=MIB,
        format_selector="w",
        max_duration_s=100.0,
        clip=None,
        timeout=5.0,
    )
    assert video.duration_s == 33.0


async def test_calls_are_logged_without_urls_or_hosts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret_url = "https://private.example.org/doc?token=abc123"
    opener = FakeOpener(_response(status=404, url=secret_url))
    with caplog.at_level(logging.INFO), pytest.raises(WebNotFound):
        await _tools(tmp_path, page=FakePage(), opener=opener).fetch_file(
            secret_url, dest_dir=tmp_path / "out", max_bytes=MIB, timeout=5.0
        )
    records = [r for r in caplog.records if r.getMessage() == "web_call_finished"]
    assert len(records) == 1
    dumped = repr(records[0].__dict__)
    assert "private.example.org" not in dumped
    assert "abc123" not in dumped
    assert "web_not_found" in dumped


async def test_an_encode_past_its_timeout_is_a_call_timeout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "ffmpeg", 'case "$*" in *-encoders*) echo " V....D libx264 x"; exit 0;; esac\nsleep 30\n')
    with pytest.raises(CallTimedOut):
        await _tools(tmp_path, page=FakePage(), opener=FakeOpener()).reencode_video(
            tmp_path / "v.mp4", dest_dir=tmp_path, video_kbps=300, audio_kbps=64, max_height=240, timeout=0.5
        )


def test_every_mapping_is_an_external_error() -> None:
    samples = [
        translate.from_http_status(500, site=SITE),
        translate.from_network(OSError(), site=SITE),
        translate.from_ytdlp(DownloadError("x"), site=SITE),
        translate.from_playwright(PlaywrightError("x"), site=SITE, operation="screenshot"),
    ]
    assert all(isinstance(error, ExternalError) for error in samples)
    assert isinstance(samples[0], ProviderUnavailable)


# --- audio compression (UC-37) ----------------------------------------------------------------------------------------
async def test_compress_audio_writes_a_private_mono_mp3(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_ffmpeg(bin_dir)
    _fake_ffprobe(bin_dir, prints="1834.2")
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"src")
    audio = await _encoder(bin_dir).compress_audio(source, dest_dir=tmp_path, kbps=32, timeout=10.0)
    assert audio.path == tmp_path / "audio-32k.mp3"
    assert (audio.size_bytes, audio.duration_s, audio.mime) == (4096, 1834.2, "audio/mpeg")
    assert _mode(audio.path) == 0o600
    argv = (bin_dir / "argv.txt").read_text()
    assert "-map 0:a:0 -vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 32k" in argv


async def test_compress_audio_failure_leaves_no_file(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_ffmpeg(bin_dir, exit_code=1)
    source = tmp_path / "audio.bin"
    source.write_bytes(b"src")
    with pytest.raises(AudioEncodeFailed):
        await _encoder(bin_dir).compress_audio(source, dest_dir=tmp_path, kbps=32, timeout=10.0)
    assert not (tmp_path / "audio-32k.mp3").exists()


async def test_compress_audio_without_ffmpeg_is_not_installed(tmp_path: Path) -> None:
    with pytest.raises(NotInstalled):
        await _encoder(tmp_path / "nowhere").compress_audio(tmp_path / "a.mp3", dest_dir=tmp_path, kbps=32, timeout=5)
