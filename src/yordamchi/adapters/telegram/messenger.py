"""`TelegramMessenger`: the `Messenger` port on aiogram's `Bot` (CONVENTIONS §4, §10; SPEC §3.2.1).

- Text goes out only as `BotText`. No request ever carries a keyboard or a menu (D8, UC-12): the port has no such
  parameter, and the recording session in `tests/fakes/telegram_session.py` fails any request that carries one.
- Every call is bounded by its keyword-only `timeout` (A4). Inside it, `core.retry` retries retryable failures with
  backoff and honours a 429's `retry_after` (NFR-CAP-02). A wait that would pass the deadline re-raises
  `RateLimited`, so the outbox can park the chat for `retry_after` durably.
- A send whose delivery is unknown (network error or unreadable answer after the request left) is never resent here:
  the error goes up still `retryable`, and the outbox decides with its `dedupe_key` (SPEC §4.6, dup ≤ 2 %).
  Edits, deletes, reactions, chat actions, pins, leave, drafts and getFile are idempotent and are retried.
- Files: inbound ≤ 20 MB (getFile), outbound ≤ 50 MB, photos ≤ 10 MB (NFR-CAP-04), checked before any transfer.
- Forum topics (D4 revised 2026-10-26): every send and typing hint carries `message_thread_id` when the caller names a
  topic. `createForumTopic` is a send (never resent on an unknown outcome: it would open a second topic);
  `reopenForumTopic` is idempotent, and a topic that is open already (`TOPIC_NOT_MODIFIED`) counts as reopened.
- One log line per call: `telegram_call_finished` or `telegram_call_failed` with the API method, chat id,
  outcome, reason, attempts and duration. Never text, captions, file ids or the token.

The constructor takes the token and, for tests, an aiogram `BaseSession`. Nothing is created at import time.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.methods import (
    CreateForumTopic,
    DeleteMessage,
    EditMessageText,
    GetFile,
    LeaveChat,
    PinChatMessage,
    ReopenForumTopic,
    SendChatAction,
    SendDocument,
    SendMessage,
    SendMessageDraft,
    SendPhoto,
    SendVideo,
    SendVoice,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import FSInputFile, Message, ReactionTypeEmoji, ReplyParameters
from aiogram.utils.token import TokenValidationError

from yordamchi.adapters.telegram.translate import (
    INBOUND_MAX_BYTES,
    OUTBOUND_MAX_BYTES,
    OUTBOUND_PHOTO_MAX_BYTES,
    TelegramFileTooLarge,
    Translated,
    translate,
    translate_transport,
)
from yordamchi.core.errors import CallTimedOut, ConfigInvalid, ExternalError, RateLimited
from yordamchi.core.log import get_log
from yordamchi.core.retry import DEFAULT_ATTEMPTS, DEFAULT_BASE_DELAY_S, DEFAULT_MAX_DELAY_S, retrying
from yordamchi.core.text import BotText
from yordamchi.ports.messenger import ChatAction, ForumTopic, Messenger, OutboundFile, ParseMode, SentMessage

log = get_log(__name__)

DOWNLOAD_CHUNK_BYTES: Final = 64 * 1024

# Refusals that are a normal outcome, not an error.
_EDIT_UNCHANGED: Final = frozenset({"message_not_modified"})
_DELETE_REFUSED: Final = frozenset(
    {"message_not_found", "message_cant_be_deleted", "not_enough_rights", "bot_not_member", "chat_not_found"}
)
_ALREADY_LEFT: Final = frozenset({"bot_not_member", "chat_not_found"})
_ALREADY_OPEN: Final = frozenset({"topic_not_modified"})


@dataclass(frozen=True, slots=True)
class _Refused:
    reason: str


class _DeliveryUnknown(Exception):
    """Carries a translated error out of `retrying` without a retry: resending might post the message twice."""

    def __init__(self, translated: Translated) -> None:
        super().__init__(translated.reason)
        self.translated = translated


class TelegramMessenger:
    def __init__(self, token: str, *, session: BaseSession | None = None) -> None:
        try:
            self._bot = Bot(token, session=session)
        except TokenValidationError:
            raise ConfigInvalid(key="bot_token", problem="malformed") from None
        # Mechanism, not behaviour: the backoff of transient failures inside one call's own timeout.
        self.retry_attempts = DEFAULT_ATTEMPTS
        self.retry_base_delay_s = DEFAULT_BASE_DELAY_S
        self.retry_max_delay_s = DEFAULT_MAX_DELAY_S

    @property
    def bot(self) -> Bot:
        """The aiogram `Bot`, for the polling runner in `bots/` (one HTTP session per process)."""
        return self._bot

    async def close(self) -> None:
        await self._bot.session.close()

    # --- Messenger: text ---
    async def send_text(
        self,
        chat_id: int,
        text: BotText,
        *,
        reply_to: int | None = None,
        parse_mode: ParseMode = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        method = SendMessage(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            reply_parameters=_reply(reply_to),
        )
        return _sent(await self._call(method, timeout=timeout, idempotent=False))

    async def edit_text(
        self, chat_id: int, message_id: int, text: BotText, *, parse_mode: ParseMode = None, timeout: float
    ) -> None:
        method = EditMessageText(chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode)
        await self._call_or_refused(method, timeout=timeout, accept=_EDIT_UNCHANGED)

    async def delete(self, chat_id: int, message_id: int, *, timeout: float) -> bool:
        method = DeleteMessage(chat_id=chat_id, message_id=message_id)
        result = await self._call_or_refused(method, timeout=timeout, accept=_DELETE_REFUSED)
        return result is True

    async def react(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        method = SetMessageReaction(chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
        await self._call(method, timeout=timeout)

    async def mark_read(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        await self.react(chat_id, message_id, emoji, timeout=timeout)
        await self._call(SetMessageReaction(chat_id=chat_id, message_id=message_id, reaction=[]), timeout=timeout)

    async def chat_action(
        self, chat_id: int, action: ChatAction, *, thread_id: int | None = None, timeout: float
    ) -> None:
        method = SendChatAction(chat_id=chat_id, action=action.value, message_thread_id=thread_id)
        await self._call(method, timeout=timeout)

    async def pin(self, chat_id: int, message_id: int, *, timeout: float) -> None:
        # Silent: the heartbeat pin (SPEC §2.8) must not notify the group each time it moves.
        method = PinChatMessage(chat_id=chat_id, message_id=message_id, disable_notification=True)
        await self._call(method, timeout=timeout)

    async def leave(self, chat_id: int, *, timeout: float) -> None:
        # Idempotent: a chat the bot is no longer in counts as left (UC-01).
        await self._call_or_refused(LeaveChat(chat_id=chat_id), timeout=timeout, accept=_ALREADY_LEFT)

    async def send_draft(self, chat_id: int, draft_id: int, text: BotText, *, timeout: float) -> None:
        method = SendMessageDraft(chat_id=chat_id, draft_id=draft_id, text=text, parse_mode=None)
        await self._call(method, timeout=timeout)

    # --- Messenger: forum topics ---
    async def create_forum_topic(self, chat_id: int, name: str, *, icon_color: int, timeout: float) -> ForumTopic:
        method = CreateForumTopic(chat_id=chat_id, name=name, icon_color=icon_color)
        topic = await self._call(method, timeout=timeout, idempotent=False)
        return ForumTopic(thread_id=topic.message_thread_id, name=topic.name, icon_color=topic.icon_color)

    async def reopen_forum_topic(self, chat_id: int, thread_id: int, *, timeout: float) -> None:
        method = ReopenForumTopic(chat_id=chat_id, message_thread_id=thread_id)
        await self._call_or_refused(method, timeout=timeout, accept=_ALREADY_OPEN)

    # --- Messenger: files ---
    async def download(self, file_id: str, *, max_bytes: int, timeout: float) -> bytes:
        limit = min(max_bytes, INBOUND_MAX_BYTES)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        info = await self._call(GetFile(file_id=file_id), timeout=timeout)
        if info.file_size is not None and info.file_size > limit:
            raise TelegramFileTooLarge(direction="in", limit_bytes=limit, size_bytes=info.file_size)
        if not info.file_path:
            # Telegram leaves `file_path` out for files it will not serve to bots.
            raise TelegramFileTooLarge(direction="in", limit_bytes=limit, size_bytes=info.file_size)
        file_path = info.file_path
        trace = _Trace("file_download", None)

        async def fetch() -> bytes:
            return await self._fetch(file_path, limit=limit, trace=trace, timeout=deadline - loop.time())

        return await self._observed(trace, lambda: self._retrying(fetch, deadline - loop.time()))

    async def send_photo(
        self,
        chat_id: int,
        photo: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        method = SendPhoto(
            chat_id=chat_id,
            message_thread_id=thread_id,
            photo=await _upload(photo, limit=OUTBOUND_PHOTO_MAX_BYTES),
            caption=caption,
            parse_mode=None,
            disable_notification=disable_notification,
            reply_parameters=_reply(reply_to),
        )
        return _sent(await self._call(method, timeout=timeout, idempotent=False))

    async def send_document(
        self,
        chat_id: int,
        document: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        method = SendDocument(
            chat_id=chat_id,
            message_thread_id=thread_id,
            document=await _upload(document, limit=OUTBOUND_MAX_BYTES),
            caption=caption,
            parse_mode=None,
            disable_notification=disable_notification,
            reply_parameters=_reply(reply_to),
        )
        return _sent(await self._call(method, timeout=timeout, idempotent=False))

    async def send_voice(
        self,
        chat_id: int,
        voice: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        duration_s: int | None = None,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        method = SendVoice(
            chat_id=chat_id,
            message_thread_id=thread_id,
            voice=await _upload(voice, limit=OUTBOUND_MAX_BYTES),
            caption=caption,
            parse_mode=None,
            duration=duration_s,
            disable_notification=disable_notification,
            reply_parameters=_reply(reply_to),
        )
        return _sent(await self._call(method, timeout=timeout, idempotent=False))

    async def send_video(
        self,
        chat_id: int,
        video: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        method = SendVideo(
            chat_id=chat_id,
            message_thread_id=thread_id,
            video=await _upload(video, limit=OUTBOUND_MAX_BYTES),
            caption=caption,
            parse_mode=None,
            disable_notification=disable_notification,
            reply_parameters=_reply(reply_to),
        )
        return _sent(await self._call(method, timeout=timeout, idempotent=False))

    # --- internals ---
    async def _call[T](self, method: TelegramMethod[T], *, timeout: float, idempotent: bool = True) -> T:
        result = await self._call_or_refused(method, timeout=timeout, idempotent=idempotent)
        if isinstance(result, _Refused):  # pragma: no cover - `accept` is empty, so nothing is refused
            raise AssertionError(result.reason)
        return result

    async def _call_or_refused[T](
        self,
        method: TelegramMethod[T],
        *,
        timeout: float,
        idempotent: bool = True,
        accept: frozenset[str] = frozenset(),
    ) -> T | _Refused:
        """One Bot API call under `timeout`, retried while retryable; a refusal in `accept` returns `_Refused`."""
        api_method = method.__api_method__
        trace = _Trace(api_method, getattr(method, "chat_id", None))
        request_timeout = max(1, math.ceil(timeout))

        async def attempt() -> T | _Refused:
            trace.attempts += 1
            try:
                return await self._bot(method, request_timeout=request_timeout)
            except TimeoutError:
                # aiogram's own HTTP timeout; the call's budget surfaces as CancelledError here, not TimeoutError.
                translated = Translated(CallTimedOut(operation=api_method), "request_timeout", delivery_unknown=True)
            except (TelegramAPIError, ClientDecodeError) as exc:
                translated = translate(exc, operation=api_method)
            # Raised outside the `except` blocks: no vendor exception (and no URL with the token) is chained.
            trace.reason = translated.reason
            if translated.reason in accept:
                return _Refused(translated.reason)
            if translated.delivery_unknown and not idempotent:
                raise _DeliveryUnknown(translated)
            raise translated.error

        return await self._observed(trace, lambda: self._retrying(attempt, timeout))

    async def _retrying[T](self, fn: Callable[[], Awaitable[T]], timeout: float) -> T:
        return await retrying(
            fn,
            timeout=timeout,
            operation="telegram",
            attempts=self.retry_attempts,
            base_delay_s=self.retry_base_delay_s,
            max_delay_s=self.retry_max_delay_s,
        )

    async def _observed[T](self, trace: "_Trace", run: Callable[[], Awaitable[T]]) -> T:
        """Run one logical call (all its attempts) and write its single log line."""
        started = time.monotonic()
        try:
            result = await run()
        except _DeliveryUnknown as unknown:
            undelivered = unknown.translated.error
        except ExternalError as error:
            trace.failed(error, started)
            raise
        else:
            log.info(
                "telegram_call_finished",
                method=trace.api_method,
                chat_id=trace.chat_id,
                outcome=f"refused_{result.reason}" if isinstance(result, _Refused) else "ok",
                attempts=trace.attempts,
                duration_ms=_elapsed_ms(started),
            )
            return result
        # Outside the handler, so the internal carrier is not chained onto the error.
        trace.failed(undelivered, started)
        raise undelivered

    async def _fetch(self, file_path: str, *, limit: int, trace: "_Trace", timeout: float) -> bytes:
        trace.attempts += 1
        outcome = await self._stream(file_path, limit=limit, timeout=timeout)
        if isinstance(outcome, Translated):
            trace.reason = outcome.reason
            raise outcome.error
        return outcome

    async def _stream(self, file_path: str, *, limit: int, timeout: float) -> bytes | Translated:
        session = self._bot.session
        url = session.api.file_url(self._bot.token, file_path)
        stream = session.stream_content(
            url=url, timeout=max(1, math.ceil(timeout)), chunk_size=DOWNLOAD_CHUNK_BYTES, raise_for_status=True
        )
        data = bytearray()
        try:
            async for chunk in stream:
                data += chunk
                if len(data) > limit:
                    # No `file_size` in getFile, or a wrong one: stop at the limit instead of buffering it all.
                    return Translated(TelegramFileTooLarge(direction="in", limit_bytes=limit), "file_too_big")
        except Exception as exc:  # aiohttp's classes cannot be imported here (C5); see translate_transport()
            translated = translate_transport(exc, operation="file_download")
            if translated is None:
                raise
            return translated
        finally:
            await stream.aclose()
        return bytes(data)


@dataclass(slots=True)
class _Trace:
    """What one logical call did, for its log line."""

    api_method: str
    chat: object
    attempts: int = 0
    reason: str | None = None

    @property
    def chat_id(self) -> int | None:
        return self.chat if isinstance(self.chat, int) else None

    def failed(self, error: ExternalError, started: float) -> None:
        log.warning(
            "telegram_call_failed",
            method=self.api_method,
            chat_id=self.chat_id,
            outcome=error.code.value,
            reason=self.reason,
            retryable=error.retryable,
            retry_after=error.retry_after if isinstance(error, RateLimited) else None,
            attempts=self.attempts,
            duration_ms=_elapsed_ms(started),
        )


def _reply(reply_to: int | None) -> ReplyParameters | None:
    # A reply target that was deleted meanwhile must not fail the send.
    return None if reply_to is None else ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)


def _sent(message: Message) -> SentMessage:
    return SentMessage(chat_id=message.chat.id, message_id=message.message_id)


async def _upload(file: OutboundFile, *, limit: int) -> FSInputFile:
    """Refuse an oversized file before any byte is sent (NFR-CAP-04)."""
    size = (await asyncio.to_thread(file.path.stat)).st_size
    if size > limit:
        raise TelegramFileTooLarge(direction="out", limit_bytes=limit, size_bytes=size)
    return FSInputFile(file.path, filename=file.filename)


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


_: type[Messenger] = TelegramMessenger
