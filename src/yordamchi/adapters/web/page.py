"""`PageReader`: one web page's readable text, for the assistant to read as data (UC-31, SPEC §3.2.14; T73).

stdlib only, in a worker thread (CONVENTIONS §6), sharing the T74 downloader's opener (`download.urllib_open`):
http(s) only with redirects, TLS against certifi, no cookies, no login, a desktop User-Agent.

- **Bounded.** Each socket operation times out (`download.SOCKET_TIMEOUT_MAX_S`, less if the call has less); the call
  runs under the caller's `timeout` through `core.retry.retrying`; an abandoned worker stops at its next chunk. At most
  `max_bytes` of HTML are read: a longer page is read up to there and marked `truncated`, not refused.
- **Readable text, not markup.** `html.parser` with entities decoded; scripts, styles, navigation, forms, footers,
  asides and anything hidden (`hidden`, `aria-hidden="true"`, `display:none`, `visibility:hidden`) are dropped, so
  the text a person sees is what the model gets, and hidden instructions are not passed on. Headings become `#`
  lines and list items `-` lines; `<article>`/`<main>` win when they hold the bulk of the text.
- **Login walls and paywalls are recognised**, not summarised: HTTP 401/402/403 (via `translate`), a password field on
  a page with little text, or a paywall marker (`isAccessibleForFree: false`, a `paywall` class or id) next to a
  short text. A page with almost no text (a JavaScript app) is `PageNoText`; a file is `PageNotReadable`.

Retried (GET is idempotent): 5xx, 429 (`Retry-After` honoured), a reset or stalled connection. Never: 4xx, DNS, TLS.
Nothing read here reaches a log line: callers log the outcome, the size and the duration only.
"""

import asyncio
import re
import threading
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.client import HTTPException
from typing import Final

from yordamchi.adapters.web import translate
from yordamchi.adapters.web.download import (
    CHUNK_BYTES,
    SOCKET_TIMEOUT_MAX_S,
    Body,
    Opener,
    media_type,
    urllib_open,
)
from yordamchi.core.errors import (
    CallTimedOut,
    PageNoText,
    PageNotReadable,
    ProviderUnavailable,
    WebLoginRequired,
    WebPaywalled,
)
from yordamchi.core.retry import retrying
from yordamchi.ports.web import PageText

OPERATION: Final = "read_page"
ATTEMPTS: Final = 2
HTML_MIMES: Final = frozenset({"text/html", "application/xhtml+xml"})
TEXT_MIMES: Final = frozenset({"text/plain", "text/markdown"})
SNIFF_BYTES: Final = 2048
MIN_TEXT_CHARS: Final = 80  # less than this is a shell, not a page
LOGIN_MAX_TEXT_CHARS: Final = 1500  # a password field next to less text than this is a login page
PAYWALL_MAX_TEXT_CHARS: Final = 2500  # a paywall marker next to less text than this is a teaser, not the article
MAIN_MIN_SHARE: Final = 0.4  # <article>/<main> is used when it holds at least this share of the text
TITLE_MAX_CHARS: Final = 300

_SKIP_TAGS: Final = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "nav",
        "aside",
        "footer",
        "form",
        "button",
        "select",
        "dialog",
        "menu",
    }
)
_VOID_TAGS: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_HEADINGS: Final = frozenset({"h1", "h2", "h3"})
_MAIN_TAGS: Final = frozenset({"article", "main"})
_HIDDEN_STYLE: Final = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_NOT_FREE: Final = re.compile(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', re.IGNORECASE)
_META_CHARSET: Final = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_.:-]{1,40})""", re.IGNORECASE)
_SPACES: Final = re.compile(r"\s+")


# --- extraction (pure) -----------------------------------------------------------------------------------------------
@dataclass
class Extracted:
    """What one HTML document holds, as a reader sees it."""

    title: str = ""
    description: str = ""
    paragraphs: list[str] = field(default_factory=list)
    main_paragraphs: list[str] = field(default_factory=list)
    og_title: str = ""
    has_password: bool = False
    paywall_marker: bool = False

    def body(self) -> list[str]:
        """`<article>`/`<main>` when it holds the bulk of the text (less chrome), else everything readable."""
        total = sum(len(text) for text in self.paragraphs)
        main = sum(len(text) for text in self.main_paragraphs)
        return self.main_paragraphs if total and main >= MAIN_MIN_SHARE * total else self.paragraphs


class _Readable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = Extracted()
        self._skip: tuple[str, int] | None = None  # the tag whose subtree is dropped, and its nesting depth
        self._main_depth = 0
        self._in_title = False
        self._in_jsonld = False
        self._title: list[str] = []
        self._line: list[str] = []
        self._prefix = ""

    # A tag's subtree is dropped when it is chrome or hidden; nested tags of the same name are counted.
    @staticmethod
    def _hidden(attrs: dict[str, str]) -> bool:
        return (
            "hidden" in attrs
            or attrs.get("aria-hidden", "").strip().lower() == "true"
            or bool(_HIDDEN_STYLE.search(attrs.get("style", "")))
        )

    def _flush(self) -> None:
        text = _SPACES.sub(" ", "".join(self._line)).strip()
        self._line = []
        if text:
            line = f"{self._prefix}{text}"
            if not self.out.paragraphs or self.out.paragraphs[-1] != line:
                self.out.paragraphs.append(line)
                if self._main_depth:
                    self.out.main_paragraphs.append(line)
        self._prefix = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        self._markers(tag, values)
        if self._skip is not None:
            name, depth = self._skip
            if tag == name and tag not in _VOID_TAGS:
                self._skip = (name, depth + 1)
            return
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._skip = (tag, 1)
            return
        if tag in _VOID_TAGS:
            if tag in _BLOCK_TAGS:
                self._flush()
            return
        if tag in _SKIP_TAGS or self._hidden(values):
            self._skip = (tag, 1)
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _MAIN_TAGS:
            self._main_depth += 1
        if tag in _HEADINGS:
            self._prefix = "# "
        elif tag == "li":
            self._prefix = "- "

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        self._markers(tag, values)
        if self._skip is None and tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self._skip is not None:
            name, depth = self._skip
            if tag == name:
                self._skip = None if depth <= 1 else (name, depth - 1)
                if self._skip is None:
                    self._in_jsonld = False
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            if _NOT_FREE.search(data):
                self.out.paywall_marker = True
            return
        if self._skip is not None:
            return
        if self._in_title:
            self._title.append(data)
            return
        self._line.append(data)

    def _markers(self, tag: str, values: dict[str, str]) -> None:
        """Login and paywall signs, and the page's own description, wherever they sit (also in dropped parts)."""
        if tag == "input" and values.get("type", "").strip().lower() == "password":
            self.out.has_password = True
        marker = f"{values.get('class', '')} {values.get('id', '')}".lower()
        if "paywall" in marker:
            self.out.paywall_marker = True
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").strip().lower()
            content = _SPACES.sub(" ", values.get("content", "")).strip()
            if name in {"description", "og:description"} and content and not self.out.description:
                self.out.description = content
            elif name == "og:title" and content and not self.out.og_title:
                self.out.og_title = content

    def finish(self) -> Extracted:
        self._flush()
        title = _SPACES.sub(" ", "".join(self._title)).strip() or self.out.og_title
        self.out.title = title[:TITLE_MAX_CHARS]
        return self.out


def extract(html: str) -> Extracted:
    """Readable parts of an HTML document. A malformed document yields what was read before the fault."""
    parser = _Readable()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):  # html.parser's rare hard failures on broken declarations
        pass
    return parser.finish()


def page_text(extracted: Extracted, *, max_chars: int) -> tuple[str, bool]:
    """The description (unless the body repeats it) and the body, cut at `max_chars`; and whether it was cut."""
    body = extracted.body()
    parts = [extracted.description] if extracted.description and extracted.description not in body else []
    text = "\n\n".join([*parts, *body]).strip()
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary = cut.rfind("\n")
    if boundary >= max_chars // 2:
        cut = cut[:boundary]
    return cut.rstrip(), True


def check_access(extracted: Extracted, text: str, *, site: str) -> None:
    """Raise when the page is a login wall, a paywall teaser or an empty shell (UC-31 F: say so, never guess)."""
    length = len(text)
    if extracted.has_password and length < LOGIN_MAX_TEXT_CHARS:
        raise WebLoginRequired(site=site)
    if extracted.paywall_marker and length < PAYWALL_MAX_TEXT_CHARS:
        raise WebPaywalled(site=site)
    if length < MIN_TEXT_CHARS:
        raise PageNoText(site=site)


def charset_of(content_type: str, head: bytes) -> str:
    """`charset=` of the Content-Type, else a `<meta charset>` in the first bytes, else UTF-8."""
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip("\"'")
    found = _META_CHARSET.search(head[:SNIFF_BYTES])
    return found.group(1).decode("ascii") if found else "utf-8"


def decode(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:  # an unknown charset name
        return raw.decode("utf-8", errors="replace")


def looks_like_html(head: bytes) -> bool:
    start = head[:SNIFF_BYTES].lstrip().lower()
    return start.startswith((b"<!doctype html", b"<html")) or b"<body" in start


# --- the reader ------------------------------------------------------------------------------------------------------
class PageReader:
    def __init__(self, *, opener: Opener = urllib_open, chunk_bytes: int = CHUNK_BYTES) -> None:
        self._opener = opener
        self._chunk_bytes = chunk_bytes

    async def read(self, url: str, *, site: str, max_chars: int, max_bytes: int, timeout: float) -> PageText:
        abort = threading.Event()
        socket_timeout = max(1.0, min(SOCKET_TIMEOUT_MAX_S, timeout / 2))

        async def attempt() -> PageText:
            return await asyncio.to_thread(self._read, url, site, max_chars, max_bytes, socket_timeout, abort)

        try:
            return await retrying(attempt, timeout=timeout, operation=OPERATION, attempts=ATTEMPTS)
        finally:
            abort.set()  # a worker left behind by a timeout stops at its next chunk

    def _read(
        self, url: str, site: str, max_chars: int, max_bytes: int, socket_timeout: float, abort: threading.Event
    ) -> PageText:
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
            raw, cut = self._body(response.body, max_bytes=max_bytes, site=site, abort=abort)
        finally:
            response.body.close()
        content_type = response.headers.get("content-type", "")
        declared = media_type(response.headers)
        if declared in TEXT_MIMES:
            text = _SPACES.sub(" ", decode(raw, charset_of(content_type, raw))).strip()
            extracted = Extracted(paragraphs=[text] if text else [])
        elif declared in HTML_MIMES or (not declared and looks_like_html(raw)):
            extracted = extract(decode(raw, charset_of(content_type, raw)))
        else:
            raise PageNotReadable(site=site)
        text, truncated = page_text(extracted, max_chars=max_chars)
        check_access(extracted, text, site=site)
        return PageText(
            url=response.url, title=extracted.title, text=text, truncated=truncated or cut, size_bytes=len(raw)
        )

    def _body(self, body: Body, *, max_bytes: int, site: str, abort: threading.Event) -> tuple[bytes, bool]:
        """At most `max_bytes` of the body, and whether there was more."""
        chunks: list[bytes] = []
        total = 0
        while True:
            if abort.is_set():
                raise CallTimedOut(operation=OPERATION)
            try:
                chunk = body.read(min(self._chunk_bytes, max_bytes - total + 1))
            except (OSError, HTTPException) as error:
                raise translate.from_network(error, site=site) from None
            if not chunk:
                return b"".join(chunks), False
            total += len(chunk)
            if total > max_bytes:
                chunks.append(chunk[: len(chunk) - (total - max_bytes)])
                return b"".join(chunks), True
            chunks.append(chunk)
