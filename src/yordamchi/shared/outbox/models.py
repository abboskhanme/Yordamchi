"""Tables of `shared/outbox` (SPEC §4.2, §4.6; NFR-REL-06).

- `outbox_messages`: every Telegram call either bot makes for the owner, written before the call. UNIQUE(dedupe_key)
  makes enqueueing idempotent; `payload_json` holds only scrubbed content and only the keys its op allows. A finished
  row (sent, failed, superseded, cancelled) is deleted after 30 days; a pending or sending row is never touched.
- `thread_id` (since `0002`): the forum topic a message or a typing hint goes to; NULL is the chat itself (General in
  a forum). Set when the row is written: the caller's thread (an answer goes where the owner wrote), else the open
  topic of the project the row is about (`shared/topics`), else NULL. Edits, deletes, reactions and pins name their
  message, so they carry none.
- `message_links`: which project, turn, question or decision each message we posted belongs to, so routing can use a
  reply as one signal (SPEC §3.2.6). Kept 90 days. `outbox_id` is a plain column: the outbox row it came from is
  deleted after 30 days, so a foreign key could not hold.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("ix_outbox_messages_bot_status_priority_next_attempt_at", "bot", "status", "priority", "next_attempt_at"),
        Index("ix_outbox_messages_coalesce_key_status", "coalesce_key", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    op: Mapped[str] = mapped_column(String(16))  # send_text | edit_text | delete | react | chat_action | pin | send_*
    kind: Mapped[str] = mapped_column(String(16))  # ack | echo | progress | result | question | decision | …
    payload_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128))  # <kind>:<turn_or_ref>:<part>
    coalesce_key: Mapped[str | None] = mapped_column(String(128))  # progress:<turn_id> | typing:<chat_id>
    priority: Mapped[int] = mapped_column(Integer)  # 0 (acks, questions, decisions) … 3
    status: Mapped[str] = mapped_column(String(16))  # pending | sending | sent | failed | superseded | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime]
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    disable_notification: Mapped[bool] = mapped_column(Boolean, default=False)
    # T17 / T24 add ForeignKey("projects.id") and ForeignKey("turns.id") once those tables exist.
    project_id: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]  # the latest successful call (a typing hint updates it on each repeat)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)  # the forum topic; NULL: the chat itself (0002)


class MessageLink(Base):
    __tablename__ = "message_links"
    # An edit or a pin finds its message through the outbox row it was posted from.
    __table_args__ = (Index("ix_message_links_outbox_id", "outbox_id"),)

    bot: Mapped[str] = mapped_column(String(16), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outbox_id: Mapped[int] = mapped_column(Integer)
    # String ids of other features' rows (A2): T17 projects, T24 turns, T29 questions, T18 decisions.
    project_id: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[int | None] = mapped_column(Integer)
    question_id: Mapped[int | None] = mapped_column(Integer)
    decision_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
