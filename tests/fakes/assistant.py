"""`FakeAssistant`: replies queued per kind, a responder function, or recorded LLM fixtures (SPEC §8).

Recorded fixtures live in `tests/fixtures/llm/<suite>/<key>.json` with `key = fixture_key(request)`; a request with
no recording fails loudly (a stale key means the prompt changed: re-record with `pytest -m live --record-llm`).
"""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import Assistant, AssistantDelta, AssistantKind, AssistantReply, AssistantRequest

READY = RuntimeStatus(installed=True, version="2.1.309", logged_in=True, auth_method="claude.ai")

type Scripted = AssistantReply | Exception | str
type Responder = Callable[[AssistantRequest], AssistantReply]


def fixture_key(request: AssistantRequest) -> str:
    """sha256 over the prompt version (the system prompt) and the input."""
    material = json.dumps(
        {
            "kind": request.kind,
            "system": request.system_prompt,
            "prompt": request.prompt,
            "model": request.model,
            "schema": request.output_schema,
            "images": [path.name for path in request.images],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_fixtures(directory: Path) -> dict[str, AssistantReply]:
    replies: dict[str, AssistantReply] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        structured: JsonValue = data.get("structured")
        replies[path.stem] = AssistantReply(
            text=str(data["text"]),
            provider=str(data.get("provider", "claude")),
            model=str(data.get("model", "")),
            duration_ms=int(data.get("duration_ms", 0)),
            structured=structured,
        )
    return replies


class FakeAssistant:
    def __init__(
        self,
        *,
        provider: str = "claude",
        status: RuntimeStatus = READY,
        responder: Responder | None = None,
        fixtures: dict[str, AssistantReply] | None = None,
    ) -> None:
        self.provider = provider
        self.status = status
        self.responder = responder
        self.fixtures = fixtures
        self._queued: defaultdict[str, deque[Scripted]] = defaultdict(deque)
        self.requests: list[AssistantRequest] = []
        self.resets: list[tuple[AssistantKind, str | None]] = []

    def reply_with(self, kind: AssistantKind, *replies: Scripted) -> None:
        self._queued[kind].extend(replies)

    def _reply(self, request: AssistantRequest, text: str) -> AssistantReply:
        return AssistantReply(text=text, provider=self.provider, model=request.model, duration_ms=1)

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return self.status

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        self.requests.append(request)
        queued = self._queued[request.kind]
        if queued:
            item = queued.popleft()
            if isinstance(item, Exception):
                raise item
            return self._reply(request, item) if isinstance(item, str) else item
        if self.fixtures is not None:
            key = fixture_key(request)
            if key not in self.fixtures:
                msg = f"no recorded reply {key}; re-record with `pytest -m live --record-llm`"
                raise LookupError(msg)
            return self.fixtures[key]
        if self.responder is not None:
            return self.responder(request)
        return self._reply(request, "ok")

    async def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        reply = await self.ask(request, timeout=idle_timeout)
        for word in reply.text.split(" "):
            yield AssistantDelta(text=word + " ")
        yield reply

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        self.resets.append((kind, conversation))


_: Assistant = FakeAssistant()
