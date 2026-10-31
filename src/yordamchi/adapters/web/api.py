"""`HttpApiCaller`: one GET of an API-key integration (UC-35, SPEC §3.2.10 step 7; T76).

stdlib `urllib` in a worker thread, as `download.py`:
- **Only http and https**, and **no redirect is followed**: a 3xx comes back as it is, so the auth header the caller
  added never reaches another host.
- **Capped**: the body is read up to `max_bytes`; what is left is not read (`truncated`).
- **Bounded**: the socket times out, and the whole call runs under the caller's `timeout`. No retry: the assistant
  sees the status and decides.
- An HTTP status never raises (a 401 or 404 body tells the assistant what went wrong); a network failure does.
"""

import asyncio
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Final
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPSHandler,
    OpenerDirector,
    Request,
    UnknownHandler,
)

import certifi

from yordamchi.adapters.web import translate
from yordamchi.adapters.web.download import ALLOWED_SCHEMES, SOCKET_TIMEOUT_MAX_S, Body, media_type
from yordamchi.core.errors import CallTimedOut
from yordamchi.ports.web import ApiAnswer

USER_AGENT: Final = "Yordamchi/1 (+owner's personal assistant)"
_TEXT_FALLBACK: Final = "cp1251"


@dataclass(frozen=True, slots=True)
class RawAnswer:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: Body


type ApiOpener = Callable[[str, Mapping[str, str], float], RawAnswer]


def urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> RawAnswer:
    """GET with an opener that has no redirect handler: a 3xx is returned like any other status."""
    if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
        msg = "only http(s) URLs are fetched"
        raise ValueError(msg)
    opener = OpenerDirector()
    context = ssl.create_default_context(cafile=certifi.where())
    for handler in (UnknownHandler(), HTTPHandler(), HTTPSHandler(context=context), HTTPDefaultErrorHandler()):
        opener.add_handler(handler)
    opener.add_handler(HTTPErrorProcessor())
    sent = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.9, */*;q=0.5", **headers}
    request = Request(url, headers=sent)  # noqa: S310  # scheme checked above
    try:
        raw = opener.open(request, timeout=timeout)
    except HTTPError as error:
        raw = error
    found = {name.lower(): value for name, value in raw.headers.items()}
    return RawAnswer(status=int(raw.getcode() or 0), headers=found, body=raw)


def decode(data: bytes, headers: Mapping[str, str]) -> str:
    """The body as text: the declared charset, else UTF-8, else cp1251 (never raises)."""
    declared = headers.get("content-type", "")
    charset = next(
        (part.split("=", 1)[1].strip().strip('"') for part in declared.split(";") if "charset=" in part.lower()),
        "utf-8",
    )
    for encoding in (charset, "utf-8"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode(_TEXT_FALLBACK, errors="replace")


class HttpApiCaller:
    def __init__(self, *, opener: ApiOpener = urllib_get) -> None:
        self._opener = opener

    async def get(
        self, url: str, *, site: str, headers: Mapping[str, str], max_bytes: int, timeout: float
    ) -> ApiAnswer:
        socket_timeout = max(1.0, min(SOCKET_TIMEOUT_MAX_S, timeout))
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._get, url, site, headers, max_bytes, socket_timeout), timeout=timeout
            )
        except TimeoutError:
            raise CallTimedOut(operation="api_get") from None

    def _get(self, url: str, site: str, headers: Mapping[str, str], max_bytes: int, socket_timeout: float) -> ApiAnswer:
        try:
            raw = self._opener(url, headers, socket_timeout)
        except (OSError, HTTPException) as error:  # URLError is an OSError
            raise translate.from_network(error, site=site) from None
        try:
            data = raw.body.read(max_bytes + 1)
        except (OSError, HTTPException) as error:
            raise translate.from_network(error, site=site) from None
        finally:
            raw.body.close()
        truncated = len(data) > max_bytes
        return ApiAnswer(
            status=raw.status,
            mime=media_type(raw.headers),
            text=decode(data[:max_bytes], raw.headers),
            truncated=truncated,
        )
