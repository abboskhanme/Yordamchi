"""Offline harness for the TTS adapters: a fake ffmpeg, a local OpenRouter that answers with audio bytes, a Google
transport that streams Interactions SSE events, and a scripted edge-tts `Communicate`.

Error payloads are the synthetic STT fixtures (`tests/fixtures/{openrouter,gemini}/`): both vendors share one error
shape across endpoints. Audio is synthetic too: silence as 24 kHz PCM, and `make_ogg_opus` for what ffmpeg prints.
Nothing opens an internet socket: OpenRouter is an aiohttp server on a unix socket, Google an `httpx.MockTransport`.
"""

import asyncio
import base64
import json
import os
import shutil
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import httpx
from aiohttp import web
from google import genai
from google.genai import types as genai_types

from tests.integration.stt_harness import Recording, make_ogg_opus
from yordamchi.core.paths import Paths

OPENROUTER_TTS_BASE = "http://openrouter.test/api/v1"
SPEECH = "/api/v1/audio/speech"
INTERACTIONS = "/v1beta/interactions"
PCM_RATE = 24_000


def pcm_silence(ms: int, *, rate: int = PCM_RATE) -> bytes:
    """16-bit little-endian mono silence."""
    return b"\x00\x00" * (rate * ms // 1000)


# --- ffmpeg --------------------------------------------------------------------------------------------------------
def fake_ffmpeg(directory: Path, *, output: bytes | None = None, fail: bool = False, delay_s: float = 0.0) -> Path:
    """An executable standing in for `DATA/bin/ffmpeg`: records its argv and everything it read on stdin, then
    prints `output` (a 1.5 s OGG/Opus note by default), or fails like ffmpeg on bad input."""
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / "ffmpeg.out"
    out.write_bytes(output if output is not None else make_ogg_opus(1500))
    argv_file = directory / "ffmpeg.argv"
    stdin_file = directory / "ffmpeg.stdin"
    pause = f"sleep {delay_s}\n" if delay_s else ""
    tail = 'echo "Invalid data found when processing input" >&2\nexit 1\n' if fail else f'cat "{out}"\n'
    script = directory / "ffmpeg"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\ncat > "{stdin_file}"\n{pause}{tail}', encoding="utf-8"
    )
    script.chmod(0o755)
    return script


def ffmpeg_argv(script: Path) -> list[str]:
    return (script.parent / "ffmpeg.argv").read_text(encoding="utf-8").splitlines()


def ffmpeg_stdin(script: Path) -> bytes:
    return (script.parent / "ffmpeg.stdin").read_bytes()


def real_ffmpeg() -> Path | None:
    """`DATA/bin/ffmpeg` of this Mac (the installer's static build), or `YORDAMCHI_TEST_FFMPEG`; None if absent."""
    override = os.environ.get("YORDAMCHI_TEST_FFMPEG", "").strip()  # noqa: TID251  # opt-in path from the shell
    candidate = Path(override) if override else Paths.defaults(Path.home()).bin / "ffmpeg"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


# --- OpenRouter (aiohttp server on a unix socket, answering with audio) ------------------------------------------
@dataclass(frozen=True)
class AudioReply:
    """A 200 with audio: `chunks` are written one by one, `gap_s` apart (a streamed body)."""

    chunks: Sequence[bytes]
    content_type: str = "audio/pcm"
    headers: Mapping[str, str] = field(default_factory=lambda: {"x-generation-id": "gen-synthetic-tts-0001"})
    delay_s: float = 0.0
    gap_s: float = 0.0


type SpeechReply = AudioReply | Recording


@dataclass(frozen=True)
class SeenSpeech:
    headers: dict[str, str]
    json: Any


@dataclass
class OpenRouterSpeechServer:
    """Scripted replies in order (the last one repeats); `seen` keeps every request."""

    session: aiohttp.ClientSession | None = None
    replies: deque[SpeechReply] = field(default_factory=deque)
    seen: list[SeenSpeech] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0  # how many requests were open at the same time, at most

    def script(self, *replies: SpeechReply) -> None:
        self.replies.extend(replies)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return await self._answer(request)
        finally:
            self.in_flight -= 1

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        if request.path != SPEECH:
            return web.json_response({"error": {"code": 404, "message": "Resource not found"}}, status=404)
        self.seen.append(SeenSpeech(dict(request.headers), json.loads(body) if body else None))
        reply = self.replies.popleft() if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Recording):
            headers = {k: v for k, v in reply.headers.items() if k.lower() != "content-type"}
            return web.json_response(reply.body, status=reply.status, headers=headers)
        if reply.delay_s:
            await asyncio.sleep(reply.delay_s)
        response = web.StreamResponse(status=200, headers={"Content-Type": reply.content_type, **reply.headers})
        await response.prepare(request)
        for chunk in reply.chunks:
            if reply.gap_s:
                await asyncio.sleep(reply.gap_s)
            await response.write(chunk)
        await response.write_eof()
        return response


@asynccontextmanager
async def openrouter_speech_server() -> AsyncIterator[OpenRouterSpeechServer]:
    directory = Path(tempfile.mkdtemp(prefix="yd-tts-"))  # AF_UNIX paths are capped at 104 bytes on macOS
    socket_path = directory / "or.sock"
    server = OpenRouterSpeechServer()
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


# --- Google (Interactions SSE through an httpx transport) ---------------------------------------------------------
def audio_event(data: bytes, *, mime: str = "audio/l16", sample_rate: int | None = PCM_RATE) -> dict[str, Any]:
    delta: dict[str, Any] = {"type": "audio", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime}
    if sample_rate is not None:
        delta["sample_rate"] = sample_rate
    return {"event_type": "step.delta", "index": 0, "delta": delta}


CREATED = {"event_type": "interaction.created", "interaction": {"id": "int-synthetic-tts-1", "status": "in_progress"}}
COMPLETED = {"event_type": "interaction.completed", "interaction": {"id": "int-synthetic-tts-1", "status": "completed"}}


def sse(events: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events) + b"data: [DONE]\n\n"


@dataclass(frozen=True)
class SseReply:
    events: Sequence[Mapping[str, Any]]


type GoogleTtsReply = SseReply | Recording | Exception | Callable[[httpx.Request], Awaitable[httpx.Response]]


@dataclass
class GoogleTtsServer:
    replies: deque[GoogleTtsReply] = field(default_factory=deque)
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    def script(self, *replies: GoogleTtsReply) -> None:
        self.replies.extend(replies)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests if request.content]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.popleft() if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return await reply(request)
        return await self.handle_reply(request, reply)

    async def handle_reply(self, request: httpx.Request, reply: SseReply | Recording) -> httpx.Response:
        if isinstance(reply, Recording):
            return httpx.Response(reply.status, headers=reply.headers, json=reply.body, request=request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(reply.events), request=request
        )

    def client_factory(self, key: str) -> genai.Client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.clients.append(http)
        return genai.Client(api_key=key, vertexai=False, http_options=genai_types.HttpOptions(httpx_async_client=http))

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


@asynccontextmanager
async def google_tts_server() -> AsyncIterator[GoogleTtsServer]:
    server = GoogleTtsServer()
    try:
        yield server
    finally:
        await server.aclose()


# --- Edge (a scripted edge_tts.Communicate) -----------------------------------------------------------------------
@dataclass
class EdgeScript:
    """What one `Communicate` does: yields `chunks` (audio bytes, or a boundary dict), then raises `error`."""

    chunks: Sequence[bytes | Mapping[str, object]] = (b"\xff\xfb\x90\x64" + bytes(412),)
    error: Exception | None = None
    gap_s: float = 0.0


@dataclass(frozen=True)
class EdgeCall:
    text: str
    voice: str
    connect_timeout_s: int
    receive_timeout_s: int


class _FakeCommunicate:
    def __init__(self, script: EdgeScript) -> None:
        self._script = script

    async def stream(self) -> AsyncIterator[Mapping[str, object]]:
        for chunk in self._script.chunks:
            if self._script.gap_s:
                await asyncio.sleep(self._script.gap_s)
            yield {"type": "audio", "data": chunk} if isinstance(chunk, bytes) else chunk
        if self._script.error is not None:
            raise self._script.error


@dataclass
class FakeEdge:
    """A `CommunicateFactory`: scripted streams in order (the last one repeats); construction errors too."""

    scripts: deque[EdgeScript | Exception] = field(default_factory=lambda: deque([EdgeScript()]))
    calls: list[EdgeCall] = field(default_factory=list)

    def script(self, *scripts: EdgeScript | Exception) -> None:
        self.scripts = deque(scripts)

    def __call__(self, text: str, voice: str, connect_timeout_s: int, receive_timeout_s: int) -> _FakeCommunicate:
        self.calls.append(EdgeCall(text, voice, connect_timeout_s, receive_timeout_s))
        step = self.scripts.popleft() if len(self.scripts) > 1 else self.scripts[0]
        if isinstance(step, Exception):
            raise step
        return _FakeCommunicate(step)
