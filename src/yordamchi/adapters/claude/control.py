"""Every use of the SDK's private surface, and its error translation, in one place (docs/SPIKES.md N1, N4).

Re-check on each SDK upgrade; `tests/integration/test_claude_executor.py` pins each fallback.
- `raw_messages()`: the stream before `parse_message`, which drops `command_lifecycle` (N1). Reads
  `client._query.receive_messages()`. If that is gone the session cannot track merged inputs, so it fails loudly
  (`PrivateApiMissing` → `RuntimeFailed(protocol)`) instead of guessing.
- `interrupt()`: `_query._send_control_request({"subtype": "interrupt", "cancel_queued": True})` returns the receipt
  (`still_queued`, `cancelled`) that `ClaudeSDKClient.interrupt()` discards (N4). If the private call is gone it falls
  back to the public `interrupt()` and reports no receipt.
- `cli_pid()`: the CLI's pid from the initialize response (`pid`), else from the transport's process.
- `connect()`: filters exactly `CanUseToolShadowedWarning`. The SDK raises it on every connect that combines
  `can_use_tool` with bypass or a whole-tool allow rule, yet `AskUserQuestion` still reaches the callback (S3), and the
  suite runs with `filterwarnings = error`.
- The SDK raises bare `Exception` for control errors (a refused request, its own timeout) and for stream errors it
  cannot type; those are re-raised here as `ControlFailed` / `StreamFailed`, so callers catch named types only.
"""

import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultError,
)
from claude_agent_sdk.types import CanUseToolShadowedWarning

from yordamchi.ports.agent import FailureCode

MISSING_SESSION_TEXT: Final = "No conversation found"
AUTH_STATUSES: Final = frozenset({401, 403})


class PrivateApiMissing(RuntimeError):
    """An SDK upgrade removed a private attribute this module relies on."""


class ControlFailed(RuntimeError):
    """A control request (interrupt, stop_task, set_model) was refused or timed out inside the SDK."""


class StreamFailed(RuntimeError):
    """The message stream ended with an error the SDK did not type."""


@dataclass(frozen=True, slots=True)
class InterruptReceipt:
    still_queued: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    has_receipt: bool = False  # False: the public fallback ran, which returns nothing


async def connect(client: ClaudeSDKClient) -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
        await client.connect()


def _query(client: ClaudeSDKClient) -> Any:
    query = getattr(client, "_query", None)
    if query is None:
        raise PrivateApiMissing("ClaudeSDKClient._query")
    return query


async def raw_messages(client: ClaudeSDKClient) -> AsyncGenerator[dict[str, Any]]:
    receive = getattr(_query(client), "receive_messages", None)
    if receive is None:
        raise PrivateApiMissing("Query.receive_messages")
    stream: AsyncIterator[dict[str, Any]] = receive()
    try:
        async for message in stream:
            yield message
    except (ClaudeSDKError, PrivateApiMissing):
        raise
    except Exception as error:
        raise StreamFailed(type(error).__name__) from error


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


async def interrupt(client: ClaudeSDKClient, *, cancel_queued: bool, timeout: float) -> InterruptReceipt:
    """Stop the running turn; with `cancel_queued`, also drop inputs the CLI has queued but not started."""
    send = getattr(getattr(client, "_query", None), "_send_control_request", None)
    try:
        if send is None:
            await client.interrupt()
            return InterruptReceipt()
        request: dict[str, Any] = {"subtype": "interrupt"}
        if cancel_queued:
            request["cancel_queued"] = True
        response = await send(request, timeout=timeout)
    except Exception as error:
        raise ControlFailed("interrupt") from error
    if not isinstance(response, Mapping):
        return InterruptReceipt(has_receipt=True)
    return InterruptReceipt(
        still_queued=_strings(response.get("still_queued")),
        cancelled=_strings(response.get("cancelled")),
        has_receipt=True,
    )


async def stop_task(client: ClaudeSDKClient, task_id: str) -> None:
    try:
        await client.stop_task(task_id)
    except Exception as error:
        raise ControlFailed("stop_task") from error


async def set_model(client: ClaudeSDKClient, model: str) -> None:
    try:
        await client.set_model(model)
    except Exception as error:
        raise ControlFailed("set_model") from error


def cli_pid(client: ClaudeSDKClient) -> int | None:
    info = getattr(getattr(client, "_query", None), "_initialization_result", None)
    pid = info.get("pid") if isinstance(info, Mapping) else None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return pid
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def failure(error: BaseException) -> tuple[FailureCode, bool]:
    """`RuntimeFailed(code, retryable)` for an error from connect or the stream. Never inspects more than the type,
    the result payload's status and the SDK's fixed "No conversation found" phrase."""
    if isinstance(error, CLINotFoundError):
        return "cli_missing", False
    if MISSING_SESSION_TEXT in str(error):
        return "session_not_found", False
    if isinstance(error, ResultError) and error.api_error_status in AUTH_STATUSES:
        return "auth_failed", False
    if isinstance(error, CLIJSONDecodeError | PrivateApiMissing | StreamFailed):
        return "protocol", False
    if isinstance(error, ProcessError | CLIConnectionError | OSError):
        return "process_died", True
    return "protocol", False
