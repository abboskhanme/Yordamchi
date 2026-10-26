"""SQL for `project_topics` and `topic_chats`. Never commits; the service's unit of work does."""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.persistence import bulk
from yordamchi.shared.topics.models import ProjectTopic, TopicChat


# --- project_topics ---------------------------------------------------------------------------------------------------
async def get_project_topic(s: AsyncSession, project_id: str) -> ProjectTopic | None:
    return await s.get(ProjectTopic, project_id)


async def find_topic_by_thread(s: AsyncSession, *, chat_id: int, thread_id: int) -> ProjectTopic | None:
    """The project whose topic `thread_id` is in `chat_id` (IX chat_id, thread_id)."""
    query = select(ProjectTopic).where(ProjectTopic.chat_id == chat_id, ProjectTopic.thread_id == thread_id)
    return await s.scalar(query.order_by(ProjectTopic.created_at.desc()).limit(1))


async def list_project_topics(s: AsyncSession, *, chat_id: int | None = None) -> list[ProjectTopic]:
    query = select(ProjectTopic)
    if chat_id is not None:
        query = query.where(ProjectTopic.chat_id == chat_id)
    return list((await s.scalars(query.order_by(ProjectTopic.project_id))).all())


async def set_project_topic(s: AsyncSession, *, project_id: str, chat_id: int, thread_id: int, at: datetime) -> None:
    """The project's topic is now `thread_id`, open, opened at `at` (a first topic, or a new one for a gone one)."""
    row = await s.get(ProjectTopic, project_id)
    if row is None:
        s.add(ProjectTopic(project_id=project_id, chat_id=chat_id, thread_id=thread_id, created_at=at, closed_at=None))
    else:
        row.chat_id = chat_id
        row.thread_id = thread_id
        row.created_at = at
        row.closed_at = None
    await s.flush()


async def mark_topic_closed(s: AsyncSession, *, chat_id: int, thread_id: int, closed_at: datetime | None) -> int:
    """Set (`closed_at`) or clear (None) the closed mark of the topic `thread_id`. Returns the rows changed."""
    statement = (
        update(ProjectTopic)
        .where(ProjectTopic.chat_id == chat_id, ProjectTopic.thread_id == thread_id)
        .values(closed_at=closed_at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def mark_open_topics_closed(s: AsyncSession, *, chat_id: int, at: datetime) -> int:
    """Every open topic of the chat gets `closed_at = at`. Returns how many."""
    statement = (
        update(ProjectTopic)
        .where(ProjectTopic.chat_id == chat_id, ProjectTopic.closed_at.is_(None))
        .values(closed_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_project_topic(s: AsyncSession, *, project_id: str, thread_id: int) -> int:
    """Forget the project's topic, if it is still `thread_id`."""
    statement = (
        delete(ProjectTopic)
        .where(ProjectTopic.project_id == project_id, ProjectTopic.thread_id == thread_id)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


# --- topic_chats ------------------------------------------------------------------------------------------------------
async def get_topic_chat(s: AsyncSession, chat_id: int) -> TopicChat | None:
    return await s.get(TopicChat, chat_id)


async def set_chat_forum(s: AsyncSession, *, chat_id: int, is_forum: bool, at: datetime) -> bool:
    """Record whether the chat is a forum (what the owner's latest message there says). True if it changed."""
    row = await s.get(TopicChat, chat_id)
    if row is None:
        s.add(TopicChat(chat_id=chat_id, is_forum=is_forum, noted_at=at, refused_at=None, refusal=None))
    elif row.is_forum == is_forum:
        return False
    else:
        row.is_forum = is_forum
        row.noted_at = at
    await s.flush()
    return True


async def set_chat_refusal(s: AsyncSession, *, chat_id: int, refusal: str | None, at: datetime) -> None:
    """Telegram refused to open a topic (`refusal`), or opened one again (None). A chat seen refusing as no forum
    is no forum until a message says otherwise."""
    row = await s.get(TopicChat, chat_id)
    if row is None:
        row = TopicChat(chat_id=chat_id, is_forum=refusal != "not_forum", noted_at=at, refused_at=None, refusal=None)
        s.add(row)
    if refusal == "not_forum":
        row.is_forum = False
        row.noted_at = at
    row.refusal = refusal
    row.refused_at = at if refusal is not None else None
    await s.flush()
