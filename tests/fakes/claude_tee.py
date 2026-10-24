"""A recording wrapper around the real `ClaudeSDKClient`: it writes what the session does in the replay format of
`tests/fakes/claude_client.py` (`raw`, `hook`, `sent`, `init_response`), so a live run through `ClaudeAssistant`
re-records a fixture in `tests/fixtures/claude/streams/` (see that README). Used only by `-m live --record-llm` tests.

Sanitising: the home dir becomes `/Users/owner`, the session cwd `/tmp/yordamchi-fixture/sessions/<name>` (also inside
the transcript path slug), the `init` lists of skills, agents, plugins and slash commands are cut to three entries,
and the initialize response keeps only the fields the README names.
"""

import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput

INIT_RESPONSE_KEYS: Final = ("pid", "claude_code_version", "current_permission_mode", "session_state", "capabilities")
INIT_LISTS: Final = ("skills", "agents", "plugins", "slash_commands")

type Hook = Callable[[HookInput, str | None, HookContext], Awaitable[HookJSONOutput]]


class _QueryProxy:
    def __init__(self, query: object, tee: "TeeClient") -> None:
        self._real = query
        self._tee = tee

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        receive: Callable[[], AsyncIterator[dict[str, Any]]] = cast("Any", self._real).receive_messages
        async for message in receive():
            self._tee.record("raw", message)
            yield message


class TeeClient:
    """Stands in for `ClaudeSDKClient` in `ClaudeAssistant(client_factory=…)`; `records` grows as the session runs."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.records: list[dict[str, Any]] = []
        self._started = time.monotonic()
        for matchers in (options.hooks or {}).values():
            for matcher in matchers:
                matcher.hooks = [self._wrap(hook) for hook in matcher.hooks]
        self._client = ClaudeSDKClient(options=options)
        self._query: Any = None
        self._transport: Any = None

    def record(self, kind: str, data: object) -> None:
        self.records.append({"t": round(time.monotonic() - self._started, 3), "kind": kind, "data": data})

    def _wrap(self, hook: Hook) -> Hook:
        async def recorded(data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
            output = await hook(data, tool_use_id, context)
            self.record("hook", {"input": dict(data), "tool_use_id": tool_use_id, "output": output})
            return output

        return recorded

    async def connect(self) -> None:
        await self._client.connect()
        self._transport = self._client._transport
        query = self._client._query
        info = getattr(query, "_initialization_result", None) or {}
        self.record("init_response", {key: info[key] for key in INIT_RESPONSE_KEYS if key in info})
        self._query = _QueryProxy(query, self)

    async def query(self, prompt: AsyncIterable[dict[str, Any]]) -> None:
        async def tee() -> AsyncIterator[dict[str, Any]]:
            async for message in prompt:
                self.record("sent", message)
                yield message

        await self._client.query(tee())

    async def set_model(self, model: str | None = None) -> None:
        await self._client.set_model(model)

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def disconnect(self) -> None:
        await self._client.disconnect()


def _sanitise(value: object, replacements: list[tuple[str, str]]) -> object:
    text = json.dumps(value, ensure_ascii=False)
    for old, new in replacements:
        text = text.replace(old, new)
    loaded: object = json.loads(text)
    return loaded


def write_fixture(records: list[dict[str, Any]], path: Path, *, home: Path, cwd: Path, name: str) -> None:
    """The records as one JSONL fixture, sanitised (module docstring)."""
    fixture_cwd = f"/tmp/yordamchi-fixture/sessions/{name}"
    replacements: list[tuple[str, str]] = []
    for form in dict.fromkeys((str(cwd.resolve()), str(cwd))):  # macOS: /var is /private/var
        replacements.append((form, fixture_cwd))
        replacements.append((form.replace("/", "-").replace(".", "-").replace("_", "-"), fixture_cwd.replace("/", "-")))
    replacements.append((str(home), "/Users/owner"))
    lines: list[str] = []
    for record in records:
        data = _sanitise(record["data"], replacements)
        if record["kind"] == "raw" and isinstance(data, dict) and data.get("subtype") == "init":
            for key in INIT_LISTS:
                if isinstance(data.get(key), list):
                    data[key] = data[key][:3]
        lines.append(json.dumps({**record, "data": data}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
