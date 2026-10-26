"""Forum topics of the dev group, pure (D4 revised 2026-10-26; SPEC §3.2.16).

The dev group is a forum supergroup: General for talk, reports and routing, and one topic per project that the bot
opens itself on the project's first work. The owner never opens one by hand. This module decides:

1. what a project's topic is called and which of Telegram's six icon colours it gets: the same project always gets
   the same colour (a hash of its id, never Python's salted `hash()`);
2. what to do when a project needs its topic (`plan_topic`): use the open one, reopen a closed one, open a new one,
   or none, so the work goes on in General: the feature is off, the chat is no forum (Topics off: the flat group of
   before), or Telegram refused lately for want of the "Manage topics" right;
3. what to do when a send into a topic failed (`plan_repair`): a closed topic is reopened, a deleted one opened anew;
   a thread no project owns (a topic the owner made himself) sends to General instead;
4. which topic a message is in (`topic_of`). Telegram also sets `message_thread_id` on a reply in General, so only a
   topic message (`is_topic_message`) has a topic;
5. the `https://t.me/c/<chat>/<topic>` link of General's pointer line, for a `-100…` supergroup id.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

# Bot API createForumTopic: "Color of the topic icon in RGB format. Currently, must be one of 7322096 (0x6FB9F0),
# 16766590 (0xFFD67E), 13338331 (0xCB86DB), 9367192 (0x8EEE98), 16749490 (0xFF93B2), or 16478047 (0xFB6F5F)".
ICON_COLORS: Final = (0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F)
NAME_MAX: Final = 128  # createForumTopic: "Topic name, 1-128 characters"
_SUPERGROUP_PREFIX: Final = "-100"
LINK_BASE: Final = "https://t.me/c"


class Refusal(StrEnum):
    """Why Telegram would open no topic in a chat (`TopicsUnavailable.reason`)."""

    NOT_FORUM = "not_forum"  # Topics are off in the group
    NO_RIGHT = "no_right"  # the bot lacks the "Manage topics" admin right


class Step(StrEnum):
    USE = "use"  # the project's open topic
    REOPEN = "reopen"  # its topic is closed: reopen it, then use it
    CREATE = "create"  # it has none (or it is gone): open one
    NONE = "none"  # no topic: the work goes on in General


@dataclass(frozen=True, slots=True)
class KnownTopic:
    """A project's topic as the bot last knew it. `closed_at`: the owner closed it, or the bot set it aside
    (`topics.enabled` off): nothing is posted there until it is reopened."""

    project_id: str
    chat_id: int
    thread_id: int
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True, slots=True)
class ChatTopics:
    """What the bot knows of a chat's topics: whether it is a forum (the owner's latest message there says), and
    when and why Telegram last refused to open a topic in it."""

    chat_id: int
    is_forum: bool
    refused_at: datetime | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True, slots=True)
class TopicPlan:
    step: Step
    thread_id: int | None = None  # USE and REOPEN: the topic
    reason: str = ""  # NONE: why (snake_case, for the log)


def topic_name(project_id: str) -> str:
    """The topic is named after the project: its folder name, as the owner reads it everywhere else."""
    return project_id[:NAME_MAX]


def icon_color_of(project_id: str) -> int:
    """One of Telegram's six icon colours, the same for a project every time (also after a restart)."""
    digest = hashlib.sha256(project_id.encode("utf-8")).digest()
    return ICON_COLORS[int.from_bytes(digest[:4], "big") % len(ICON_COLORS)]


def topic_of(message_thread_id: int | None, *, is_topic_message: bool) -> int | None:
    """The forum topic a message is in; None for General, for a reply chain in General, and for any other chat."""
    return message_thread_id if is_topic_message else None


def topic_link(chat_id: int, thread_id: int) -> str | None:
    """`https://t.me/c/<id>/<topic>` for a supergroup (`-100<id>`); None for an id with no such link."""
    text = str(chat_id)
    internal = text.removeprefix(_SUPERGROUP_PREFIX)
    if internal == text or not internal.isdigit():
        return None
    return f"{LINK_BASE}/{internal}/{thread_id}"


def refused_lately(chat: ChatTopics, *, now: datetime, retry_after: timedelta) -> bool:
    """Telegram refused to open a topic here for want of the right less than `retry_after` ago: do not ask again
    yet (`topics.retry_after_min`). A refusal because Topics were off ends when the chat is a forum again."""
    if chat.refused_at is None or chat.refusal is not Refusal.NO_RIGHT:
        return False
    return now - chat.refused_at < retry_after


def plan_topic(
    topic: KnownTopic | None,
    chat: ChatTopics | None,
    *,
    chat_id: int,
    enabled: bool,
    now: datetime,
    retry_after: timedelta,
) -> TopicPlan:
    """What a project needs for its work to go to its own topic in `chat_id` (module docstring, rule 2). A topic of
    another chat (the dev group changed) counts as none."""
    if not enabled:
        return TopicPlan(Step.NONE, reason="disabled")
    if chat is None or not chat.is_forum:
        return TopicPlan(Step.NONE, reason="not_forum")
    if refused_lately(chat, now=now, retry_after=retry_after):
        return TopicPlan(Step.NONE, reason="refused")
    if topic is not None and topic.chat_id == chat_id:
        if topic.is_open:
            return TopicPlan(Step.USE, topic.thread_id)
        return TopicPlan(Step.REOPEN, topic.thread_id)
    return TopicPlan(Step.CREATE)


def plan_repair(topic: KnownTopic | None, *, closed: bool) -> TopicPlan:
    """A send into a topic failed: Telegram said it is closed (`closed`) or gone (module docstring, rule 3)."""
    if topic is None:
        return TopicPlan(Step.NONE, reason="unknown_thread")
    if closed:
        return TopicPlan(Step.REOPEN, topic.thread_id)
    return TopicPlan(Step.CREATE)
