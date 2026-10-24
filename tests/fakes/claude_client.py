"""A stand-in for `ClaudeSDKClient` that replays a recorded session (`tests/fixtures/claude/streams/*.jsonl`).

Each fixture line is `{"t", "kind", "data"}`, recorded from the real CLI (see the fixtures README):
- `raw`: a stream-json message, put on the raw stream (`_query.receive_messages()`);
- `hook`: the CLI calling a hook; the replay calls the session's own hook with the recorded input;
- `perm`: the CLI calling `can_use_tool`; the replay calls the session's callback (and waits for it, as the CLI does);
- `sent`: a user message the recorder wrote; the replay pauses here until the code under test calls `query()`;
- `receipt`: the interrupt receipt; the replay pauses until the interrupt control request arrives, then returns it.

Nothing spawns: the network and the process stay closed, and tests read `sent`, `control_requests`, `hook_outputs`.
"""

import asyncio
import json
import os
import warnings
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import CanUseToolShadowedWarning, ToolPermissionContext

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "claude"
FAKE_CLI_PID: Final = 2_000_000_001  # above any real pid: a stray signal can never hit a process
_END: Final = object()


def records(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / "streams" / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def raw_stream(name: str) -> list[dict[str, Any]]:
    return [record["data"] for record in records(name) if record["kind"] == "raw"]


class FakeQuery:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client
        self._initialization_result: dict[str, Any] = {"pid": client.pid, "claude_code_version": "2.1.310"}
        self.control_requests: list[dict[str, Any]] = []

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._client.outbox.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _send_control_request(self, request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        del timeout
        self.control_requests.append(request)
        if self._client.control_error is not None:
            raise self._client.control_error
        if request.get("subtype") == "interrupt":
            self._client.interrupted.set()
            return dict(self._client.receipt)
        return {}


class FakeQueryWithoutControl(FakeQuery):
    """An SDK whose private `_send_control_request` is gone (the interrupt fallback path)."""

    def __getattribute__(self, name: str) -> object:
        if name == "_send_control_request":
            raise AttributeError(name)
        return super().__getattribute__(name)


class FakeClient:
    def __init__(
        self,
        options: ClaudeAgentOptions,
        recording: Sequence[dict[str, Any]] = (),
        *,
        connect_error: BaseException | None = None,
        warn: bool = True,
        private_api: bool = True,
        control_api: bool = True,
        connect_hangs: bool = False,
        pid: int = FAKE_CLI_PID,
    ) -> None:
        self.options = options
        self.recording = list(recording)
        self.connect_error = connect_error
        self.control_error: BaseException | None = None
        self.warn = warn
        self.private_api = private_api
        self.control_api = control_api
        self.connect_hangs = connect_hangs
        self.pid = pid
        self.outbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.hook_outputs: list[Any] = []
        self.permission_results: list[Any] = []
        self.public_interrupts = 0
        self.models: list[str | None] = []
        self.stopped_tasks: list[str] = []
        self.interrupted = asyncio.Event()
        self.receipt: dict[str, Any] = {"still_queued": []}
        self.disconnected = False
        self.replay_done = asyncio.Event()
        self._sends = asyncio.Semaphore(0)
        self._replayer: asyncio.Task[None] | None = None
        self._query: Any = None

    # --- the ClaudeSDKClient surface the adapter uses -------------------------------------------------------
    async def connect(self) -> None:
        if self.warn:  # the SDK warns on every connect with can_use_tool + bypass (S3)
            warnings.warn("can_use_tool will not be invoked", CanUseToolShadowedWarning, stacklevel=1)
        if self.connect_error is not None:
            raise self.connect_error
        if self.connect_hangs:
            await asyncio.Event().wait()
        if not self.private_api:
            self._query = None
        elif self.control_api:
            self._query = FakeQuery(self)
        else:
            self._query = FakeQueryWithoutControl(self)
        self._replayer = asyncio.get_running_loop().create_task(self._replay())

    async def query(self, prompt: AsyncIterable[dict[str, Any]]) -> None:
        async for message in prompt:
            self.sent.append(message)
            self._sends.release()

    async def interrupt(self) -> None:
        self.public_interrupts += 1
        self.interrupted.set()

    async def set_model(self, model: str | None = None) -> None:
        if self.control_error is not None:
            raise self.control_error
        self.models.append(model)

    async def stop_task(self, task_id: str) -> None:
        self.stopped_tasks.append(task_id)

    async def disconnect(self) -> None:
        self.disconnected = True
        if self._replayer is not None:
            self._replayer.cancel()
        await self.outbox.put(_END)

    # --- test controls --------------------------------------------------------------------------------------
    def push(self, *messages: dict[str, Any]) -> None:
        for message in messages:
            self.outbox.put_nowait(message)

    def die(self, error: BaseException) -> None:
        self.outbox.put_nowait(error)

    async def _replay(self) -> None:
        for record in self.recording:
            kind, data = record["kind"], record["data"]
            if kind == "sent":
                await self._sends.acquire()
            elif kind == "raw":
                await self.outbox.put(data)
            elif kind == "hook":
                await self._run_hook(data)
            elif kind == "perm":
                await self._run_permission(data)
            elif kind == "receipt":
                self.receipt = data
                await self.interrupted.wait()
            await asyncio.sleep(0)
        self.replay_done.set()

    async def _run_hook(self, data: dict[str, Any]) -> None:
        event = data["input"].get("hook_event_name")
        for matcher in (self.options.hooks or {}).get(event, []):
            for hook in matcher.hooks:
                self.hook_outputs.append(await hook(data["input"], data.get("tool_use_id"), {"signal": None}))

    async def _run_permission(self, data: dict[str, Any]) -> None:
        callback = self.options.can_use_tool
        assert callback is not None, "the session has no can_use_tool"
        context = ToolPermissionContext(tool_use_id=data.get("tool_use_id"), agent_id=data.get("agent_id"))
        self.permission_results.append(await callback(data["tool_name"], data["input"], context))


# --- live tests (`pytest -m live`): the real CLI, a clean environment -------------------------------------------
LIVE_CLI: Final = Path("~/.local/bin/claude").expanduser()
LIVE_SEARCH_PATH: Final = f"{LIVE_CLI.parent}:/usr/bin:/bin:/usr/sbin:/sbin"


def scrub_live_env(monkeypatch: "pytest.MonkeyPatch") -> None:
    """The SDK merges `os.environ` into the child and strips only CLAUDECODE (STACK §4.4): do what app.py does."""
    from yordamchi.core import proc

    inherited = dict(os.environ)  # noqa: TID251 - read-only snapshot; the SDK child inherits this process env
    if proc.refused_credentials(inherited):
        pytest.skip("a refused credential source is set in this environment")
    for name in proc.scrub_environ(dict(inherited)):
        monkeypatch.delenv(name)
