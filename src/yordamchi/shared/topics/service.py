"""Forum topics of the dev group (D4 revised 2026-10-26; SPEC §3.2.16): one topic per project, opened by the bot.

Who calls what:
- intake (`shared/inbox`, inside its unit of work): `note_chat`, whether the chat is a forum as each owner message
  says, and `note_topic_event`, the owner closed or reopened a topic;
- the outbox: `thread_of_project` (`s` first) when it writes a row about a project, which then goes to that
  project's open topic; `repair_thread` when a send into a topic failed (reopen it, or open a new one when Telegram
  says it is gone);
- `dev/turns` and the features that queue work, before the work's first message: `ensure_topic`, the project's topic,
  opened on its first work (never for every project at once);
- the router: `project_of_thread`, so a message in a project's topic goes to that project;
- `set_topics_aside`: `topics.enabled` is off, so the bot posts nothing more in its topics (a project's next work
  reopens its topic once the setting is on again).

This module sends no message: it sits below the outbox, which reads it. A caller that must tell the owner something
(`Ensured.refused`) queues that line itself. Telegram is called outside every unit of work (A3), within the caller's
`timeout`. Logs carry ids, codes and counts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from yordamchi.core.errors import ExternalError, TopicGone, TopicsUnavailable
from yordamchi.core.log import get_log
from yordamchi.persistence.uow import Session
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.topics import domain, store
from yordamchi.shared.topics.domain import ChatTopics, KnownTopic, Refusal, Step
from yordamchi.shared.topics.models import ProjectTopic, TopicChat

log = get_log(__name__)


@dataclass(frozen=True, slots=True)
class Ensured:
    """`ensure_topic`'s answer: the topic to post the project's work in (None: General), and whether Telegram has
    just refused for want of the "Manage topics" right for the first time (the caller tells the owner once)."""

    thread_id: int | None
    refused: bool = False


def _known(row: ProjectTopic | None) -> KnownTopic | None:
    if row is None:
        return None
    return KnownTopic(project_id=row.project_id, chat_id=row.chat_id, thread_id=row.thread_id, closed_at=row.closed_at)


def _chat(row: TopicChat | None) -> ChatTopics | None:
    if row is None:
        return None
    refusal = Refusal(row.refusal) if row.refusal in {member.value for member in Refusal} else None
    return ChatTopics(chat_id=row.chat_id, is_forum=row.is_forum, refused_at=row.refused_at, refusal=refusal)


# --- intake (in the inbox's unit of work) -------------------------------------------------------------------------
async def note_chat(s: Session, *, chat_id: int, is_forum: bool, at: datetime) -> None:
    """The owner's message says whether the chat is a forum (Telegram's `chat.is_forum`)."""
    if await store.set_chat_forum(s, chat_id=chat_id, is_forum=is_forum, at=at):
        log.info("topic_chat_noted", chat_id=chat_id, is_forum=is_forum)


async def note_topic_event(s: Session, *, chat_id: int, thread_id: int, closed: bool, at: datetime) -> None:
    """The owner closed (`closed`) or reopened a topic: a closed project topic takes nothing until its project's
    next work reopens it."""
    changed = await store.mark_topic_closed(s, chat_id=chat_id, thread_id=thread_id, closed_at=at if closed else None)
    if changed:
        log.info("topic_marked", chat_id=chat_id, thread_id=thread_id, closed=closed)


# --- reads --------------------------------------------------------------------------------------------------------
async def thread_of_project(s: Session, *, chat_id: int, project_id: str) -> int | None:
    """In the caller's unit of work (the outbox writing a row about `project_id`): the project's open topic in a
    chat that is a forum, or None (General)."""
    row = await store.get_project_topic(s, project_id)
    if row is None or row.chat_id != chat_id or row.closed_at is not None:
        return None
    chat = await store.get_topic_chat(s, chat_id)
    return row.thread_id if chat is not None and chat.is_forum else None


async def find_project_thread(deps: SharedDeps, *, chat_id: int, project_id: str) -> int | None:
    """`thread_of_project` on its own read, for a sender outside the outbox (`dev/gate` posts its questions itself)."""
    async with deps.db.read() as s:
        return await thread_of_project(s, chat_id=chat_id, project_id=project_id)


async def project_of_thread(deps: SharedDeps, *, chat_id: int, thread_id: int) -> str | None:
    """The project whose topic `thread_id` is; None for a topic the bot did not open (the owner's own)."""
    async with deps.db.read() as s:
        row = await store.find_topic_by_thread(s, chat_id=chat_id, thread_id=thread_id)
    return None if row is None else row.project_id


# --- opening, reopening, repairing ----------------------------------------------------------------------------------
async def ensure_topic(
    deps: SharedDeps, *, chat_id: int, project_id: str, enabled: bool, retry_after_s: float, timeout: float
) -> Ensured:
    """The project's topic for its work: the open one, else reopened, else opened now (`domain.plan_topic`). None
    when there is none to be had (off, no forum, refused lately) or Telegram did not answer: the work goes to
    General and never waits for a topic."""
    now = deps.clock.now()
    async with deps.db.read() as s:
        topic = _known(await store.get_project_topic(s, project_id))
        chat = _chat(await store.get_topic_chat(s, chat_id))
    plan = domain.plan_topic(
        topic, chat, chat_id=chat_id, enabled=enabled, now=now, retry_after=timedelta(seconds=retry_after_s)
    )
    if plan.step is Step.USE or plan.step is Step.NONE:
        return Ensured(plan.thread_id)
    try:
        if plan.step is Step.REOPEN and plan.thread_id is not None:
            reopened = await _reopen(deps, chat_id=chat_id, thread_id=plan.thread_id, timeout=timeout)
            if reopened is not None:
                return Ensured(reopened)
        return Ensured(await _open(deps, chat_id=chat_id, project_id=project_id, timeout=timeout))
    except TopicsUnavailable as error:
        return Ensured(None, refused=await _refused(deps, chat_id=chat_id, error=error, chat=chat))
    except ExternalError as error:
        log.warning("topic_unavailable_now", project=project_id, chat_id=chat_id, error_code=error.code.value)
        return Ensured(None)


async def repair_thread(deps: SharedDeps, *, chat_id: int, thread_id: int, closed: bool, timeout: float) -> int | None:
    """A send into `thread_id` failed: Telegram said the topic is closed (`closed`) or gone. Returns the topic to send
    to now: the same one reopened, or the project's new one; None (General) for a thread no project owns, or when
    Telegram will open no topic here. Any other Telegram failure propagates: the sender retries the row later."""
    async with deps.db.read() as s:
        topic = _known(await store.find_topic_by_thread(s, chat_id=chat_id, thread_id=thread_id))
        chat = _chat(await store.get_topic_chat(s, chat_id))
    plan = domain.plan_repair(topic, closed=closed)
    if topic is None or plan.step is Step.NONE:
        return None
    try:
        if plan.step is Step.REOPEN:
            reopened = await _reopen(deps, chat_id=chat_id, thread_id=thread_id, timeout=timeout)
            if reopened is not None:
                return reopened
        return await _open(deps, chat_id=chat_id, project_id=topic.project_id, timeout=timeout, replaces=thread_id)
    except TopicsUnavailable as error:
        await _refused(deps, chat_id=chat_id, error=error, chat=chat)
        async with deps.db.uow() as s:
            if closed:  # the topic stays; nothing goes there until it can be reopened
                await store.mark_topic_closed(s, chat_id=chat_id, thread_id=thread_id, closed_at=deps.clock.now())
            else:
                await store.delete_project_topic(s, project_id=topic.project_id, thread_id=thread_id)
        return None


async def set_topics_aside(deps: SharedDeps, *, chat_id: int) -> int:
    """`topics.enabled` is off: every open topic of the chat is set aside (`closed_at`), so the bot posts nothing
    more there and the work of its projects goes to General. Returns how many."""
    async with deps.db.uow() as s:
        count = await store.mark_open_topics_closed(s, chat_id=chat_id, at=deps.clock.now())
    if count:
        log.info("topics_set_aside", chat_id=chat_id, count=count)
    return count


async def _reopen(deps: SharedDeps, *, chat_id: int, thread_id: int, timeout: float) -> int | None:
    """Reopen a closed topic; None when Telegram says it is gone (open a new one instead)."""
    try:
        await deps.messenger.reopen_forum_topic(chat_id, thread_id, timeout=timeout)
    except TopicGone:
        return None
    async with deps.db.uow() as s:
        await store.mark_topic_closed(s, chat_id=chat_id, thread_id=thread_id, closed_at=None)
    log.info("topic_reopened", chat_id=chat_id, thread_id=thread_id)
    return thread_id


async def _open(deps: SharedDeps, *, chat_id: int, project_id: str, timeout: float, replaces: int | None = None) -> int:
    """Open the project's topic (named after it, its own icon colour) and remember it; a refusal raises."""
    topic = await deps.messenger.create_forum_topic(
        chat_id, domain.topic_name(project_id), icon_color=domain.icon_color_of(project_id), timeout=timeout
    )
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await store.set_project_topic(s, project_id=project_id, chat_id=chat_id, thread_id=topic.thread_id, at=now)
        chat = await store.get_topic_chat(s, chat_id)
        if chat is not None and chat.refusal is not None:
            await store.set_chat_refusal(s, chat_id=chat_id, refusal=None, at=now)
    log.info("topic_opened", project=project_id, chat_id=chat_id, thread_id=topic.thread_id, replaces=replaces)
    return topic.thread_id


async def _refused(deps: SharedDeps, *, chat_id: int, error: TopicsUnavailable, chat: ChatTopics | None) -> bool:
    """Remember Telegram's refusal. True only for a first refusal for want of the right since topics last opened:
    the owner is told once, not on every project's work."""
    refusal = Refusal.NO_RIGHT if error.reason == Refusal.NO_RIGHT.value else Refusal.NOT_FORUM
    async with deps.db.uow() as s:
        await store.set_chat_refusal(s, chat_id=chat_id, refusal=refusal.value, at=deps.clock.now())
    log.warning("topic_refused", chat_id=chat_id, reason=refusal.value)
    already = chat is not None and chat.refusal is Refusal.NO_RIGHT
    return refusal is Refusal.NO_RIGHT and not already
