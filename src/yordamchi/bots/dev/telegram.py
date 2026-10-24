"""The dev bot's aiogram handlers: translate one admitted update, persist it through the inbox, return (A7).

`OwnerOnly` (`bots/common.py`) runs first, so every handler here gets the update's `OwnerOrigin`; a stranger's update
never reaches this module. Each handler builds an `IncomingUpdate` and awaits exactly one service call,
`inbox.service.accept_update`. What happens next is the burst loop's business: `intake.accepted(…)` only wakes it
(and starts the speech-key check when the message held one), without awaiting anything. Ordering comes from
`update_id`, never from handler scheduling (CONVENTIONS §4).

There is no command handler: `/holat` and the other typed aliases are text like any other, read by
`bots/dev/intents.py` once their burst closes. Nothing here ever sends a keyboard or a menu (D8).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.types import ChatMemberUpdated, Message

from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.inbox import domain as inbox_domain
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import Attachment, IncomingUpdate, UpdateKind

if TYPE_CHECKING:
    from yordamchi.bots.dev.runtime import Intake


# --- translation ----------------------------------------------------------------------------------------------------
def _attachment(message: Message) -> tuple[UpdateKind, Attachment] | None:  # noqa: PLR0911  # one per file kind
    """The one file a message carries, as the inbox stores it (ids and sizes, never the bytes)."""
    if (voice := message.voice) is not None:
        return UpdateKind.VOICE, Attachment(voice.file_id, voice.mime_type, voice.file_size, voice.duration)
    if (audio := message.audio) is not None:
        found = Attachment(audio.file_id, audio.mime_type, audio.file_size, audio.duration, audio.file_name)
        return UpdateKind.AUDIO, found
    if message.photo:
        largest = max(message.photo, key=lambda size: (size.width * size.height, size.file_size or 0))
        return UpdateKind.PHOTO, Attachment(largest.file_id, "image/jpeg", largest.file_size)
    if (video := message.video) is not None:
        found = Attachment(video.file_id, video.mime_type, video.file_size, video.duration, video.file_name)
        return UpdateKind.VIDEO, found
    if (note := message.video_note) is not None:
        return UpdateKind.VIDEO, Attachment(note.file_id, "video/mp4", note.file_size, note.duration)
    if (animation := message.animation) is not None:
        found = Attachment(
            animation.file_id, animation.mime_type, animation.file_size, animation.duration, animation.file_name
        )
        return UpdateKind.VIDEO, found
    if (document := message.document) is not None:
        found = Attachment(document.file_id, document.mime_type, document.file_size, None, document.file_name)
        return UpdateKind.DOCUMENT, found
    return None


def _sent_at(message: Message, *, edited: bool) -> datetime:
    """When the owner sent it, by Telegram's clock (whole seconds): an edit's `edit_date`, else `date`. aiogram gives
    `date` as an aware datetime and `edit_date` as Unix seconds."""
    if edited and message.edit_date is not None:
        return datetime.fromtimestamp(message.edit_date, tz=UTC)
    return message.date.astimezone(UTC)


def incoming_of(origin: OwnerOrigin, message: Message, *, edited: bool = False) -> IncomingUpdate:
    """One admitted message (or its edit) as the inbox takes it: kind, text, caption, attachment, album, reply, when
    the owner sent it (`_sent_at`), and where it was forwarded from (`inbox.domain.forward_of`: someone else's words
    are material, never a command). A message with neither words nor a file we read (a sticker, a location) is
    `other`. Anything but the dev bot's minted `OwnerOrigin` raises before anything is read (`require_owner_origin`)."""
    origin = require_owner_origin(origin, BotName.DEV)
    found = _attachment(message)
    text = message.text
    if edited:
        kind = UpdateKind.EDITED
    elif found is not None:
        kind = found[0]
    elif text is not None:
        kind = inbox_domain.kind_for_text(text)
    else:
        kind = UpdateKind.OTHER
    reply = message.reply_to_message
    return IncomingUpdate(
        bot=origin.bot,
        update_id=origin.update_id,
        chat_id=message.chat.id,
        kind=kind,
        user_id=origin.user_id,
        message_id=message.message_id,
        text=text,
        caption=message.caption,
        attachment=None if found is None else found[1],
        media_group_id=message.media_group_id,
        reply_to_message_id=None if reply is None else reply.message_id,
        forward=inbox_domain.forward_of(message.model_dump(mode="json")),
        sent_at=_sent_at(message, edited=edited),
    )


def member_of(origin: OwnerOrigin, change: ChatMemberUpdated) -> IncomingUpdate:
    """The bot's membership changed in the owner's own chat: stored for the record, it carries no request."""
    origin = require_owner_origin(origin, BotName.DEV)
    return IncomingUpdate(
        bot=origin.bot,
        update_id=origin.update_id,
        chat_id=change.chat.id,
        kind=UpdateKind.MEMBER,
        user_id=origin.user_id,
    )


# --- handlers -------------------------------------------------------------------------------------------------------
def build_router() -> Router:
    """The dev bot's handlers. A new router per dispatcher: aiogram attaches a router to one parent only."""
    router = Router(name="dev")

    # Closures, not entries: aiogram calls them with the `origin` `OwnerOnly` minted (`data["origin"]`).
    @router.message()
    async def _on_message(message: Message, origin: OwnerOrigin, deps: DevDeps, intake: "Intake") -> None:
        intake.accepted(origin, await inbox.accept_update(deps, origin, incoming_of(origin, message)))

    @router.edited_message()
    async def _on_edit(message: Message, origin: OwnerOrigin, deps: DevDeps, intake: "Intake") -> None:
        intake.accepted(origin, await inbox.accept_update(deps, origin, incoming_of(origin, message, edited=True)))

    @router.my_chat_member()
    async def _on_member(change: ChatMemberUpdated, origin: OwnerOrigin, deps: DevDeps, intake: "Intake") -> None:
        intake.accepted(origin, await inbox.accept_update(deps, origin, member_of(origin, change)))

    return router
