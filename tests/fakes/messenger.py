"""`FakeMessenger`: records every request; message ids increase per chat; failures can be scripted.

The Messenger port has no `reply_markup` parameter, so any attempt to send buttons is a `TypeError` here and a mypy
error in `src/` (UC-12). `assert_no_reply_markup()` also scans recorded payloads, for adapters under test.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field

from yordamchi.core.text import BotText
from yordamchi.ports.messenger import ChatAction, Messenger, OutboundFile, ParseMode, SentMessage


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    chat_id: int
    payload: dict[str, object] = field(default_factory=dict)


class FakeMessenger:
    def __init__(self) -> None:
        self.requests: list[Request] = []
        self._next_id: defaultdict[int, int] = defaultdict(lambda: 100)
        self._failures: defaultdict[str, deque[Exception]] = defaultdict(deque)
        self.undeletable: set[tuple[int, int]] = set()
        self.files: dict[str, bytes] = {}

    # --- scripting ---
    def fail_next(self, method: str, error: Exception) -> None:
        self._failures[method].append(error)

    def sent(self, method: str = "send_text") -> list[Request]:
        return [request for request in self.requests if request.method == method]

    def assert_no_reply_markup(self) -> None:
        offenders = [request for request in self.requests if "reply_markup" in request.payload]
        assert not offenders, f"reply_markup sent: {offenders}"

    # --- internals ---
    def _record(self, method: str, chat_id: int, **payload: object) -> None:
        failures = self._failures[method]
        if failures:
            raise failures.popleft()
        self.requests.append(Request(method, chat_id, dict(payload)))

    def _new_message(self, chat_id: int) -> SentMessage:
        self._next_id[chat_id] += 1
        return SentMessage(chat_id=chat_id, message_id=self._next_id[chat_id])

    # --- Messenger ---
    async def send_text(
        self,
        chat_id: int,
        text: BotText,
        *,
        reply_to: int | None = None,
        parse_mode: ParseMode = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage:
        self._record(
            "send_text",
            chat_id,
            text=text,
            reply_to=reply_to,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            timeout=timeout,
        )
        return self._new_message(chat_id)

    async def edit_text(
        self, chat_id: int, message_id: int, text: BotText, *, parse_mode: ParseMode = None, timeout: float
    ) -> None:
        self._record("edit_text", chat_id, message_id=message_id, text=text, parse_mode=parse_mode, timeout=timeout)

    async def delete(self, chat_id: int, message_id: int, *, timeout: float) -> bool:
        self._record("delete", chat_id, message_id=message_id, timeout=timeout)
        return (chat_id, message_id) not in self.undeletable

    async def react(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        self._record("react", chat_id, message_id=message_id, emoji=emoji, timeout=timeout)

    async def mark_read(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        self._record("mark_read", chat_id, message_id=message_id, emoji=emoji, timeout=timeout)

    async def chat_action(self, chat_id: int, action: ChatAction, *, timeout: float) -> None:
        self._record("chat_action", chat_id, action=action, timeout=timeout)

    async def pin(self, chat_id: int, message_id: int, *, timeout: float) -> None:
        self._record("pin", chat_id, message_id=message_id, timeout=timeout)

    async def leave(self, chat_id: int, *, timeout: float) -> None:
        self._record("leave", chat_id, timeout=timeout)

    async def download(self, file_id: str, *, max_bytes: int, timeout: float) -> bytes:
        self._record("download", 0, file_id=file_id, max_bytes=max_bytes, timeout=timeout)
        data = self.files[file_id]
        if len(data) > max_bytes:
            msg = f"{file_id} is larger than {max_bytes} bytes"
            raise ValueError(msg)
        return data

    async def _send_file(self, method: str, chat_id: int, file: OutboundFile, **payload: object) -> SentMessage:
        self._record(method, chat_id, path=str(file.path), filename=file.filename, mime=file.mime, **payload)
        return self._new_message(chat_id)

    async def send_photo(
        self,
        chat_id: int,
        photo: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage:
        return await self._send_file("send_photo", chat_id, photo, caption=caption, reply_to=reply_to, timeout=timeout)

    async def send_document(
        self,
        chat_id: int,
        document: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage:
        return await self._send_file(
            "send_document", chat_id, document, caption=caption, reply_to=reply_to, timeout=timeout
        )

    async def send_voice(
        self,
        chat_id: int,
        voice: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        duration_s: int | None = None,
        timeout: float,
    ) -> SentMessage:
        return await self._send_file(
            "send_voice", chat_id, voice, caption=caption, reply_to=reply_to, duration_s=duration_s, timeout=timeout
        )

    async def send_video(
        self,
        chat_id: int,
        video: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage:
        return await self._send_file("send_video", chat_id, video, caption=caption, reply_to=reply_to, timeout=timeout)

    async def send_draft(self, chat_id: int, draft_id: int, text: BotText, *, timeout: float) -> None:
        self._record("send_draft", chat_id, draft_id=draft_id, text=text, timeout=timeout)


_: Messenger = FakeMessenger()
