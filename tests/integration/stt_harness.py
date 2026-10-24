"""Offline harness for the STT adapters: replayed payloads, a Google transport, a local OpenRouter, fake ffmpeg.

The payloads in `tests/fixtures/{gemini,openrouter}/` are synthetic (each file says so in `_synthetic`), shaped after
the vendors' docs. Nothing here opens an internet socket: Google traffic goes through an `httpx.MockTransport`, and
OpenRouter traffic through a real aiohttp server on a unix socket, which pytest-socket allows.
"""

import asyncio
import copy
import io
import json
import os
import shutil
import struct
import tempfile
import wave
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import aiohttp
import httpx
import pytest
from aiohttp import web
from google import genai
from google.genai import types as genai_types

from yordamchi.core import keychain, redact
from yordamchi.core.errors import KeychainItemMissing, KeychainUnavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOOGLE_KEY = "AIza" + "SyntheticKeyForOfflineTests_0123456"
GOOGLE_KEY_2 = "AIza" + "SyntheticKeyForOfflineTests_6543210"
OPENROUTER_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
LANGUAGES = ("uz-UZ", "ru-RU", "en-US")


# --- recordings ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Recording:
    status: int
    headers: dict[str, str]
    body: Any

    def with_body(self, edit: Callable[[Any], None]) -> "Recording":
        body = copy.deepcopy(self.body)
        edit(body)
        return Recording(self.status, dict(self.headers), body)

    def with_headers(self, **headers: str) -> "Recording":
        return Recording(
            self.status, {**self.headers, **{k.replace("_", "-"): v for k, v in headers.items()}}, self.body
        )


def load(vendor: str, name: str) -> Recording:
    raw = json.loads((FIXTURES / vendor / f"{name}.json").read_text(encoding="utf-8"))
    assert raw["_synthetic"].startswith("Synthetic"), "every STT fixture says that it is synthetic"
    return Recording(int(raw["status"]), dict(raw["headers"]), raw["body"])


# --- audio ---------------------------------------------------------------------------------------------------------
def make_wav(ms: int, *, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * (rate * ms // 1000))
    return buffer.getvalue()


def streamed_wav(ms: int) -> bytes:
    """A WAV as ffmpeg writes it to a pipe: RIFF and data sizes left at the 0xFFFFFFFF placeholder."""
    data = bytearray(make_wav(ms))
    struct.pack_into("<I", data, 4, 0xFFFFFFFF)
    struct.pack_into("<I", data, data.find(b"data") + 4, 0xFFFFFFFF)
    return bytes(data)


def _ogg_page(granule: int, sequence: int, payload: bytes) -> bytes:
    header = b"OggS" + bytes([0, 0]) + struct.pack("<qII", granule, 0x59440001, sequence) + b"\x00\x00\x00\x00"
    return header + bytes([1, len(payload)]) + payload


def make_ogg_opus(ms: int, *, pre_skip: int = 312) -> bytes:
    """A minimal Ogg Opus stream: OpusHead, OpusTags, one audio page whose granule gives `ms` (RFC 7845)."""
    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<HIhB", pre_skip, 48_000, 0, 0)
    tags = b"OpusTags" + struct.pack("<I", 0) + struct.pack("<I", 0)
    audio = b"\xfc\xff\xfe"  # one silent Opus frame
    return _ogg_page(0, 0, head) + _ogg_page(0, 1, tags) + _ogg_page(pre_skip + 48_000 * ms // 1000, 2, audio)


def fake_ffmpeg(directory: Path, *, output: bytes | None = None, fail: bool = False) -> Path:
    """An executable standing in for `DATA/bin/ffmpeg`: records its argv, drains stdin, prints `output`."""
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / "ffmpeg.out"
    out.write_bytes(output if output is not None else streamed_wav(1500))
    argv_file = directory / "ffmpeg.argv"
    script = directory / "ffmpeg"
    tail = 'echo "Invalid data found when processing input" >&2\nexit 1\n' if fail else f'cat "{out}"\n'
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\ncat > /dev/null\n{tail}', encoding="utf-8")
    script.chmod(0o755)
    return script


def ffmpeg_argv(script: Path) -> list[str]:
    return (script.parent / "ffmpeg.argv").read_text(encoding="utf-8").splitlines()


# --- Google (httpx transport under google-genai) ------------------------------------------------------------------
type GoogleReply = Recording | Exception | Callable[[httpx.Request], Awaitable[Recording]]


@dataclass
class GoogleServer:
    """Scripted replies in order; the last one repeats. `requests` keeps every request the SDK sent."""

    replies: deque[GoogleReply] = field(default_factory=deque)
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    def script(self, *replies: GoogleReply) -> None:
        self.replies.extend(replies)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests if request.content]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.popleft() if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if not isinstance(reply, Recording):
            reply = await reply(request)
        return httpx.Response(reply.status, headers=reply.headers, json=reply.body, request=request)

    def client_factory(self, key: str) -> genai.Client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.clients.append(http)
        return genai.Client(api_key=key, vertexai=False, http_options=genai_types.HttpOptions(httpx_async_client=http))

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


@asynccontextmanager
async def google_server() -> AsyncIterator[GoogleServer]:
    server = GoogleServer()
    try:
        yield server
    finally:
        await server.aclose()


# --- OpenRouter (aiohttp server on a unix socket) ------------------------------------------------------------------
@dataclass(frozen=True)
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]
    json: Any


type OpenRouterReply = Recording | Callable[[], Awaitable[Recording]]


@dataclass
class OpenRouterServer:
    """Per path, scripted replies in order; the last one repeats."""

    session: aiohttp.ClientSession | None = None
    routes: dict[str, deque[OpenRouterReply]] = field(default_factory=dict)
    seen: list[SeenRequest] = field(default_factory=list)

    def script(self, path: str, *replies: OpenRouterReply) -> None:
        self.routes.setdefault(path, deque()).extend(replies)

    def requests_to(self, path: str) -> list[SeenRequest]:
        return [request for request in self.seen if request.path == path]

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.seen.append(
            SeenRequest(request.method, request.path, dict(request.headers), json.loads(body) if body else None)
        )
        queue = self.routes.get(request.path)
        if not queue:
            return web.json_response({"error": {"code": 404, "message": "Resource not found"}}, status=404)
        reply = queue.popleft() if len(queue) > 1 else queue[0]
        if not isinstance(reply, Recording):
            reply = await reply()
        headers = {k: v for k, v in reply.headers.items() if k.lower() != "content-type"}
        return web.json_response(reply.body, status=reply.status, headers=headers)


@asynccontextmanager
async def openrouter_server() -> AsyncIterator[OpenRouterServer]:
    # AF_UNIX paths are capped at 104 bytes on macOS; pytest's tmp_path is too long for that.
    directory = Path(tempfile.mkdtemp(prefix="yd-or-"))
    socket_path = directory / "or.sock"
    server = OpenRouterServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.2)
    await runner.setup()
    site = web.UnixSite(runner, str(socket_path))
    await site.start()
    server.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
    try:
        yield server
    finally:
        await server.session.close()
        await runner.cleanup()
        shutil.rmtree(directory, ignore_errors=True)


OPENROUTER_BASE = "http://openrouter.test/api/v1"


async def slow(seconds: float, reply: Recording) -> Recording:
    await asyncio.sleep(seconds)
    return reply


# --- live ----------------------------------------------------------------------------------------------------------
# Where a live test finds its key when the shell does not export it: the owner's login Keychain, so no plaintext key
# file is ever needed (owner requirement 2026-10-24).
LIVE_KEYCHAIN_ITEMS: Final = {
    "YORDAMCHI_LIVE_GOOGLE_API_KEY": keychain.GOOGLE_API_KEY,
    "YORDAMCHI_LIVE_OPENROUTER_API_KEY": keychain.OPENROUTER_API_KEY,
}


def _keychain_key(item: keychain.KeychainItem, runner: keychain.Runner) -> str:
    try:
        result = runner(keychain.find_argv(item, reveal=True), stdin=None, timeout=keychain.TIMEOUT_S)
        return keychain.password_from(result, item)
    except (KeychainItemMissing, KeychainUnavailable, TimeoutError, OSError):
        return ""


def live_key(name: str, *, runner: keychain.Runner = keychain.security_runner) -> str:
    """The opt-in key for a `live` test: the environment variable `name`, else its Keychain item
    (`uz.yordamchi/google-api-key`, `uz.yordamchi/openrouter-api-key`) through `/usr/bin/security`. The test is skipped
    without either. Called only inside a `live` test body, so the default suite never touches the Keychain; `runner`
    is the offline test seam."""
    value = os.environ.get(name, "").strip()  # noqa: TID251  # live tests take their opt-in key from the shell
    item = LIVE_KEYCHAIN_ITEMS.get(name)
    if not value and item is not None:
        value = _keychain_key(item, runner)
    if not value:
        where = f" or add the Keychain item {item}" if item is not None else ""
        pytest.skip(f"set {name}{where} to run this live test")
    redact.add_known_secrets([value])
    return value
