"""`HttpDownloader`: one file over HTTP(S), streamed to disk under a byte cap (UC-32, SPEC §3.2.14 `fetch_file`).

stdlib `urllib` in a worker thread (CONVENTIONS §6: a sync library runs via `asyncio.to_thread` in its adapter):
- **Only http and https**, redirects included: the opener has no `file:`, `ftp:` or `data:` handler, and a redirect
  to another scheme is not followed. TLS verifies against certifi's CA bundle.
- **Capped**: a `Content-Length` over `max_bytes` is refused before the body is read; a body that grows past it is
  cut off and its partial file deleted. The file is written as `<name>.part` (0600) and renamed when complete.
- **Bounded**: each socket operation times out (`SOCKET_TIMEOUT_MAX_S`, less if the call has less), the whole call
  runs under the caller's `timeout` through `core.retry.retrying`, and an abandoned worker stops at its next chunk.
- **Content type checked**: an HTML answer is not a file; it comes back as `PageLink`, and `LocalWebTools` asks the
  browser for the page's main image instead.

Retried (GET is idempotent; the partial file is discarded first): 5xx, 429 (`Retry-After` honoured), a reset or
stalled connection. Never: 4xx, DNS or TLS failures.
"""

import asyncio
import mimetypes
import os
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from http.client import HTTPException, HTTPMessage
from pathlib import Path
from typing import IO, Final, Protocol
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    Request,
    UnknownHandler,
)

import certifi

from yordamchi.adapters.web import translate
from yordamchi.core.errors import CallTimedOut, ProviderUnavailable, WebContentTooLarge, WebNoMedia
from yordamchi.core.retry import retrying
from yordamchi.ports.web import FetchedFile

MIB: Final = 1024 * 1024
CHUNK_BYTES: Final = 256 * 1024
SOCKET_TIMEOUT_MAX_S: Final = 30.0
ATTEMPTS: Final = 2
NAME_MAX_CHARS: Final = 120
HTML_MIMES: Final = frozenset({"text/html", "application/xhtml+xml"})
GENERIC_MIME: Final = "application/octet-stream"
ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
# Many sites refuse "Python-urllib"; a plain desktop browser string gets the same file a person would.
USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
_UNSAFE_NAME_CHARS: Final = frozenset('<>:"|?*')


class Body(Protocol):
    def read(self, amt: int, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-case names
    url: str  # after redirects
    body: Body


@dataclass(frozen=True, slots=True)
class PageLink:
    """The link answered with an HTML page, not a file."""

    url: str


type Opener = Callable[[str, float], HttpResponse]


class _SameSchemeRedirect(HTTPRedirectHandler):
    """Follows a redirect only to http(s); anything else surfaces as the 3xx itself."""

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        if urlsplit(newurl).scheme.lower() not in ALLOWED_SCHEMES:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_open(url: str, timeout: float) -> HttpResponse:
    """GET `url` with an opener that knows http and https only. An error status is returned, not raised, so the
    caller sees its headers (`Retry-After`)."""
    if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
        msg = "only http(s) URLs are fetched"
        raise ValueError(msg)
    opener = OpenerDirector()
    context = ssl.create_default_context(cafile=certifi.where())
    for handler in (
        UnknownHandler(),
        HTTPHandler(),
        HTTPSHandler(context=context),
        HTTPDefaultErrorHandler(),
        _SameSchemeRedirect(),
        HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})  # noqa: S310  # scheme checked above
    try:
        raw = opener.open(request, timeout=timeout)
    except HTTPError as error:
        raw = error
    headers = {name.lower(): value for name, value in raw.headers.items()}
    return HttpResponse(status=int(raw.getcode() or 0), headers=headers, url=str(raw.geturl()), body=raw)


# --- names and types -------------------------------------------------------------------------------------------------
def media_type(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length", "").strip()
    return int(raw) if raw.isdigit() else None


def safe_name(raw: str) -> str:
    """A bare file name: no directory part, no control or shell-awkward characters, no leading dot, ≤ 120 chars
    (the extension is kept when cutting)."""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if (not c.isprintable() or c in _UNSAFE_NAME_CHARS) else c for c in base).strip()
    cleaned = cleaned.lstrip(".").strip()
    if len(cleaned) > NAME_MAX_CHARS:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            cleaned = stem[: NAME_MAX_CHARS - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:NAME_MAX_CHARS]
    return cleaned or "file"


def file_name(disposition: str | None, url: str, mime: str) -> str:
    """`Content-Disposition`'s filename (RFC 6266/2231 aware), else the URL's last path segment, made safe; an
    extension from the content type is added when the name has none."""
    name = ""
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        name = message.get_filename() or ""
    if not name:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = safe_name(name)
    if "." not in name and (extension := mimetypes.guess_extension(mime or "")):
        name += extension
    return name


def file_mime(declared: str, name: str) -> str:
    if declared and declared != GENERIC_MIME:
        return declared
    guessed, _ = mimetypes.guess_type(name)
    return guessed or GENERIC_MIME


# --- the downloader --------------------------------------------------------------------------------------------------
class HttpDownloader:
    def __init__(self, *, opener: Opener = urllib_open, chunk_bytes: int = CHUNK_BYTES) -> None:
        self._opener = opener
        self._chunk_bytes = chunk_bytes

    async def fetch(
        self, url: str, *, site: str, dest_dir: Path, max_bytes: int, timeout: float
    ) -> FetchedFile | PageLink:
        abort = threading.Event()
        socket_timeout = max(1.0, min(SOCKET_TIMEOUT_MAX_S, timeout / 2))

        async def attempt() -> FetchedFile | PageLink:
            return await asyncio.to_thread(self._fetch, url, site, dest_dir, max_bytes, socket_timeout, abort)

        try:
            return await retrying(attempt, timeout=timeout, operation="fetch_file", attempts=ATTEMPTS)
        finally:
            abort.set()  # a worker left behind by a timeout stops at its next chunk

    def _fetch(
        self, url: str, site: str, dest_dir: Path, max_bytes: int, socket_timeout: float, abort: threading.Event
    ) -> FetchedFile | PageLink:
        try:
            response = self._opener(url, socket_timeout)
        except (OSError, HTTPException) as error:  # URLError is an OSError
            raise translate.from_network(error, site=site) from None
        try:
            if response.status >= 400:
                retry_after = translate.retry_after_s(response.headers)
                raise translate.from_http_status(response.status, site=site, retry_after=retry_after)
            if response.status >= 300:  # a redirect to a scheme we do not follow
                raise ProviderUnavailable(provider=site, retryable=False)
            declared = media_type(response.headers)
            if declared in HTML_MIMES:
                return PageLink(url=response.url)
            length = content_length(response.headers)
            if length is not None and length > max_bytes:
                raise WebContentTooLarge(limit_mb=max_bytes // MIB)
            name = file_name(response.headers.get("content-disposition"), response.url, declared)
            dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = dest_dir / name
            size = self._stream(response.body, path, max_bytes=max_bytes, site=site, abort=abort)
        finally:
            response.body.close()
        if size == 0:
            path.unlink(missing_ok=True)
            raise WebNoMedia(site=site)
        return FetchedFile(path=path, mime=file_mime(declared, name), size_bytes=size, source_url=response.url)

    def _stream(self, body: Body, path: Path, *, max_bytes: int, site: str, abort: threading.Event) -> int:
        part = path.with_name(path.name + ".part")
        total = 0
        try:
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as out:
                while True:
                    if abort.is_set():
                        raise CallTimedOut(operation="fetch_file")
                    try:
                        chunk = body.read(self._chunk_bytes)
                    except (OSError, HTTPException) as error:
                        raise translate.from_network(error, site=site) from None
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise WebContentTooLarge(limit_mb=max_bytes // MIB)
                    out.write(chunk)
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)  # gone already after a complete download
        return total
