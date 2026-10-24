"""aiogram and Telegram failures → the `ExternalError` hierarchy (CONVENTIONS §7).

Telegram answers `{"ok": false, "error_code": …, "description": …, "parameters": {"retry_after": …}}` (Bot API
"Making requests", "ResponseParameters"); aiogram turns that into one exception class per HTTP status. This module
maps each class to an `ExternalError` with the right `retryable`, plus a snake_case `reason` for logs and for the few
refusals the adapter treats as a normal outcome (an unchanged edit, an undeletable message, a chat already left).

The vendor description is never copied into an error or a log: it is free text and may echo message content. The
original exception is never chained either, because aiohttp errors carry `api.telegram.org/bot<token>/…` in their
URL (STACK §4.3).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    MarkupRejected,
    ProviderUnavailable,
    RateLimited,
)

PROVIDER: Final = "telegram"

# Limits (Bot API "getFile": bots download ≤ 20 MB; "Sending files": multipart ≤ 10 MB for photos, 50 MB otherwise).
# Telegram counts in MiB.
INBOUND_MAX_BYTES: Final = 20 * 1024 * 1024
OUTBOUND_MAX_BYTES: Final = 50 * 1024 * 1024
OUTBOUND_PHOTO_MAX_BYTES: Final = 10 * 1024 * 1024

type Direction = Literal["in", "out"]


class TelegramFileTooLarge(ProviderUnavailable):
    """A file over Telegram's limit (NFR-CAP-04): `in` = getFile serves ≤ 20 MB to bots, `out` = uploads ≤ 50 MB
    (photos ≤ 10 MB). Never retryable: waiting does not shrink a file.

    Interim class: `core/errors.py` has no code for this yet (see the T09 report, "Pending wiring"). It subclasses
    `ProviderUnavailable(retryable=False)` so every caller already treats it as a final refusal; the wiring task
    replaces it with a `FileTooLarge` error and its own `S.ERRORS` entry.
    """

    def __init__(self, *, direction: Direction, limit_bytes: int, size_bytes: int | None = None) -> None:
        super().__init__(provider=PROVIDER, retryable=False)
        self.direction: Direction = direction
        self.limit_bytes = limit_bytes
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class Translated:
    error: ExternalError
    reason: str
    # The request may have reached Telegram (a network error or an unreadable answer): resending can duplicate.
    delivery_unknown: bool = False


# Telegram's descriptions are documented as unstable, so only well-known prefixes are matched and anything else is
# `other`. Order matters: the first match wins.
_REASONS: Final[tuple[tuple[str, str], ...]] = (
    ("message is not modified", "message_not_modified"),
    ("message to delete not found", "message_not_found"),
    ("message to edit not found", "message_not_found"),
    ("message can't be deleted", "message_cant_be_deleted"),
    ("message to be replied not found", "reply_target_not_found"),
    ("file is too big", "file_too_big"),
    ("chat not found", "chat_not_found"),
    ("bot was kicked", "bot_not_member"),
    ("bot is not a member", "bot_not_member"),
    ("bot was blocked", "bot_blocked"),
    ("not enough rights", "not_enough_rights"),
    ("can't parse entities", "parse_entities_failed"),
    ("reaction_invalid", "reaction_invalid"),
    ("message is too long", "message_too_long"),
    ("too many requests", "too_many_requests"),
)
_RETRY_AFTER_TEXT: Final = re.compile(r"retry after (\d+)", re.IGNORECASE)


def reason_of(description: str) -> str:
    lowered = description.lower()
    for needle, reason in _REASONS:
        if needle in lowered:
            return reason
    return "other"


def _refused(reason: str) -> Translated:
    return Translated(ProviderUnavailable(provider=PROVIDER, retryable=False), reason)


def _retry_after(exc: TelegramAPIError, operation: str) -> Translated:
    seconds = float(exc.retry_after) if isinstance(exc, TelegramRetryAfter) else None
    return Translated(RateLimited(provider=PROVIDER, retry_after=seconds), "too_many_requests")


def _network(exc: TelegramAPIError, operation: str) -> Translated:
    if "timeout" in exc.message.lower():
        return Translated(CallTimedOut(operation=operation), "request_timeout", delivery_unknown=True)
    return Translated(ProviderUnavailable(provider=PROVIDER), "network_error", delivery_unknown=True)


def _client_error(exc: TelegramAPIError, operation: str) -> Translated:
    """400, 403, 404: Telegram refused the request itself. Never retried; resending the same request fails again.
    A text whose HTML did not parse is its own error: the outbox sends it once more as plain text."""
    reason = reason_of(exc.message)
    if reason == "file_too_big":
        return Translated(TelegramFileTooLarge(direction="in", limit_bytes=INBOUND_MAX_BYTES), reason)
    if reason == "parse_entities_failed":
        return Translated(MarkupRejected(provider=PROVIDER), reason)
    return _refused(reason)


def _unclassified(exc: TelegramAPIError, operation: str) -> Translated:
    """A status aiogram has no class for. A 429 without `parameters` still says "retry after N" in its text."""
    reason = reason_of(exc.message)
    if reason != "too_many_requests":
        return _refused(reason)
    match = _RETRY_AFTER_TEXT.search(exc.message)
    return Translated(RateLimited(provider=PROVIDER, retry_after=float(match.group(1)) if match else None), reason)


# Subclasses come before their bases: TelegramEntityTooLarge is a TelegramNetworkError, RestartingTelegram a
# TelegramServerError, and every class here a TelegramAPIError.
_BY_CLASS: Final[tuple[tuple[type[TelegramAPIError], Callable[[TelegramAPIError, str], Translated]], ...]] = (
    (TelegramRetryAfter, _retry_after),
    # The group became a supergroup with a new id: the configured chat id is stale until the owner updates it.
    (TelegramMigrateToChat, lambda _exc, _op: _refused("chat_migrated")),
    (TelegramUnauthorizedError, lambda _exc, _op: Translated(AuthFailed(provider=PROVIDER), "unauthorized")),
    (
        TelegramEntityTooLarge,
        lambda _exc, _op: Translated(
            TelegramFileTooLarge(direction="out", limit_bytes=OUTBOUND_MAX_BYTES), "entity_too_large"
        ),
    ),
    (TelegramNetworkError, _network),
    (TelegramServerError, lambda _exc, _op: Translated(ProviderUnavailable(provider=PROVIDER), "server_error")),
    (TelegramConflictError, lambda _exc, _op: _refused("conflict")),
    (TelegramBadRequest, _client_error),
    (TelegramForbiddenError, _client_error),
    (TelegramNotFound, _client_error),
)


def translate(exc: TelegramAPIError | ClientDecodeError, *, operation: str) -> Translated:
    """One aiogram exception → its `ExternalError`."""
    if isinstance(exc, ClientDecodeError):
        # Telegram answered, but not with JSON (a proxy's HTML 502): transient, and a send may have gone through.
        return Translated(ProviderUnavailable(provider=PROVIDER), "malformed_response", delivery_unknown=True)
    for kind, make in _BY_CLASS:
        if isinstance(exc, kind):
            return make(exc, operation)
    return _unclassified(exc, operation)


def translate_transport(exc: Exception, *, operation: str) -> Translated | None:
    """A failure while streaming a file from `api.telegram.org/file/…`.

    aiogram's `stream_content` lets aiohttp's own exceptions through, and aiohttp is confined to another adapter
    (C5), so its classes are recognised by module and by their `status` attribute. Anything else is not a transport
    failure and returns None, so the caller re-raises it unchanged.
    """
    if isinstance(exc, TimeoutError):
        return Translated(CallTimedOut(operation=operation), "stream_timeout")
    if type(exc).__module__.partition(".")[0] != "aiohttp":
        return None
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        return Translated(ProviderUnavailable(provider=PROVIDER), "stream_connection_error")
    if status == 429:
        return Translated(RateLimited(provider=PROVIDER), "too_many_requests")
    if status >= 500:
        return Translated(ProviderUnavailable(provider=PROVIDER), "server_error")
    # 4xx: the file path expired or the file is gone. Asking again will not help.
    return Translated(ProviderUnavailable(provider=PROVIDER, retryable=False), f"stream_http_{status}")
