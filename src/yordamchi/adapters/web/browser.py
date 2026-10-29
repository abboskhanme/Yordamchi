"""`PlaywrightBrowser`: page screenshots and a page's main image, in headless Chromium (Playwright 1.64.1).

**Clean profile, every time** (ASSUMPTIONS 2026-10-23): each call starts its own browser and a new context with no
storage state, so no cookie, login or local storage survives from one request to the next, and the owner's own
browser profile is never touched. Downloads are refused and service workers blocked.

Bounded twice: the whole call runs under the caller's `timeout` (`core.retry.retrying`, one retry for a crashed
browser or a reset connection), and every Playwright wait gets the time that is left, in milliseconds.

A page that never goes quiet still gets its picture: navigation waits for `domcontentloaded`, then up to
`LOAD_WAIT_S` for `load` and `IDLE_WAIT_S` for network idle, each best-effort. The full-page PNG is clipped at
`FULL_PAGE_MAX_PX` (an infinite feed would otherwise grow without end; Chromium's texture limit is 16,384 px).

`print_pdf` (UC-37) prints the documents the personal assistant makes: no URL is opened, the page is set from HTML.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import FloatRect, PdfMargins, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from yordamchi.adapters.web import translate
from yordamchi.core.errors import CallTimedOut, DocumentFailed
from yordamchi.core.retry import retrying
from yordamchi.ports.web import Screenshot

FULL_PAGE_MAX_PX: Final = 16_000
LOAD_WAIT_S: Final = 5.0
IDLE_WAIT_S: Final = 2.0
CLOSE_TIMEOUT_S: Final = 5.0
ATTEMPTS: Final = 2
TITLE_MAX_CHARS: Final = 200

# The main image of a page: og:image (and its Twitter twin), else the largest decoded <img>. Absolute URLs only;
# data: URIs are skipped (nothing to fetch).
MAIN_IMAGE_JS: Final = """() => {
  const abs = (value) => { try { return new URL(value, document.baseURI).href; } catch (e) { return null; } };
  for (const selector of ['meta[property="og:image:secure_url"]', 'meta[property="og:image"]',
                          'meta[name="twitter:image"]', 'meta[name="twitter:image:src"]']) {
    const element = document.querySelector(selector);
    const found = element && element.content ? abs(element.content) : null;
    if (found && /^https?:/.test(found)) return found;
  }
  let best = null, area = 0;
  for (const image of Array.from(document.images)) {
    const source = abs(image.currentSrc || image.src);
    const size = (image.naturalWidth || 0) * (image.naturalHeight || 0);
    if (source && /^https?:/.test(source) && size > area) { best = source; area = size; }
  }
  return best;
}"""
PAGE_HEIGHT_JS: Final = (
    "() => Math.max(document.documentElement ? document.documentElement.scrollHeight : 0,"
    " document.body ? document.body.scrollHeight : 0)"
)

type LoadState = Literal["domcontentloaded", "load", "networkidle"]


class BrowserPage(Protocol):
    """The part of Playwright's `Page` this adapter uses (the seam the offline tests fake)."""

    async def goto(self, url: str, *, timeout: float, wait_until: LoadState) -> object: ...

    async def wait_for_load_state(self, state: LoadState, *, timeout: float) -> None: ...

    async def title(self) -> str: ...

    async def evaluate(self, expression: str) -> object: ...

    async def screenshot(
        self, *, path: Path, full_page: bool, clip: FloatRect | None, timeout: float, animations: Literal["disabled"]
    ) -> bytes: ...


type PageOpener = Callable[[int, int], AbstractAsyncContextManager[BrowserPage]]


@asynccontextmanager
async def chromium_page(width: int, height: int) -> AsyncIterator[BrowserPage]:
    """A fresh headless Chromium with one new, empty context and one page; everything is closed on the way out."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                accept_downloads=False,
                service_workers="block",
                storage_state=None,
            )
            yield await context.new_page()
        finally:
            with contextlib.suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await browser.close()


@dataclass(slots=True)
class _Deadline:
    at: float

    @classmethod
    def after(cls, seconds: float) -> "_Deadline":
        return cls(asyncio.get_running_loop().time() + seconds)

    def ms(self, cap_s: float | None = None) -> float:
        """What is left, in Playwright's milliseconds (at least 1 ms), optionally capped."""
        left = self.at - asyncio.get_running_loop().time()
        if cap_s is not None:
            left = min(left, cap_s)
        return max(1.0, left * 1000)


def _private(path: Path) -> Path:
    path.chmod(0o600)
    return path


async def _settle(page: BrowserPage, deadline: _Deadline) -> None:
    waits: tuple[tuple[LoadState, float], ...] = (("load", LOAD_WAIT_S), ("networkidle", IDLE_WAIT_S))
    for state, cap_s in waits:
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state(state, timeout=deadline.ms(cap_s))


async def _open(page: BrowserPage, url: str, deadline: _Deadline) -> None:
    await page.goto(url, timeout=deadline.ms(), wait_until="domcontentloaded")
    await _settle(page, deadline)


class PlaywrightBrowser:
    def __init__(self, *, opener: PageOpener = chromium_page) -> None:
        self._opener = opener

    async def screenshot(
        self, url: str, *, site: str, dest_dir: Path, width: int, height: int, timeout: float
    ) -> Screenshot:
        async def attempt() -> Screenshot:
            return await self._screenshot(
                url, site=site, dest_dir=dest_dir, width=width, height=height, deadline=deadline
            )

        deadline = _Deadline.after(timeout)
        return await retrying(attempt, timeout=timeout, operation="screenshot", attempts=ATTEMPTS)

    async def main_image_url(self, url: str, *, site: str, width: int, height: int, timeout: float) -> str | None:
        async def attempt() -> str | None:
            try:
                async with self._opener(width, height) as page:
                    await _open(page, url, deadline)
                    found = await page.evaluate(MAIN_IMAGE_JS)
            except PlaywrightError as error:
                raise translate.from_playwright(error, site=site, operation="fetch_file") from None
            return found if isinstance(found, str) and found else None

        deadline = _Deadline.after(timeout)
        return await retrying(attempt, timeout=timeout, operation="fetch_file", attempts=ATTEMPTS)

    async def _screenshot(
        self, url: str, *, site: str, dest_dir: Path, width: int, height: int, deadline: _Deadline
    ) -> Screenshot:
        viewport_png, full_png = dest_dir / "viewport.png", dest_dir / "full-page.png"
        try:
            async with self._opener(width, height) as page:
                await _open(page, url, deadline)
                title = (await page.title()).strip()[:TITLE_MAX_CHARS]
                await page.screenshot(
                    path=viewport_png, full_page=False, clip=None, timeout=deadline.ms(), animations="disabled"
                )
                page_height = await page.evaluate(PAGE_HEIGHT_JS)
                clip: FloatRect | None = None
                if isinstance(page_height, int | float) and page_height > FULL_PAGE_MAX_PX:
                    clip = {"x": 0, "y": 0, "width": width, "height": FULL_PAGE_MAX_PX}
                await page.screenshot(
                    path=full_png, full_page=True, clip=clip, timeout=deadline.ms(), animations="disabled"
                )
        except PlaywrightError as error:
            raise translate.from_playwright(error, site=site, operation="screenshot") from None
        return Screenshot(viewport_png=_private(viewport_png), full_page_png=_private(full_png), title=title)


# --- printing (UC-37) ------------------------------------------------------------------------------------------------
PDF_MARGIN: Final = PdfMargins(top="18mm", bottom="18mm", left="16mm", right="16mm")


async def _refuse_request(route: Route) -> None:
    await route.abort()


async def print_pdf(html: str, out: Path, *, timeout: float) -> None:
    """`html` (built by `adapters/office`, every text escaped) printed as an A4 PDF at `out` (0600) by a fresh headless
    Chromium with JavaScript off and every network request refused: the page can only show what it holds. A browser
    failure raises `DocumentFailed("pdf")`, the clock `CallTimedOut("document")`."""
    deadline = _Deadline.after(timeout)
    try:
        async with asyncio.timeout(timeout), async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    java_script_enabled=False, accept_downloads=False, service_workers="block", storage_state=None
                )
                await context.route("**/*", _refuse_request)
                page = await context.new_page()
                await page.set_content(html, wait_until="load", timeout=deadline.ms())
                await page.pdf(path=out, format="A4", margin=PDF_MARGIN, print_background=True)
            finally:
                with contextlib.suppress(PlaywrightError, TimeoutError):
                    async with asyncio.timeout(CLOSE_TIMEOUT_S):
                        await browser.close()
    except TimeoutError:
        raise CallTimedOut(operation="document") from None
    except PlaywrightError as error:
        raise DocumentFailed(format="pdf") from error
    _private(out)
