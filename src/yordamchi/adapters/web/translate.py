"""Vendor failures → the `ExternalError` hierarchy, for every part of `adapters/web` (CONVENTIONS §7).

Pure functions over exception types and messages, so each mapping has an offline test (`tests/integration/
test_web.py`). The vendor's message is read here and dropped: it can quote the URL, and a URL may carry a token, so
no message ever reaches a log line or the owner. `site` is the host name only.

Retryable (`core.retry` may try again inside the call's timeout): HTTP 5xx, a reset or stalled connection, a crashed
browser. Never retryable: 4xx, DNS or TLS failures, login walls, paywalls, bot checks, geo blocks.
"""

import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Final
from urllib.error import URLError

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from yt_dlp.networking.exceptions import HTTPError as YtHttpError  # type: ignore[import-untyped]
from yt_dlp.networking.exceptions import TransportError as YtTransportError
from yt_dlp.utils import GeoRestrictedError, UnsupportedError  # type: ignore[import-untyped]

from yordamchi.core.errors import (
    CallTimedOut,
    ExternalError,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
    VideoIsLive,
    VideoNotFound,
    WebAccessDenied,
    WebBotCheck,
    WebContentUnavailable,
    WebGeoBlocked,
    WebLoginRequired,
    WebNotAPage,
    WebNotFound,
    WebPaywalled,
    WebUnreachable,
)

BROWSER: Final = "Chromium"
CHROMIUM_BINARY: Final = "chromium (uv run playwright install chromium)"
FFMPEG_BINARY: Final = "ffmpeg"
RETRY_AFTER_MAX_S: Final = 120.0

# --- Playwright (Chromium net:: codes, lower case) -------------------------------------------------------------------
_PW_NOT_INSTALLED: Final = ("executable doesn't exist", "playwright install")
_PW_NOT_A_PAGE: Final = ("download is starting", "net::err_aborted")
_PW_UNREACHABLE: Final = (
    "err_name_not_resolved",
    "err_name_resolution_failed",
    "err_connection_refused",
    "err_address_unreachable",
    "err_address_invalid",
    "err_internet_disconnected",
    "err_cert_",
    "err_ssl_",
    "err_bad_ssl",
    "err_unsafe_port",
    "err_invalid_url",
)
_PW_TRANSIENT: Final = (
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_timed_out",
    "err_timed_out",
    "err_empty_response",
    "err_network_changed",
    "err_http2",
    "has been closed",  # TargetClosedError: the browser or page crashed under us
    "browser closed",
)


def _has(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def from_playwright(error: PlaywrightError, *, site: str, operation: str) -> ExternalError:
    """A Playwright failure while opening or capturing a page."""
    if isinstance(error, PlaywrightTimeoutError):
        return CallTimedOut(operation=operation)
    text = (error.message or str(error)).lower()
    if _has(text, _PW_NOT_INSTALLED):
        return NotInstalled(binary=CHROMIUM_BINARY)
    if _has(text, _PW_NOT_A_PAGE):
        return WebNotAPage(site=site)
    if _has(text, _PW_UNREACHABLE):
        return WebUnreachable(site=site)
    if _has(text, _PW_TRANSIENT):
        return ProviderUnavailable(provider=site if "err_" in text else BROWSER, retryable=True)
    return ProviderUnavailable(provider=BROWSER, retryable=False)


# --- HTTP (stdlib urllib downloads) ----------------------------------------------------------------------------------
def retry_after_s(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in seconds, capped; None when absent or an HTTP date (the default backoff applies then)."""
    raw = headers.get("retry-after", "").strip()
    return min(float(raw), RETRY_AFTER_MAX_S) if raw.isdigit() else None


_HTTP_FINAL: Final[Mapping[int, Callable[[str], ExternalError]]] = {
    401: lambda site: WebLoginRequired(site=site),
    402: lambda site: WebPaywalled(site=site),
    403: lambda site: WebAccessDenied(site=site),
    404: lambda site: WebNotFound(site=site),
    410: lambda site: WebNotFound(site=site),
    451: lambda site: WebGeoBlocked(site=site),
}


def from_http_status(status: int, *, site: str, retry_after: float | None = None) -> ExternalError:
    """An HTTP error status of a page or file. Never called for 2xx/3xx."""
    if (final := _HTTP_FINAL.get(status)) is not None:
        return final(site)
    if status == 429:
        return RateLimited(provider=site, retry_after=retry_after)
    return ProviderUnavailable(provider=site, retryable=status >= 500)


def from_network(error: OSError | HTTPException, *, site: str) -> ExternalError:
    """A connection-level failure of a download (no HTTP status)."""
    reason: object = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLError)):
        return WebUnreachable(site=site)
    if isinstance(reason, (TimeoutError, ConnectionError, HTTPException)):
        return ProviderUnavailable(provider=site, retryable=True)
    if isinstance(error, HTTPException):
        return ProviderUnavailable(provider=site, retryable=True)
    if isinstance(reason, str) and "unknown url type" in reason.lower():
        return WebUnreachable(site=site)
    return ProviderUnavailable(provider=site, retryable=False)


# --- yt-dlp ---------------------------------------------------------------------------------------------------------
_YT_BOT_CHECK: Final = ("not a bot",)
_YT_PAYWALL: Final = (
    "members-only",
    "members only",
    "available to this channel's members",
    "join this channel",
    "requires payment",
    "premium members",
    "premium subscription",
    "only available to music premium",
    "purchase",
    "rental",
    "paid content",
    "subscribers only",
    "subscription required",
)
_YT_LOGIN: Final = (
    "sign in to confirm your age",
    "age-restricted",
    "confirm your age",
    "private video",
    "this video is private",
    "only available for registered users",
    "login required",
    "requires login",
    "log in to",
    "--cookies",
    "cookies-from-browser",
    "requires authentication",
    "account is private",
)
_YT_GEO: Final = ("geo restriction", "geo-restricted", "available in your country", "from your location")
_YT_LIVE: Final = ("live event will begin", "premieres in", "this live event")
_YT_UNAVAILABLE: Final = (
    "video unavailable",
    "is unavailable",
    "has been removed",
    "has been terminated",
    "no longer available",
    "does not exist",
    "content isn't available",
    "this video is not available",
)
_YT_NOT_FOUND: Final = (
    "unsupported url",
    "no video formats found",
    "requested format is not available",
    "no video could be found",
    "no media found",
    "unable to extract",
)
_YT_FFMPEG_MISSING: Final = ("ffmpeg not found", "ffprobe and ffmpeg not found", "ffmpeg is not installed")
_YT_UNREACHABLE: Final = (
    "nodename nor servname",
    "name or service not known",
    "failed to resolve",
    "getaddrinfo failed",
    "connection refused",
    "certificate verify failed",
)
_YT_TRANSIENT: Final = ("timed out", "connection reset", "incompleteread", "remote end closed", "connection aborted")
_YT_STATUS: Final = ((429, "http error 429"), (404, "http error 404"), (410, "http error 410"), (403, "http error 403"))


def _cause(error: BaseException) -> BaseException | None:
    """yt-dlp wraps the real failure: `DownloadError.exc_info` holds it (an `ExtractorError`, or the network error
    underneath one)."""
    exc_info = getattr(error, "exc_info", None)
    if isinstance(exc_info, tuple) and len(exc_info) == 3 and isinstance(exc_info[1], BaseException):
        return exc_info[1]
    return error.__cause__


@dataclass(frozen=True, slots=True)
class _Failure:
    """What yt-dlp said: the outer error, the wrapped cause and the lower-cased text of both."""

    error: BaseException
    cause: BaseException | None
    text: str

    def says(self, needles: Iterable[str]) -> bool:
        return _has(self.text, needles)

    def is_a(self, kind: type[BaseException]) -> bool:
        return isinstance(self.error, kind) or isinstance(self.cause, kind)


def _http_cause(failure: _Failure, site: str) -> ExternalError | None:
    if isinstance(failure.cause, YtHttpError):
        return from_http_status(int(failure.cause.status), site=site)
    for status, needle in _YT_STATUS:
        if needle in failure.text:
            return from_http_status(status, site=site)
    return ProviderUnavailable(provider=site, retryable=True) if "http error 5" in failure.text else None


type _Rule = tuple[Callable[[_Failure], bool], Callable[[str], ExternalError]]

# In order: YouTube's bot check says "Sign in", and a members-only video adds a cookies hint, so both come before the
# login wall; content reasons come before transport ones.
_YT_RULES: Final[tuple[_Rule, ...]] = (
    (lambda f: f.says(_YT_BOT_CHECK), lambda site: WebBotCheck(site=site)),
    (lambda f: f.says(_YT_PAYWALL), lambda site: WebPaywalled(site=site)),
    (lambda f: f.says(_YT_LOGIN), lambda site: WebLoginRequired(site=site)),
    (lambda f: f.is_a(GeoRestrictedError) or f.says(_YT_GEO), lambda site: WebGeoBlocked(site=site)),
    (lambda f: f.says(_YT_LIVE), lambda site: VideoIsLive(site=site)),
    (lambda f: f.says(_YT_UNAVAILABLE), lambda site: WebContentUnavailable(site=site)),
    (lambda f: f.says(_YT_FFMPEG_MISSING), lambda _site: NotInstalled(binary=FFMPEG_BINARY)),
)
_YT_LATE_RULES: Final[tuple[_Rule, ...]] = (
    (lambda f: f.is_a(UnsupportedError) or f.says(_YT_NOT_FOUND), lambda site: VideoNotFound(site=site)),
    (lambda f: f.says(_YT_UNREACHABLE), lambda site: WebUnreachable(site=site)),
    (
        lambda f: f.says(_YT_TRANSIENT) or f.is_a(YtTransportError),
        lambda site: ProviderUnavailable(provider=site, retryable=True),
    ),
)


def from_ytdlp(error: BaseException, *, site: str) -> ExternalError:
    """A yt-dlp extraction or download failure (`_YT_RULES`, then the HTTP status, then `_YT_LATE_RULES`)."""
    cause = _cause(error)
    failure = _Failure(error=error, cause=cause, text=f"{error} {cause or ''}".lower().replace("\u2019", "'"))
    for matches, build in _YT_RULES:
        if matches(failure):
            return build(site)
    if (status_error := _http_cause(failure, site)) is not None:
        return status_error
    for matches, build in _YT_LATE_RULES:
        if matches(failure):
            return build(site)
    return ProviderUnavailable(provider=site, retryable=False)
