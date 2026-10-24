"""`ClaudeAssistant`: the `Assistant` port on warm `claude` clients (SPEC §5.3, §5.4, §3.2.4, §3.2.6; NFR-SPD-02).

Speed first (reports and routing ≤ 4 s): an ask never waits for a `claude` process to start if it can avoid it.

**Pool, per kind.** One live client and one hot spare, each a connected `ClaudeSDKClient` streaming session. The spare
is connected but has never been asked, so it carries no conversation and is as good as new whenever its launch
options match. Asks of one kind are serialised on the live client (one owner; an ask takes 1–3 s on haiku).

**Rotation.** A client is retired and the spare promoted (and a new spare started at once) when
- it has answered `max_asks` (20) asks, or its last context crossed `context_limit_tokens` (soft: a warm client
  keeps answering until the spare is ready, so a rotation never makes an ask cold);
- the request's `settings_version` is higher than the one it last answered under (hard), or its frozen launch
  options no longer match: the system prompt or the output schema (hard);
- the effort changed (soft: effort is a launch option, so the spare is rebuilt with the new effort and the live
  client answers until it is ready; SPEC §5.4 "Assistant pools rebuild their spare at once");
- it broke (the process died, a timeout, a refused tool list): discarded at once.
A model change is no rotation: `set_model()` on the client before the ask.

**Prompts (SPEC §5.4 revised, spike S4).** The custom system prompt (`prompts/dev/<kind>_system.md`, rendered by
the caller) is frozen per client and holds only fixed parts. `request.instructions`, the current owner-editable
block, goes into every ask as `additionalContext` from the `UserPromptSubmit` hook (`guard.GuardBridge`, the same
bridge the executor uses). `prompt_version` is logged and echoed in the reply; a change never rotates.

**Session options (SPEC §5.3, dev Assistant column).** `options.build_options` + `options.session_env` (cli_path, the
clean env, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `SendMessage`/`ListAgents` disallowed), then the assistant's own
values: cwd `<sessions>/<kind>`, `setting_sources=[]`, the custom system prompt, `tools=[]`, `dontAsk`,
`strict_mcp_config=True`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `models.<kind>` and the effort. The `init` tool list
must match exactly (`stream.tools_match`): nothing, or only `StructuredOutput` when the request carries a schema.
The guard allows `StructuredOutput` and denies everything else.

**Structured output (routing).** `output_schema` → `output_format={"type": "json_schema", ...}` (CLI
`--json-schema`); the model answers through the `StructuredOutput` tool and the result carries
`structured_output`, which becomes `AssistantReply.structured`.

**Errors (CONVENTIONS §7).** Every ask is bounded by its `timeout`. Timeout → `CallTimedOut` and the client is
discarded (the spare is promoted). A client that died before answering is skipped once, on the spare, if the budget
allows (no tools, so an ask has no side effect). Usage limit (`rate_limit_event` rejected, or a 429 result, as in the
executor) → `RateLimited(retry_after)`; 401/403 → `AuthFailed`; missing CLI → `NotInstalled`; anything else →
`ProviderUnavailable`. Cost is the per-ask delta in micro-USD (`stream.StreamTranslator`).

Logs: kind, session id, codes, counts, durations, versions, cost. Never prompts, instructions or answers.
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError

from yordamchi.adapters.claude import cli, control, proctree
from yordamchi.adapters.claude.guard import GuardBridge
from yordamchi.adapters.claude.options import Launch, build_options, session_env
from yordamchi.adapters.claude.stream import StreamTranslator
from yordamchi.core import aio, proc
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ConfigInvalid,
    ExternalError,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.ports.agent import (
    AgentEvent,
    Effort,
    FailureCode,
    GuardVerdict,
    Progress,
    RuntimeFailed,
    RuntimeStatus,
    SessionSpec,
    ToolCall,
    TurnResult,
    UsageLimited,
)
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)
from yordamchi.ports.clock import Clock

log = get_log(__name__)

PROVIDER: Final = "claude"
SUPPORTED_KINDS: Final[frozenset[AssistantKind]] = frozenset({"routing", "report", "settings"})
MAX_ASKS: Final = 20  # SPEC §5.3
CONTEXT_LIMIT_TOKENS: Final = 50_000
CONNECT_TIMEOUT_S: Final = 60.0  # a spare's spawn; `timeouts.session_open_s` default
CLOSE_TIMEOUT_S: Final = 5.0
KILL_GRACE_S: Final = 0.5
PS_TIMEOUT_S: Final = 1.0
GUARD_TIMEOUT_S: Final = 2.0
STRUCTURED_TOOL: Final = "StructuredOutput"
WEB_SEARCH_TOOL: Final = "WebSearch"
ASSISTANT_ENV: Final = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh", "max"})
DENY_MESSAGE: Final = "assistant_no_tools"
# Soft reasons let a warm client keep answering until the spare is ready; the others retire it before the ask.
SOFT_REASONS: Final = frozenset({"max_asks", "context_size", "effort_changed"})

type ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


@dataclass(frozen=True, slots=True)
class _LaunchKey:
    """The options a client is spawned with and cannot change later. The model is not here: `set_model()`."""

    system_prompt: str
    effort: str | None
    schema: str | None

    @classmethod
    def of(cls, profile: AssistantProfile) -> "_LaunchKey":
        if profile.effort is not None and profile.effort not in EFFORTS:
            raise ConfigInvalid(key="effort", problem="unknown_effort")
        schema = None if profile.output_schema is None else json.dumps(profile.output_schema, sort_keys=True)
        return cls(system_prompt=profile.system_prompt, effort=profile.effort, schema=schema)

    @property
    def frozen_part(self) -> tuple[str, str | None]:
        return self.system_prompt, self.schema


@dataclass(frozen=True, slots=True)
class _Frame:
    raw: Mapping[str, Any] | None
    events: Sequence[AgentEvent]


@dataclass(frozen=True, slots=True)
class _Ended:
    code: FailureCode


@dataclass(eq=False)
class _Client:
    kind: AssistantKind
    key: _LaunchKey
    session_id: str
    model: str
    settings_version: int
    translator: StreamTranslator
    queue: asyncio.Queue[_Frame | _Ended] = field(default_factory=asyncio.Queue)
    sdk: ClaudeSDKClient | None = None
    cli_pid: int | None = None
    pump: asyncio.Task[None] | None = None
    asks: int = 0
    context_tokens: int = 0
    context: str = ""  # this ask's instructions; the UserPromptSubmit hook returns it as additionalContext
    turn_over: bool = True  # False from the send until the turn's result: a client left mid-turn is discarded
    dead: bool = False
    closing: bool = False

    def emit(self, event: AgentEvent) -> None:
        self.queue.put_nowait(_Frame(raw=None, events=(event,)))


type _Built = _Client | ExternalError


@dataclass(eq=False)
class _Pool:
    kind: AssistantKind
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    live: _Client | None = None
    spare: asyncio.Task[_Built] | None = None
    spare_key: _LaunchKey | None = None
    profile: AssistantProfile | None = None
    prompt_version: int | None = None


@dataclass(slots=True)
class _TurnState:
    """What one ask's frames add up to: deltas out, the result and what came with it kept."""

    partial: bool = False
    structured: Any = None
    context: int = 0
    limited: UsageLimited | None = None
    failure: RuntimeFailed | None = None
    web_searches: int = 0
    result: TurnResult | None = None

    def read(self, frame: _Frame) -> list[str]:
        texts: list[str] = []
        raw = frame.raw
        kind = raw.get("type") if raw is not None else None
        if raw is not None and kind == "stream_event":
            text = _partial_delta(raw)
            if text:
                self.partial = True
                texts.append(text)
        elif raw is not None and kind == "assistant" and not self.partial:
            texts.extend(_text_deltas(raw))
        elif raw is not None and kind == "result":
            self.structured = raw.get("structured_output")
            self.context = context_tokens(raw)
        for event in frame.events:
            if isinstance(event, UsageLimited):
                self.limited = event
            elif isinstance(event, RuntimeFailed):
                self.failure = event
            elif isinstance(event, Progress) and event.kind == "tool_start" and event.tool_name == WEB_SEARCH_TOOL:
                self.web_searches += 1
            elif isinstance(event, TurnResult):
                self.result = event
        return texts


class _Broken(Exception):
    """The client failed before answering; `error` is what the caller gets if no retry happens."""

    def __init__(self, error: ExternalError, *, retry: bool) -> None:
        super().__init__(error.code.value)
        self.error = error
        self.retry = retry


async def _one(message: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield message


def _drop_stderr(line: str) -> None:
    """The CLI's stderr may echo content; it is never logged."""
    del line


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def context_tokens(result: Mapping[str, Any]) -> int:
    """The context the last model call of the turn saw, plus what it wrote: the next ask starts from this."""
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    iterations = usage.get("iterations")
    last = iterations[-1] if isinstance(iterations, list) and iterations else usage
    if not isinstance(last, Mapping):
        return 0
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(_int(last.get(key)) for key in keys)


def _json_text(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def structured_value(raw: Any, text: str, schema: Mapping[str, JsonValue]) -> JsonValue:
    """The schema's value from `structured_output`, else from the JSON result text. Raises when neither has the
    schema's top-level type (a malformed answer is the provider's failure, never a silent `None`)."""
    value = raw if raw is not None else _json_text(text)
    expected = schema.get("type")
    shapes: dict[str, type | tuple[type, ...]] = {"object": dict, "array": list, "string": str}
    shape = shapes.get(expected) if isinstance(expected, str) else None
    if value is None or (shape is not None and not isinstance(value, shape)):
        raise ProviderUnavailable(provider=PROVIDER, retryable=True)
    try:
        checked: JsonValue = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ProviderUnavailable(provider=PROVIDER, retryable=True) from None
    return checked


def _text_deltas(raw: Mapping[str, Any]) -> list[str]:
    """Text from a main-thread assistant message (whole blocks, when partial messages are off)."""
    if raw.get("parent_tool_use_id") is not None:
        return []
    message = raw.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [
        str(block["text"])
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def _partial_delta(raw: Mapping[str, Any]) -> str | None:
    """`stream_event` `content_block_delta` / `text_delta` (with `include_partial_messages`)."""
    if raw.get("parent_tool_use_id") is not None:
        return None
    event = raw.get("event")
    if not isinstance(event, Mapping) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if isinstance(delta, Mapping) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
        return str(delta["text"])
    return None


def _usage(result: TurnResult, context: int) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    for usage in result.model_usage.values():
        totals["input_tokens"] += usage.input_tokens
        totals["output_tokens"] += usage.output_tokens
        totals["cache_read_tokens"] += usage.cache_read_tokens
        totals["cache_write_tokens"] += usage.cache_write_tokens
    totals["context_tokens"] = context
    return totals


def _connect_error(error: BaseException) -> ExternalError:
    code, retryable = control.failure(error)
    if code == "cli_missing":
        return NotInstalled(binary="claude")
    if code == "auth_failed":
        return AuthFailed(provider=PROVIDER)
    return ProviderUnavailable(provider=PROVIDER, retryable=retryable)


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


class ClaudeAssistant:
    """Bootstrap values only (CONVENTIONS §4): binary, child PATH, the sessions dir. Behaviour arrives per call."""

    def __init__(
        self,
        *,
        cli_path: Path,
        search_path: str,
        sessions_dir: Path,
        clock: Clock | None = None,
        base_env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        max_asks: int = MAX_ASKS,
        context_limit_tokens: int = CONTEXT_LIMIT_TOKENS,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self._cli_path = cli_path
        self._search_path = search_path
        self._sessions_dir = sessions_dir
        self._clock = clock
        self._base_env = base_env
        self._factory: ClientFactory = client_factory or (lambda options: ClaudeSDKClient(options=options))
        self._max_asks = max_asks
        self._context_limit = context_limit_tokens
        self._connect_timeout = connect_timeout_s
        self._kill_grace_s = kill_grace_s
        self._pools: dict[AssistantKind, _Pool] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._closed = False

    # --- port ---------------------------------------------------------------------------------------------------
    async def probe(self, *, timeout: float) -> RuntimeStatus:
        status = await cli.probe(
            self._cli_path, search_path=self._search_path, base_env=self._base_env, timeout=timeout
        )
        log.info("assistant_probed", version=status.version, logged_in=status.logged_in, auth_method=status.auth_method)
        return status

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        key = self._key(profile)
        pool = self._pool(profile.kind)
        try:
            async with asyncio.timeout(timeout):
                async with pool.lock:
                    pool.profile = profile
                    await self._acquire(pool, profile, key)
        except TimeoutError:
            raise CallTimedOut(operation=f"assistant_{profile.kind}_warm") from None

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        reply: AssistantReply | None = None
        try:
            async with asyncio.timeout(timeout):
                async with contextlib.aclosing(self._run(request, idle=None)) as items:
                    async for item in items:
                        if isinstance(item, AssistantReply):
                            reply = item
        except TimeoutError:
            log.warning("assistant_ask_timed_out", kind=request.kind, timeout_s=timeout)
            raise CallTimedOut(operation=f"assistant_{request.kind}") from None
        if reply is None:  # _run always ends with a reply or raises
            raise ProviderUnavailable(provider=PROVIDER, retryable=True)
        return reply

    async def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        try:
            async with contextlib.aclosing(self._run(request, idle=idle_timeout)) as items:
                async for item in items:
                    yield item
        except TimeoutError:
            log.warning("assistant_stream_timed_out", kind=request.kind, idle_timeout_s=idle_timeout)
            raise CallTimedOut(operation=f"assistant_{request.kind}") from None

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        del conversation  # dev kinds are stateless asks; personal conversations come with the `qa` kind
        pool = self._pools.get(kind)
        if pool is None:
            return
        try:
            async with asyncio.timeout(timeout), pool.lock:
                if pool.live is not None:
                    self._retire(pool, pool.live, reason="reset")
        except TimeoutError:
            raise CallTimedOut(operation=f"assistant_{kind}_reset") from None

    # --- lifecycle (composition root) ---------------------------------------------------------------------------
    async def close(self, *, timeout: float) -> None:
        """Shutdown: close every live client and spare, and wait for the closes already under way."""
        self._closed = True
        clients: list[_Client] = []
        for pool in self._pools.values():
            if pool.live is not None:
                clients.append(pool.live)
                pool.live = None
            if pool.spare is not None:
                spare, pool.spare = pool.spare, None
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(timeout):
                        await asyncio.shield(spare)
                if spare.done() and not spare.cancelled() and isinstance(spare.result(), _Client):
                    clients.append(cast("_Client", spare.result()))
                elif not spare.done():
                    spare.cancel()
        await asyncio.gather(*(self._close_client(client) for client in clients))
        pending = list(self._closing)
        if pending:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await asyncio.gather(*pending, return_exceptions=True)
        log.info("assistant_closed", clients=len(clients), pending=len(pending))

    # --- the ask ------------------------------------------------------------------------------------------------
    def _key(self, profile: AssistantProfile) -> _LaunchKey:
        if profile.kind not in SUPPORTED_KINDS:
            raise ConfigInvalid(key="assistant_kind", problem="unsupported_kind")
        return _LaunchKey.of(profile)

    def _pool(self, kind: AssistantKind) -> _Pool:
        if self._closed:
            raise ProviderUnavailable(provider=PROVIDER, retryable=False)
        pool = self._pools.get(kind)
        if pool is None:
            pool = self._pools[kind] = _Pool(kind=kind)
        return pool

    async def _bounded[T](self, awaitable: Awaitable[T], idle: float | None) -> T:
        """`ask()` bounds the whole call from outside; `stream()` bounds each wait by its idle timeout."""
        if idle is None:
            return await awaitable
        async with asyncio.timeout(idle):
            return await awaitable

    async def _run(
        self, request: AssistantRequest, *, idle: float | None
    ) -> AsyncGenerator[AssistantDelta | AssistantReply]:
        profile = request.profile
        key = self._key(profile)
        pool = self._pool(request.kind)
        content = await cli.user_content(request.prompt, request.images)
        await self._bounded(pool.lock.acquire(), idle)
        try:
            pool.profile = profile
            self._track_prompt_version(pool, request.prompt_version)
            for attempt in (1, 2):
                client = await self._bounded(self._acquire(pool, profile, key), idle)
                streamed = False
                try:
                    async with contextlib.aclosing(self._turn(pool, client, request, content, idle)) as items:
                        async for item in items:
                            streamed = streamed or isinstance(item, AssistantDelta)
                            yield item
                except _Broken as broken:
                    if attempt == 1 and broken.retry and not streamed:
                        log.warning("assistant_ask_retried", kind=request.kind, code=broken.error.code.value)
                        continue
                    raise broken.error from None
                return
        finally:
            pool.lock.release()

    def _track_prompt_version(self, pool: _Pool, version: int) -> None:
        if pool.prompt_version is not None and pool.prompt_version != version:
            log.info("assistant_prompt_version_changed", kind=pool.kind, previous=pool.prompt_version, current=version)
        pool.prompt_version = version

    async def _turn(
        self,
        pool: _Pool,
        client: _Client,
        request: AssistantRequest,
        content: str | list[dict[str, Any]],
        idle: float | None,
    ) -> AsyncGenerator[AssistantDelta | AssistantReply]:
        client.turn_over = False
        try:
            await self._bounded(self._ensure_model(client, request.model), idle)
            self._drain(client)
            command_uuid = str(uuid.uuid4())
            payload: dict[str, Any] = {
                "type": "user",
                "uuid": command_uuid,
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": "default",
                "priority": "later",
            }
            client.context = request.instructions
            client.translator.sent(command_uuid)
            sdk = client.sdk
            if sdk is None:
                raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=True), retry=True)
            try:
                await self._bounded(sdk.query(_one(payload)), idle)
            except (ClaudeSDKError, OSError) as error:
                raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=True), retry=True) from error
            async with contextlib.aclosing(self._collect(pool, client, request, idle)) as items:
                async for item in items:
                    yield item
        finally:
            client.context = ""
            if client.dead:
                if pool.live is client:
                    self._retire(pool, client, reason="broken")
            elif not client.turn_over:
                self._retire(pool, client, reason="ask_failed")  # a timeout or cancel mid-turn
            else:
                self._rotate_if_due(pool)

    async def _ensure_model(self, client: _Client, model: str) -> None:
        if client.model == model or client.sdk is None:
            return
        try:
            await control.set_model(client.sdk, model)
        except control.ControlFailed as error:
            raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=True), retry=True) from error
        log.info("assistant_model_set", kind=client.kind, session_id=client.session_id, model=model)
        client.model = model

    def _drain(self, client: _Client) -> None:
        """Frames left from the previous ask (a trailing state change) are old news; a dead client is broken."""
        while True:
            try:
                frame = client.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(frame, _Ended):
                client.dead = True
                raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=True), retry=True)

    async def _next(self, client: _Client, idle: float | None) -> _Frame | _Ended:
        return await self._bounded(client.queue.get(), idle)

    async def _collect(
        self, pool: _Pool, client: _Client, request: AssistantRequest, idle: float | None
    ) -> AsyncGenerator[AssistantDelta | AssistantReply]:
        started = time.monotonic()
        turn = _TurnState()
        while turn.result is None:
            frame = await self._next(client, idle)
            if isinstance(frame, _Ended):
                client.dead = True
                if turn.failure is not None and turn.failure.code == "protocol":
                    log.error("assistant_tools_mismatch", kind=client.kind, session_id=client.session_id)
                    raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=False), retry=False)
                raise _Broken(ProviderUnavailable(provider=PROVIDER, retryable=True), retry=True)
            for text in turn.read(frame):
                yield AssistantDelta(text=text)
        result = turn.result
        last_context = turn.context
        limited, failure, web_searches, structured_raw = turn.limited, turn.failure, turn.web_searches, turn.structured
        client.turn_over = True
        client.asks += 1
        client.context_tokens = last_context
        client.settings_version = max(client.settings_version, request.settings_version)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._raise_for(pool, client, result, limited=limited, failure=failure, duration_ms=duration_ms)
        structured: JsonValue = None
        if request.output_schema is not None:
            structured = structured_value(structured_raw, result.text, request.output_schema)
        log.info(
            "assistant_answered",
            kind=client.kind,
            session_id=client.session_id,
            model=client.model,
            asks=client.asks,
            context_tokens=last_context,
            settings_version=client.settings_version,
            prompt_version=request.prompt_version,
            duration_ms=duration_ms,
            cost_usd_micros=result.cost_usd_micros,
            structured=structured is not None,
        )
        yield AssistantReply(
            text=result.text,
            provider=PROVIDER,
            model=client.model,
            duration_ms=duration_ms,
            structured=structured,
            session_id=client.session_id,
            cost_usd_micros=result.cost_usd_micros,
            web_searches=web_searches,
            usage=_usage(result, last_context),
            prompt_version=request.prompt_version,
        )

    def _raise_for(
        self,
        pool: _Pool,
        client: _Client,
        result: TurnResult,
        *,
        limited: UsageLimited | None,
        failure: RuntimeFailed | None,
        duration_ms: int,
    ) -> None:
        """A finished turn that is not an answer. The client stays (the turn is over), except after an auth error."""
        if failure is not None and failure.code == "auth_failed":
            log.warning("assistant_auth_failed", kind=client.kind, session_id=client.session_id)
            self._retire(pool, client, reason="auth_failed")
            raise AuthFailed(provider=PROVIDER)
        if limited is not None:
            retry_after = None
            if limited.resets_at is not None and self._clock is not None:
                retry_after = max(0.0, (limited.resets_at - self._clock.now()).total_seconds())
            log.warning("assistant_usage_limited", kind=client.kind, limit_type=limited.limit_type)
            raise RateLimited(provider=PROVIDER, retry_after=retry_after)
        if result.is_error:
            log.warning("assistant_turn_failed", kind=client.kind, subtype=result.subtype, duration_ms=duration_ms)
            raise ProviderUnavailable(provider=PROVIDER, retryable=True)

    # --- the pool -----------------------------------------------------------------------------------------------
    def _stale(self, client: _Client, profile: AssistantProfile, key: _LaunchKey) -> str | None:
        """Why `client` must not (hard) or should not (soft) answer `profile`'s next ask; None = use it."""
        if profile.settings_version > client.settings_version and client.asks == 0:
            client.settings_version = profile.settings_version  # no conversation yet: nothing old to drop
        reasons = (
            ("broken", client.dead),
            ("launch_changed", client.key.frozen_part != key.frozen_part),
            ("settings_version", profile.settings_version > client.settings_version),
            ("max_asks", client.asks >= self._max_asks),
            ("context_size", client.context_tokens >= self._context_limit),
            ("effort_changed", client.key.effort != key.effort),
        )
        return next((reason for reason, due in reasons if due), None)

    async def _acquire(self, pool: _Pool, profile: AssistantProfile, key: _LaunchKey) -> _Client:
        live = pool.live
        reason = "empty" if live is None else self._stale(live, profile, key)
        if live is not None and reason is None:
            self._ensure_spare(pool, profile, key)
            return live
        ready = self._ready_spare(pool, key)
        if ready is None and live is not None and reason in SOFT_REASONS:
            self._ensure_spare(pool, profile, key)
            return live
        if live is not None:
            self._retire(pool, live, reason=reason or "rotated", promote=False)
        if ready is None:
            task = self._ensure_spare(pool, profile, key)
            built = await asyncio.shield(task)
            if isinstance(built, ExternalError):
                if pool.spare is task:
                    pool.spare, pool.spare_key = None, None
                raise built
            ready = built
        self._promote(pool, ready, profile)
        self._ensure_spare(pool, profile, key)
        return ready

    def _ready_spare(self, pool: _Pool, key: _LaunchKey) -> _Client | None:
        task = pool.spare
        if task is None or not task.done() or task.cancelled() or pool.spare_key != key:
            return None
        built = task.result()
        if isinstance(built, ExternalError) or built.dead:
            return None
        return built

    def _promote(self, pool: _Pool, client: _Client, profile: AssistantProfile) -> None:
        pool.spare, pool.spare_key = None, None
        pool.live = client
        client.settings_version = max(client.settings_version, profile.settings_version)
        log.info("assistant_spare_promoted", kind=pool.kind, session_id=client.session_id)

    def _ensure_spare(self, pool: _Pool, profile: AssistantProfile, key: _LaunchKey) -> "asyncio.Task[_Built]":
        """The spare for `key`: the one there is if it matches and is (or will be) usable, else a new one."""
        task = pool.spare
        if task is not None and pool.spare_key == key and not task.cancelled():
            if not task.done():
                return task
            built = task.result()
            if isinstance(built, _Client) and not built.dead:
                return task
        if task is not None:
            self._discard_spare(task)
        pool.spare_key = key
        pool.spare = aio.spawn(f"assistant_{pool.kind}_spare", self._build(profile, key))
        return pool.spare

    def _discard_spare(self, task: "asyncio.Task[_Built]") -> None:
        def close_when_built(done: "asyncio.Task[_Built]") -> None:
            if done.cancelled():
                return
            built = done.result()
            if isinstance(built, _Client):
                built.dead = True
                self._spawn_close(built)

        if task.done():
            close_when_built(task)
        else:
            task.add_done_callback(close_when_built)

    def _rotate_if_due(self, pool: _Pool) -> None:
        """After an answer: if the live client is now due and the spare is ready, swap now, off the ask path."""
        live, profile = pool.live, pool.profile
        if live is None or profile is None:
            return
        key = _LaunchKey.of(profile)
        reason = self._stale(live, profile, key)
        ready = self._ready_spare(pool, key)
        if reason is not None and ready is not None:
            self._retire(pool, live, reason=reason, promote=False)
            self._promote(pool, ready, profile)
            self._ensure_spare(pool, profile, key)

    def _retire(self, pool: _Pool, client: _Client, *, reason: str, promote: bool = True) -> None:
        """Take `client` out of service now and close it in the background; promote a ready spare if asked."""
        if pool.live is client:
            pool.live = None
        client.dead = True
        log.info(
            "assistant_client_retired",
            kind=pool.kind,
            session_id=client.session_id,
            reason=reason,
            asks=client.asks,
            context_tokens=client.context_tokens,
        )
        self._spawn_close(client)
        profile = pool.profile
        if not promote or profile is None or self._closed:
            return
        key = _LaunchKey.of(profile)
        ready = self._ready_spare(pool, key)
        if ready is not None:
            self._promote(pool, ready, profile)
        self._ensure_spare(pool, profile, key)

    # --- one client ---------------------------------------------------------------------------------------------
    def _spec(self, profile: AssistantProfile, cwd: Path, client: _Client, expected: frozenset[str]) -> SessionSpec:
        async def guard(call: ToolCall) -> GuardVerdict:
            if call.tool_name in expected:
                return GuardVerdict(allow=True)
            return GuardVerdict(allow=False, message=DENY_MESSAGE, reason_code=DENY_MESSAGE)

        return SessionSpec(
            kind="investigation",  # never read by build_options or the bridge; the closest shape (dontAsk, no edits)
            cwd=cwd,
            model=profile.model,
            guard=guard,
            effort=cast("Effort | None", profile.effort),
            context_provider=lambda: client.context,
            setting_sources=(),
            tools=(),
            permission_mode="dontAsk",
            strict_mcp=True,
            env=ASSISTANT_ENV,
            expected_tools=expected,
            hook_timeout_s=GUARD_TIMEOUT_S,
        )

    async def _build(self, profile: AssistantProfile, key: _LaunchKey) -> _Built:
        session_id = str(uuid.uuid4())
        expected = frozenset({STRUCTURED_TOOL}) if profile.output_schema is not None else frozenset[str]()
        client = _Client(
            kind=profile.kind,
            key=key,
            session_id=session_id,
            model=profile.model,
            settings_version=profile.settings_version,
            translator=StreamTranslator(session_id=session_id, expected_tools=expected),
        )
        cwd = self._sessions_dir / profile.kind
        try:
            await asyncio.to_thread(_ensure_dir, cwd)
            spec = self._spec(profile, cwd, client, expected)
            bridge = GuardBridge(
                spec, session_id=session_id, emit=client.emit, blocked=client.translator.blocked_reason
            )
            env = session_env(spec, search_path=self._search_path, githooks_dir=None, base=self._base_env)
            options = build_options(
                spec,
                cli_path=self._cli_path,
                env=env,
                launch=Launch(session_id=session_id, resume=False),
                hooks=bridge.hooks(),
                can_use_tool=bridge.can_use_tool,
                stderr=_drop_stderr,
            )
        except (OSError, ValueError) as error:
            log.error("assistant_client_failed", kind=profile.kind, error_type=type(error).__name__)
            return ProviderUnavailable(provider=PROVIDER, retryable=False)
        output_format = None
        if profile.output_schema is not None:
            output_format = {"type": "json_schema", "schema": dict(profile.output_schema)}
        options = dataclasses.replace(
            options,
            system_prompt=profile.system_prompt,  # custom and frozen: only the fixed parts (SPEC §5.4)
            output_format=output_format,
            include_partial_messages=profile.kind == "qa",
        )
        sdk = self._factory(options)
        client.sdk = sdk
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._connect_timeout):
                await control.connect(sdk)
        except TimeoutError:
            log.warning("assistant_connect_timed_out", kind=profile.kind, session_id=session_id)
            client.dead = True
            client.cli_pid = control.cli_pid(sdk)
            await self._close_client(client)
            return CallTimedOut(operation=f"assistant_{profile.kind}_connect")
        except (ClaudeSDKError, OSError, control.PrivateApiMissing) as error:
            mapped = _connect_error(error)
            log.warning("assistant_connect_failed", kind=profile.kind, session_id=session_id, code=mapped.code.value)
            client.dead = True
            return mapped
        client.cli_pid = control.cli_pid(sdk)
        client.pump = aio.spawn(f"assistant_pump_{session_id[:8]}", self._pump(client, sdk, bridge))
        log.info(
            "assistant_client_started",
            kind=profile.kind,
            session_id=session_id,
            model=profile.model,
            effort=profile.effort,
            cli_pid=client.cli_pid,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if self._closed:
            client.dead = True
            await self._close_client(client)
        return client

    async def _pump(self, client: _Client, sdk: ClaudeSDKClient, bridge: GuardBridge) -> None:
        """Raw stream → translator → the client's queue; ends with exactly one `_Ended`."""
        code: FailureCode = "process_died"
        try:
            async with contextlib.aclosing(control.raw_messages(sdk)) as stream:
                async for message in stream:
                    events = client.translator.feed(message)
                    client.queue.put_nowait(_Frame(raw=message, events=tuple(events)))
                    if client.translator.refused:
                        code = "protocol"
                        break
        except (ClaudeSDKError, OSError, control.PrivateApiMissing, control.StreamFailed) as error:
            code, _ = control.failure(error)
            if not client.closing:
                log.warning("assistant_client_failed", kind=client.kind, session_id=client.session_id, code=code)
        finally:
            client.dead = True
            bridge.release_questions()
            client.queue.put_nowait(_Ended(code=code))

    def _spawn_close(self, client: _Client) -> None:
        if client.closing:
            return
        task = aio.spawn(f"assistant_close_{client.session_id[:8]}", self._close_client(client))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_client(self, client: _Client) -> None:
        if client.closing:
            return
        client.closing = True
        client.dead = True
        sdk = client.sdk
        if sdk is not None:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await sdk.disconnect()
            except (TimeoutError, ClaudeSDKError, OSError) as error:
                log.warning("assistant_disconnect_failed", session_id=client.session_id, error=type(error).__name__)
                await self._kill(client)
        pump = client.pump
        if pump is not None and not pump.done():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._kill_grace_s * 2):
                    await asyncio.shield(pump)
            if not pump.done():
                pump.cancel()
        log.info("assistant_client_closed", kind=client.kind, session_id=client.session_id)

    async def _kill(self, client: _Client) -> None:
        """TERM, grace, KILL: the CLI by pid and its descendants' groups, never the bot's own group (S2)."""
        pid = client.cli_pid
        if pid is None:
            log.error("assistant_kill_skipped", session_id=client.session_id, reason="no_pid")
            return
        env = proc.clean_env(path=self._search_path, base=self._base_env)
        try:
            rows = await proctree.list_processes(env=env, timeout=PS_TIMEOUT_S)
        except (TimeoutError, OSError):
            rows = []
        own_pgid, own_pid = os.getpgrp(), os.getpid()
        plan = proctree.kill_plan(rows, root=pid, own_pgid=own_pgid, own_pid=own_pid)
        proctree.send(plan, signal.SIGTERM, own_pgid=own_pgid)
        pump = client.pump
        if pump is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._kill_grace_s):
                    await asyncio.shield(pump)
        proctree.send(plan, signal.SIGKILL, own_pgid=own_pgid)
        log.warning("assistant_client_killed", session_id=client.session_id, groups=len(plan.groups))


def _conforms(assistant: ClaudeAssistant) -> Assistant:
    """mypy checks the port here."""
    return assistant
