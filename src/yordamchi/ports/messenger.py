"""Messenger: everything the bots send to Telegram (CONVENTIONS §4, §10).

It accepts only `BotText` (catalog entries or scrubbed model output). No method takes a keyboard or a markup
parameter: no inline buttons, reply keyboards or menus, ever (D8, UC-12). Polling is not here: `bots/common.py`
feeds updates to aiogram.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from yordamchi.core.text import BotText

type ParseMode = Literal["HTML"] | None


class ChatAction(StrEnum):
    TYPING = "typing"
    UPLOAD_PHOTO = "upload_photo"
    UPLOAD_DOCUMENT = "upload_document"
    UPLOAD_VIDEO = "upload_video"
    RECORD_VOICE = "record_voice"
    UPLOAD_VOICE = "upload_voice"


@dataclass(frozen=True, slots=True)
class SentMessage:
    chat_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class OutboundFile:
    path: Path
    filename: str | None = None
    mime: str | None = None


class Messenger(Protocol):
    async def send_text(
        self,
        chat_id: int,
        text: BotText,
        *,
        reply_to: int | None = None,
        parse_mode: ParseMode = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage: ...

    async def edit_text(
        self, chat_id: int, message_id: int, text: BotText, *, parse_mode: ParseMode = None, timeout: float
    ) -> None: ...

    async def delete(self, chat_id: int, message_id: int, *, timeout: float) -> bool:
        """False when Telegram refuses (older than 48 h, or no delete right)."""
        ...

    async def react(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None: ...

    async def mark_read(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        """Leave the message read (✓✓) with nothing visible: set `emoji` as a reaction, then remove it. The Bot API
        has no read method for a bot in a group (`readBusinessMessage` is for business accounts only)."""
        ...

    async def chat_action(self, chat_id: int, action: ChatAction, *, timeout: float) -> None: ...

    async def pin(self, chat_id: int, message_id: int, *, timeout: float) -> None: ...

    async def leave(self, chat_id: int, *, timeout: float) -> None: ...

    async def download(self, file_id: str, *, max_bytes: int, timeout: float) -> bytes:
        """At most `max_bytes` (Telegram serves ≤ 20 MB to bots)."""
        ...

    async def send_photo(
        self,
        chat_id: int,
        photo: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage: ...

    async def send_document(
        self,
        chat_id: int,
        document: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage: ...

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
    ) -> SentMessage: ...

    async def send_video(
        self,
        chat_id: int,
        video: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        timeout: float,
    ) -> SentMessage: ...

    async def send_draft(self, chat_id: int, draft_id: int, text: BotText, *, timeout: float) -> None:
        """`sendMessageDraft`: a streamed preview in a private chat (ephemeral, then `send_text`)."""
        ...
