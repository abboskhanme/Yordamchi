"""Tables of `shared/topics` (SPEC §4.2; D4 revised 2026-10-26).

- `project_topics`: the forum topic the bot opened for a project in the dev group, one row per project. A topic
  Telegram says is gone is opened anew and its row rewritten; `closed_at` is set while nothing may be posted there
  (the owner closed it, or `topics.enabled` is off) and cleared once the bot reopens it. Kept: a row per project.
- `topic_chats`: what the bot knows of a chat's topics: whether it is a forum (each owner message says, through the
  inbox), and when Telegram last refused to open a topic there. Kept: a row per chat.

`project_id` is a `projects.id` string with no foreign key, as in `outbox_messages` and `message_links` (A2).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.persistence.base import Base


class ProjectTopic(Base):
    __tablename__ = "project_topics"
    # A message in a topic finds its project by (chat, thread).
    __table_args__ = (Index("ix_project_topics_chat_id_thread_id", "chat_id", "thread_id"),)

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger)  # the topic's message_thread_id
    created_at: Mapped[datetime]  # when this topic was opened (again, after the last one was gone)
    closed_at: Mapped[datetime | None]


class TopicChat(Base):
    __tablename__ = "topic_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_forum: Mapped[bool] = mapped_column(Boolean)
    noted_at: Mapped[datetime]  # when `is_forum` last changed
    refused_at: Mapped[datetime | None]  # Telegram's latest refusal to open a topic here
    refusal: Mapped[str | None] = mapped_column(String(16))  # not_forum | no_right
