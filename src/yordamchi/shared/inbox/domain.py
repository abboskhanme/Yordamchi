"""Intake rules, pure (SPEC §3.2.1 step 3, §4.5, §4.6; UC-04, NFR-REL-05).

One Telegram update goes through intake in this order:
1. the handler turns the aiogram object into an `IncomingUpdate` (no SQL, no decisions);
2. `scan_secrets` finds every key in the text and caption, in memory only;
3. the service writes the `(bot, update_id)` idempotency key; an update that is already there is a no-op;
4. the vault stores each key, and `redacted_content` replaces it in the text: a stored key becomes
   `[kalit: <name> ••••abcd]`, a Claude login token `[rad etildi]` (refused, never stored), a key the vault could not
   take `[kalit saqlanmadi]`. Only that redacted, scrubbed content is written;
5. `plan_ack` decides what the owner sees: a message holding a key is deleted and a masked reply follows; any other
   message gets the "seen" reaction; typing shows while work for the chat is pending.

In a forum dev group (D4 revised 2026-10-26) an update also says which topic it is in (`thread_id`: None for General)
and whether the chat is a forum at all; the masked reply and the typing hint go to that topic. A service message (a
topic closed, reopened or renamed) is stored for the record and carries no request.

Later stages (burst, voice, routing) read only `StoredUpdate`s, which hold the redacted content.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import DomainError
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.core.redact import SecretHit
from yordamchi.core.text import BotText, join
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.topics import domain as topics
from yordamchi.shared.vault import domain as vault
from yordamchi.shared.vault.domain import NewSecret, SecretRef

PAYLOAD_KEEP: Final = timedelta(days=7)  # SPEC §4.2: "payload nulled 7 d"
ROW_KEEP: Final = timedelta(days=30)  # "row 30 d"
# Bound on each Telegram call of the ack. UC-04 wants a key message gone within 5 s, and the masked reply waits for
# the delete: 2 × 2.5 s. SPEC §4.3 has no key for it yet (`timeouts.ack_s` is proposed); T15's outbox takes over.
ACK_TIMEOUT_S: Final = 2.5


class UpdateKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    AUDIO = "audio"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    LINK = "link"  # a text that is only URLs: it waits like media (SPEC §4.4)
    EDITED = "edited"
    MEMBER = "member"  # my_chat_member in the owner's own chat
    SERVICE = "service"  # a service message in the owner's chat: a forum topic closed, reopened, renamed (no request)
    OTHER = "other"


class UpdateStatus(StrEnum):
    RECEIVED = "received"  # durable, not yet attached to a burst (re-attached after a restart, SPEC §2.7)
    ATTACHED = "attached"
    IGNORED = "ignored"  # carries no request (a membership change)
    FAILED = "failed"  # a key in it could not be stored; the owner was told to send it again


# Retention never touches a row that is still waiting for a burst.
TERMINAL: Final = frozenset({UpdateStatus.ATTACHED, UpdateStatus.IGNORED, UpdateStatus.FAILED})


class ForwardSource(StrEnum):
    """Where a forwarded message came from: Bot API `MessageOrigin.type`, or what the legacy `forward_*` fields say.

    A forwarded message is the owner's update (he forwarded it) but not his words: it is material, never a command
    (NFR-SEC-09). It never approves a decision, answers a question or a clarification, changes a setting, or stops or
    redirects a turn; it can still be the material of an instruction the owner gives in his own words.
    """

    USER = "user"
    HIDDEN_USER = "hidden_user"  # a user who hides their account when forwarded
    CHAT = "chat"  # a group, or an anonymous group admin
    CHANNEL = "channel"  # a channel post, or a linked channel's automatic forward
    UNKNOWN = "unknown"  # forwarded, but the origin cannot be read


_ORIGIN_TYPES: Final[Mapping[str, ForwardSource]] = {
    "user": ForwardSource.USER,
    "hidden_user": ForwardSource.HIDDEN_USER,
    "chat": ForwardSource.CHAT,
    "channel": ForwardSource.CHANNEL,
}


def forward_of(message: Mapping[str, object]) -> ForwardSource | None:  # noqa: PLR0911  # one per Bot API field
    """The Telegram -> `IncomingUpdate` translation of forward provenance: None for the owner's own message.

    `message` is the Bot API `Message` as JSON (`message.model_dump(mode="json")` in the handler). `forward_origin`
    (Bot API 7.0+) is read first, then the legacy fields an older payload carries: `forward_from`,
    `forward_sender_name`, `forward_from_chat`, `forward_date`, and `is_automatic_forward` (a linked channel's post).
    Anything that says it is forwarded but names no readable origin is UNKNOWN, and still forwarded.
    """
    origin = message.get("forward_origin")
    if isinstance(origin, Mapping):
        kind = origin.get("type")
        return _ORIGIN_TYPES.get(kind, ForwardSource.UNKNOWN) if isinstance(kind, str) else ForwardSource.UNKNOWN
    if origin is not None:
        return ForwardSource.UNKNOWN
    if message.get("forward_from") is not None:
        return ForwardSource.USER
    if message.get("forward_sender_name") is not None:
        return ForwardSource.HIDDEN_USER
    chat = message.get("forward_from_chat")
    if isinstance(chat, Mapping):
        return ForwardSource.CHANNEL if chat.get("type") == "channel" else ForwardSource.CHAT
    if message.get("is_automatic_forward") is True:
        return ForwardSource.CHANNEL
    if chat is not None or message.get("forward_date") is not None:
        return ForwardSource.UNKNOWN
    return None


@dataclass(frozen=True, slots=True)
class Attachment:
    file_id: str
    mime: str | None = None
    size_bytes: int | None = None
    duration_s: int | None = None
    file_name: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingUpdate:
    """One Telegram update, as the handler translated it. Content fields never show in a repr."""

    bot: BotName
    update_id: int
    chat_id: int
    kind: UpdateKind
    user_id: int | None = None
    message_id: int | None = None
    text: str | None = field(default=None, repr=False)
    caption: str | None = field(default=None, repr=False)
    attachment: Attachment | None = None
    media_group_id: str | None = None
    reply_to_message_id: int | None = None
    synthetic: bool = False  # the supervisor's self-test: there is no Telegram message to touch
    forward: ForwardSource | None = None  # `forward_of(message)`: set when the owner forwarded someone else's message
    # When the owner sent it, by Telegram (UTC, whole seconds): `date`, or `edit_date` for an edit. None for a
    # synthetic update. Telegram hands over what it held while the bot was away in one batch, so the receipt time
    # alone cannot tell messages sent minutes apart (the burst places by this one, SPEC §4.4).
    sent_at: datetime | None = None
    # Forum topics (D4 revised 2026-10-26): Telegram's `message_thread_id` and `is_topic_message`. Only a topic
    # message has a topic (`thread_id`): a reply in General carries a thread id too.
    message_thread_id: int | None = None
    is_topic_message: bool = False
    is_forum: bool | None = None  # the chat's `is_forum`, for a group message; None: not said (a private chat)
    topic_closed: bool | None = None  # a service message: the owner closed (True) or reopened (False) the topic

    @property
    def forwarded(self) -> bool:
        return self.forward is not None

    @property
    def thread_id(self) -> int | None:
        """The forum topic the message is in; None for General and for any chat that is no forum."""
        return topics.topic_of(self.message_thread_id, is_topic_message=self.is_topic_message)


@dataclass(frozen=True, slots=True)
class Content:
    """What `payload_json` holds: the redacted text and caption, the attachment, the vault ids it referred to, and
    where it was forwarded from (None: the owner's own words)."""

    text: str | None = field(default=None, repr=False)
    caption: str | None = field(default=None, repr=False)
    attachment: Attachment | None = None
    secret_ids: tuple[int, ...] = ()
    forward: ForwardSource | None = None

    @property
    def forwarded(self) -> bool:
        return self.forward is not None


@dataclass(frozen=True, slots=True)
class StoredUpdate:
    """An accepted update as later stages read it (`list_received_updates`): its content is already redacted."""

    update_id: int
    chat_id: int
    kind: UpdateKind
    status: UpdateStatus
    user_id: int | None
    message_id: int | None
    media_group_id: str | None
    reply_to_message_id: int | None
    synthetic: bool
    received_at: datetime
    content: Content
    sent_at: datetime | None = None  # `IncomingUpdate.sent_at`, kept in `payload_json`; None if it had none
    thread_id: int | None = None  # `IncomingUpdate.thread_id`, kept in `payload_json`; None: General


@dataclass(frozen=True, slots=True)
class Accepted:
    """What `accept_update` did. A duplicate did nothing, so it carries no status."""

    update_id: int
    duplicate: bool
    status: UpdateStatus | None = None
    secrets: tuple[SecretRef, ...] = ()
    refused: int = 0


# --- origin -------------------------------------------------------------------------------------------------------
def require_matching_origin(origin: OwnerOrigin, update: IncomingUpdate, bot: BotName) -> None:
    """The `OwnerOrigin` proves this very update came from the owner; anything else is a wiring bug. An object that
    is not an `OwnerOrigin` raises `TypeError`, one for another update `ValueError`."""
    require_owner_origin(origin)
    same = (
        origin.bot is bot
        and update.bot is bot
        and origin.update_id == update.update_id
        and origin.chat_id == update.chat_id
        and (update.user_id is None or origin.user_id == update.user_id)
    )
    if not same:
        msg = f"owner origin {origin.bot}:{origin.update_id} does not match update {update.bot}:{update.update_id}"
        raise ValueError(msg)


# --- kinds and status ---------------------------------------------------------------------------------------------
_LINKS_ONLY: Final = re.compile(r"\s*(?:https?://\S+\s*)+")


def kind_for_text(text: str) -> UpdateKind:
    """`link` for a text that is nothing but URLs, else `text`."""
    return UpdateKind.LINK if _LINKS_ONLY.fullmatch(text) else UpdateKind.TEXT


def initial_status(update: IncomingUpdate, *, vault_failed: bool) -> UpdateStatus:
    if update.kind in {UpdateKind.MEMBER, UpdateKind.SERVICE}:
        return UpdateStatus.IGNORED
    if vault_failed:
        return UpdateStatus.FAILED
    return UpdateStatus.RECEIVED


# --- secrets ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SecretScan:
    """The `core.redact` hits in the text and the caption, in text order. In memory only, never persisted."""

    text: tuple[SecretHit, ...] = ()
    caption: tuple[SecretHit, ...] = ()

    @property
    def hits(self) -> tuple[SecretHit, ...]:
        return self.text + self.caption

    @property
    def found_key(self) -> bool:
        """A key or a refused token is in the message: it must leave the chat."""
        return any(hit.vaultable or hit.refused for hit in self.hits)

    @property
    def refused(self) -> int:
        return sum(1 for hit in self.hits if hit.refused)

    def to_store(self) -> tuple[NewSecret, ...]:
        """Each distinct key for the vault, in order. A refused token is never among them."""
        seen: set[str] = set()
        found: list[NewSecret] = []
        for hit in self.hits:
            if hit.vaultable and not hit.refused and hit.value not in seen:
                seen.add(hit.value)
                found.append(NewSecret(kind=hit.kind, value=hit.value))
        return tuple(found)


def scan_secrets(update: IncomingUpdate) -> SecretScan:
    return SecretScan(
        text=tuple(redact.find_secrets(update.text or "")),
        caption=tuple(redact.find_secrets(update.caption or "")),
    )


def _marker(hit: SecretHit, stored: Mapping[str, SecretRef]) -> str:
    if hit.refused:
        return S.SECRET_REFUSED_MARKER()
    if not hit.vaultable:  # a token-like part of a bot URL: masked, not a key to keep
        return redact.MASK + vault.last4_of(hit.value, hit.kind)
    ref = stored.get(hit.value)
    if ref is None:
        return S.SECRET_NOT_STORED_MARKER()
    return S.SECRET_MARKER(name=ref.name, masked=ref.masked)


def redact_text(text: str | None, hits: Sequence[SecretHit], stored: Mapping[str, SecretRef]) -> str | None:
    """`text` with each hit (non-overlapping, in text order, as `find_secrets` returns them) replaced by its marker.
    `stored` maps a key's value to its vault entry; a vaultable hit missing from it was not stored."""
    if text is None:
        return None
    parts: list[str] = []
    cursor = 0
    for hit in hits:
        parts.append(text[cursor : hit.start])
        parts.append(_marker(hit, stored))
        cursor = hit.end
    parts.append(text[cursor:])
    return "".join(parts)


def redacted_content(update: IncomingUpdate, scan: SecretScan, stored: Mapping[str, SecretRef]) -> Content:
    return Content(
        text=redact_text(update.text, scan.text, stored),
        caption=redact_text(update.caption, scan.caption, stored),
        attachment=update.attachment,
        secret_ids=tuple(sorted({ref.id for ref in stored.values()})),
        forward=update.forward,
    )


# --- payload_json -------------------------------------------------------------------------------------------------
def payload_json(
    content: Content, scrub: Callable[[str], str], *, sent_at: datetime | None = None, thread_id: int | None = None
) -> JsonObject:
    """The stored form of `content`; every string passes `scrub` (SPEC §4.1 ✱: redacted before the write).

    `sent_at` (intake passes `IncomingUpdate.sent_at`) is stored beside the content as an ISO instant: an instant is
    not content, so it is not scrubbed, as `received_at` is not. It lives here rather than in a column because only
    a `received` row needs it, and retention never nulls the payload of one (`sent_at_of` reads it back). The forum
    topic (`thread_id`, an id) is kept the same way, for the same reason (`thread_of` reads it back).
    """
    attachment: JsonValue = None
    if content.attachment is not None:
        attachment = {
            "file_id": content.attachment.file_id,
            "mime": content.attachment.mime,
            "size_bytes": content.attachment.size_bytes,
            "duration_s": content.attachment.duration_s,
            "file_name": content.attachment.file_name,
        }
    secret_ids: list[JsonValue] = []
    secret_ids.extend(content.secret_ids)
    payload: JsonObject = {
        "text": content.text,
        "caption": content.caption,
        "attachment": attachment,
        "secret_ids": secret_ids,
    }
    if content.forward is not None:  # only when set: the owner's own messages keep the shape they always had
        payload["forward"] = content.forward.value
    stored = {key: json_value.map_strings(value, scrub) for key, value in payload.items()}
    if sent_at is not None:  # likewise: a synthetic update's payload has no send time
        stored["sent_at"] = sent_at.isoformat()
    if thread_id is not None:  # likewise: a message in General keeps the shape it always had
        stored["thread_id"] = thread_id
    return stored


def _str(raw: JsonValue) -> str | None:
    return raw if isinstance(raw, str) else None


def _int(raw: JsonValue) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def content_from_json(raw: JsonValue) -> Content:
    """The inverse of `payload_json`. A nulled payload (older than 7 days) or a malformed one reads as empty."""
    if not isinstance(raw, dict):
        return Content()
    attachment: Attachment | None = None
    stored_attachment = raw.get("attachment")
    if isinstance(stored_attachment, dict) and (file_id := _str(stored_attachment.get("file_id"))) is not None:
        attachment = Attachment(
            file_id=file_id,
            mime=_str(stored_attachment.get("mime")),
            size_bytes=_int(stored_attachment.get("size_bytes")),
            duration_s=_int(stored_attachment.get("duration_s")),
            file_name=_str(stored_attachment.get("file_name")),
        )
    ids = raw.get("secret_ids")
    secret_ids = tuple(item for item in ids if isinstance(item, int)) if isinstance(ids, list) else ()
    return Content(
        text=_str(raw.get("text")),
        caption=_str(raw.get("caption")),
        attachment=attachment,
        secret_ids=secret_ids,
        forward=_forward(raw.get("forward")),
    )


def sent_at_of(raw: JsonValue) -> datetime | None:
    """The send time `payload_json` holds, UTC-aware. None when there is none (a synthetic update, a row stored before
    send times were kept, a nulled payload) or it cannot be read: the burst then places by the receipt time."""
    text = _str(raw.get("sent_at")) if isinstance(raw, dict) else None
    if text is None:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def thread_of(raw: JsonValue) -> int | None:
    """The forum topic `payload_json` holds; None for General, an older row, a nulled payload or an unreadable one."""
    return _int(raw.get("thread_id")) if isinstance(raw, dict) else None


def _forward(raw: JsonValue) -> ForwardSource | None:
    """A stored forward source; anything unreadable that is there at all still counts as forwarded (fail closed)."""
    if raw is None:
        return None
    try:
        return ForwardSource(raw) if isinstance(raw, str) else ForwardSource.UNKNOWN
    except ValueError:
        return ForwardSource.UNKNOWN


# --- ack ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AckPlan:
    """What the owner sees right after intake (SPEC §3.2.1 step 3). The delete comes first, then the reply."""

    chat_id: int
    message_id: int | None
    delete_message: bool  # it held a key: remove it from the cloud chat
    react: bool  # the "seen" reaction; never on a message that is being deleted
    typing: bool  # work is pending for this chat
    notices: tuple[BotText, ...] = ()  # the masked reply, sent after the delete

    @property
    def replies(self) -> bool:
        """There is a delete and/or a reply to send (the reply may only say the delete failed)."""
        return bool(self.notices) or self.delete_message


def plan_ack(
    update: IncomingUpdate,
    scan: SecretScan,
    *,
    status: UpdateStatus,
    stored: Sequence[SecretRef],
    vault_error: DomainError | None,
) -> AckPlan:
    on_telegram = update.message_id is not None and not update.synthetic
    notices: list[BotText] = []
    distinct = list({ref.id: ref for ref in stored}.values())
    if distinct:
        notices.append(S.SECRETS_STORED(keys=", ".join(f"{ref.name} {ref.masked}" for ref in distinct)))
    if scan.refused:
        notices.append(S.CLAUDE_TOKEN_REFUSED())
    if vault_error is not None:
        notices.append(S.ERRORS[vault_error.code](**vault_error.params))
    return AckPlan(
        chat_id=update.chat_id,
        message_id=update.message_id,
        delete_message=on_telegram and scan.found_key,
        react=on_telegram and not scan.found_key and status is not UpdateStatus.IGNORED,
        typing=status is UpdateStatus.RECEIVED,
        notices=tuple(notices),
    )


def ack_reply(plan: AckPlan, *, deleted: bool) -> BotText | None:
    """The one reply after intake, or None. A key message that stayed in the chat adds "kalitni almashtiring"."""
    parts = list(plan.notices)
    if plan.delete_message and not deleted:
        parts.append(S.SECRET_NOT_DELETED())
    return join(parts) if parts else None


# --- polling offset and retention ---------------------------------------------------------------------------------
def next_offset(current: int, update_ids: Iterable[int]) -> int:
    """`max(update_id) + 1` over a fed batch, never below `current` (SPEC §3.2.1 step 1, §4.6)."""
    return max([current, *(update_id + 1 for update_id in update_ids)])


def payload_cutoff(now: datetime) -> datetime:
    """A finished update's payload is nulled once `received_at` is strictly older than this."""
    return now - PAYLOAD_KEEP


def row_cutoff(now: datetime) -> datetime:
    """A finished update's row is deleted once `received_at` is strictly older than this."""
    return now - ROW_KEEP
