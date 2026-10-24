"""Owner admission, pure (SPEC §3.2.1 step 2; UC-01, D12, NFR-SEC-01).

Both bots answer exactly one person. `admit(arrival, policy)` decides every update before anything else sees it, by
these rules in this order:
1. **Leave**: the update comes from a group, supergroup or channel that is not one of this bot's chats, whoever sent
   it (the owner included): the bot must not stay there. A `my_chat_member` saying the bot is already out of such a
   chat is a **Drop** instead, since there is nothing left to leave.
2. **Drop** (silently: no reply, no reaction, no row), in turn: anyone but the owner, including an update with no
   sender; any update kind the bots never act on (callbacks, inline queries, channel posts, reactions, …): there are
   no buttons, so a callback is never theirs; the owner in a chat that is not this bot's (the dev bot's DM, say).
3. **Admit**: the owner, in one of this bot's chats, with a message, an edit or a membership change. The verdict
   carries the `OwnerOrigin` minted for that very update.

This bot's chats: the dev bot serves only `access.dev_group_id`; the personal bot the owner's DM plus
`access.personal_group_id` when it is set (0 = none). The owner is `access.owner_id`. All three start as the bootstrap
values and are read from the settings snapshot, so a change applies to the next update (SPEC §4.3).

`OwnerOrigin` is the proof that an action comes from a verified owner update. A settings change, an approval and a
decision binding all require one, so executor output, tool results and web content can never change a setting or
approve a decision (CONVENTIONS §5, A5: `OwnerOrigin(` is called only inside `shared/access/`).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from yordamchi.core.paths import BotName
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot

# UC-01: the bot leaves a foreign group within 10 s of the update. The leave call gets 8 s (the adapter retries inside
# it); the rest is margin for the settings read before it. A direct call, never the outbox: nothing is posted there.
LEAVE_TIMEOUT_S: Final = 8.0


@dataclass(frozen=True, slots=True)
class OwnerOrigin:
    bot: BotName
    update_id: int
    user_id: int
    chat_id: int


def require_owner_origin(origin: object, bot: BotName | None = None) -> OwnerOrigin:
    """`origin` as the proof that a call acts on a verified owner update of `bot`, checked at runtime too.

    The type annotation is not enough: an object shaped like an origin (`.bot`, `.update_id`, …) is what untrusted
    content or a wiring bug could at most build, and it must change nothing. Raises `TypeError` for anything that is
    not an `OwnerOrigin` minted here, `ValueError` for an origin of the other bot (`bot=None`: a caller that serves
    whichever bot the origin names); both before anything is read or written. Every public entry that takes an
    `origin` calls this first.
    """
    if not isinstance(origin, OwnerOrigin):
        msg = f"this needs the OwnerOrigin of a verified owner update, not a {type(origin).__name__}"
        raise TypeError(msg)
    if bot is not None and origin.bot is not bot:
        msg = f"an owner origin of the {origin.bot} bot cannot change or answer anything on the {bot} bot"
        raise ValueError(msg)
    return origin


class EventType(StrEnum):
    """The update kinds the policy tells apart; every other kind is `OTHER` and never served."""

    MESSAGE = "message"
    EDITED_MESSAGE = "edited_message"
    MY_CHAT_MEMBER = "my_chat_member"
    OTHER = "other"


SERVED: Final = frozenset({EventType.MESSAGE, EventType.EDITED_MESSAGE, EventType.MY_CHAT_MEMBER})


class ChatKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


# Chats a bot can be a member of, and so can leave. A private chat cannot be left; it is only ever dropped.
LEAVABLE: Final = frozenset({ChatKind.GROUP, ChatKind.SUPERGROUP, ChatKind.CHANNEL})

# `ChatMember.status` values that mean "in the chat". `restricted` counts only with `is_member` (Bot API).
_PRESENT_STATUSES: Final = frozenset({"creator", "administrator", "member"})


def is_present(status: str, *, is_member: bool = False) -> bool:
    """Whether a `ChatMember` status puts the bot in the chat (`left` and `kicked` do not)."""
    return status in _PRESENT_STATUSES or (status == "restricted" and is_member)


@dataclass(frozen=True, slots=True)
class Arrival:
    """Who sent one update, and where — all the policy needs. Ids only; content never reaches this module.

    `chat_kind` is None for a chat type this code does not know. `bot_present` matters only for `my_chat_member`:
    whether the bot is still in that chat after the change.
    """

    bot: BotName
    update_id: int
    event: EventType
    user_id: int | None
    chat_id: int | None
    chat_kind: ChatKind | None
    bot_present: bool = True


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """One bot's admission rule as of one settings snapshot."""

    bot: BotName
    owner_id: int
    chat_ids: frozenset[int]  # every chat this bot serves
    home_chat_id: int  # where jobs tell the owner something: the dev group, or the owner's DM


def policy_for(snap: Snapshot) -> AccessPolicy:
    owner_id = snap[defaults.ACCESS_OWNER_ID]
    if snap.bot is BotName.DEV:
        group_id = snap[defaults.ACCESS_DEV_GROUP_ID]
        return AccessPolicy(bot=snap.bot, owner_id=owner_id, chat_ids=frozenset({group_id}), home_chat_id=group_id)
    personal_group = snap[defaults.ACCESS_PERSONAL_GROUP_ID]  # 0 = no group
    chats = frozenset({owner_id, personal_group} if personal_group else {owner_id})
    return AccessPolicy(bot=snap.bot, owner_id=owner_id, chat_ids=chats, home_chat_id=owner_id)


class DropReason(StrEnum):
    STRANGER = "stranger"  # not the owner (no sender at all counts too: channel posts, anonymous admins)
    NOT_SERVED = "not_served"  # an update kind the bots never act on
    OWNER_ELSEWHERE = "owner_elsewhere"  # the owner, but not in one of this bot's chats
    ALREADY_OUT = "already_out"  # the bot was removed from a foreign chat: nothing to leave


@dataclass(frozen=True, slots=True)
class Admit:
    origin: OwnerOrigin


@dataclass(frozen=True, slots=True)
class Drop:
    reason: DropReason


@dataclass(frozen=True, slots=True)
class Leave:
    chat_id: int


type Verdict = Admit | Drop | Leave


def admit(arrival: Arrival, policy: AccessPolicy) -> Verdict:
    """The verdict on one update (UC-01). The rules are checked in the order the module docstring gives."""
    if arrival.bot is not policy.bot:
        msg = f"a {arrival.bot} update cannot be judged by the {policy.bot} policy"
        raise ValueError(msg)
    chat_id = arrival.chat_id
    ours = chat_id is not None and chat_id in policy.chat_ids
    if chat_id is not None and not ours and arrival.chat_kind in LEAVABLE:
        if arrival.event is EventType.MY_CHAT_MEMBER and not arrival.bot_present:
            return Drop(DropReason.ALREADY_OUT)
        return Leave(chat_id=chat_id)
    if arrival.user_id is None or arrival.user_id != policy.owner_id:
        return Drop(DropReason.STRANGER)
    if arrival.event not in SERVED:
        return Drop(DropReason.NOT_SERVED)
    if chat_id is None or not ours:
        return Drop(DropReason.OWNER_ELSEWHERE)
    origin = OwnerOrigin(bot=policy.bot, update_id=arrival.update_id, user_id=arrival.user_id, chat_id=chat_id)
    return Admit(origin=origin)
