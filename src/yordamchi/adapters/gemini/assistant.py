"""`GeminiAssistant`: the "zaxira model" of the personal bot (Q4; SPEC §3.2.13 step 9; T72).

While Claude is unavailable (all accounts limited, the login refused, the CLI missing), plain questions are answered
by a Gemini model (`models.fallback_text`, default `gemini-3.8-flash`) on the owner's Google key from the vault. The
caller labels every such answer "(zaxira model)". It is deliberately small:
- one stateless Interactions call per ask (`store: False`): no tools, no web, no conversation, no images;
- the system instruction is the caller's fixed prompt (`prompts/personal/qa_fallback_system.md`) followed by this
  ask's owner-instructions block (persona, language, limits), so the same answer contract holds;
- `stream()` yields the whole text once (a fallback answer is short and arrives in a few seconds).

Errors map exactly as in `gemini/stt.py` (`translate_error`): key failures → `AuthFailed`, 429 → `RateLimited` or
`QuotaExhausted`, timeouts → `CallTimedOut`, 5xx and network → retryable `ProviderUnavailable`. Only `core.retry`
retries; the SDK's own retries are off. Logs: model, codes, counts, durations; never the prompt or the answer.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._gaos.lib import compat_errors as interaction_errors

from yordamchi.adapters.gemini.stt import clean_model_id, default_client, is_thinking_rejection, translate_error
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.core.errors import ExternalError, ProviderUnavailable
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)

log = get_log(__name__)

PROVIDER: Final = "google"
OPERATION: Final = "assistant_fallback"
THINKING: Final = "low"
_VENDOR_ERRORS: Final = (genai_errors.APIError, interaction_errors.APIError, httpx.HTTPError)

type ClientFactory = Callable[[str], genai.Client]


def build_request(request: AssistantRequest, *, thinking: str | None) -> dict[str, Any]:
    """The `interactions.create` keyword arguments for one fallback answer."""
    system = "\n\n".join(part for part in (request.system_prompt.strip(), request.instructions.strip()) if part)
    body: dict[str, Any] = {
        "model": clean_model_id(request.model),
        "input": [{"type": "text", "text": request.prompt}],
        "system_instruction": system,
        "store": False,
    }
    if thinking:
        body["generation_config"] = {"thinking_level": thinking}
    return body


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    found: dict[str, int] = {}
    for name, field in (("input_tokens", "total_input_tokens"), ("output_tokens", "total_output_tokens")):
        value = getattr(usage, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            found[name] = value
    return found


class GeminiAssistant:
    """`Assistant` on a direct Google key. `api_key` is the key or a callable returning the vault's current one."""

    def __init__(self, *, api_key: ApiKey, client_factory: ClientFactory = default_client) -> None:
        self._api_key = api_key
        self._client_factory = client_factory
        self._client: tuple[str, genai.Client] | None = None
        self._client_lock = asyncio.Lock()
        self._thinking: dict[str, str | None] = {}  # per model: the thinking level it accepted last

    def _new_client(self, key: str) -> genai.Client:
        client = self._client_factory(key)
        interactions: Any = client.aio.interactions
        interactions.sdk_configuration.retry_config = None  # core.retry is the only retry layer
        return client

    async def _client_for(self, key: str) -> genai.Client:
        async with self._client_lock:
            if self._client is not None and self._client[0] == key:
                return self._client[1]
            old = self._client
            client = self._new_client(key)
            self._client = (key, client)
        if old is not None:
            await old[1].aio.aclose()
        return client

    async def aclose(self) -> None:
        async with self._client_lock:
            old, self._client = self._client, None
        if old is not None:
            await old[1].aio.aclose()

    # --- port ---------------------------------------------------------------------------------------------------
    async def probe(self, *, timeout: float) -> RuntimeStatus:
        """Usable when a Google key is stored; nothing is sent (the key is checked when it is added)."""
        del timeout
        try:
            await resolve_key(self._api_key, provider=PROVIDER)
        except ExternalError:
            return RuntimeStatus(installed=True, version=None, logged_in=False, auth_method=None, missing=("key",))
        return RuntimeStatus(installed=True, version=None, logged_in=True, auth_method="api_key")

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        del profile, timeout  # stateless: nothing to warm

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        del kind, conversation, timeout  # stateless: nothing to drop

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        started = time.monotonic()
        key = await resolve_key(self._api_key, provider=PROVIDER)
        model = clean_model_id(request.model)
        attempts = 0

        async def attempt() -> Any:
            nonlocal attempts
            attempts += 1
            client = await self._client_for(key)
            response = await self._create(client, request, model=model, timeout=timeout)
            if getattr(response, "status", None) == "failed" or not str(getattr(response, "output_text", "") or ""):
                raise ProviderUnavailable(provider=PROVIDER)  # a failed or empty interaction: worth one more try
            return response

        try:
            response = await retrying(attempt, timeout=timeout, operation=OPERATION)
        except ExternalError as error:
            log.warning(
                "assistant_fallback_failed",
                provider=PROVIDER,
                model=model,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        text = str(getattr(response, "output_text", None) or "").strip()
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = _usage(response)
        log.info(
            "assistant_fallback_answered",
            provider=PROVIDER,
            model=model,
            attempts=attempts,
            chars=len(text),
            duration_ms=duration_ms,
            **usage,
        )
        return AssistantReply(
            text=text,
            provider=PROVIDER,
            model=model,
            duration_ms=duration_ms,
            usage=usage,
            prompt_version=request.prompt_version,
        )

    async def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        reply = await self.ask(request, timeout=idle_timeout)
        yield AssistantDelta(text=reply.text)
        yield reply

    async def _create(self, client: genai.Client, request: AssistantRequest, *, model: str, timeout: float) -> Any:
        """One logical attempt; a rejected thinking level is retried at once without it (not a failure)."""
        interactions: Any = client.aio.interactions
        while True:
            thinking = self._thinking.get(model, THINKING)
            try:
                return await interactions.create(**build_request(request, thinking=thinking), timeout=timeout)
            except _VENDOR_ERRORS as exc:
                if thinking and is_thinking_rejection(exc):
                    self._thinking[model] = None
                    log.info("assistant_fallback_thinking_rejected", provider=PROVIDER, model=model)
                    continue
                raise translate_error(exc, operation=OPERATION) from exc


def _conforms(assistant: GeminiAssistant) -> Assistant:
    """mypy checks the port here."""
    return assistant
