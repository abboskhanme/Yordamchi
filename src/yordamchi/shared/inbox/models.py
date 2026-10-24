"""Tables of `shared/inbox` (SPEC §4.2, §4.6).

- `inbound_updates`: one row per Telegram update, keyed UNIQUE(bot, update_id) and written before anything acts on
  the update, so a redelivered update is processed exactly once (NFR-REL-05). `payload_json` holds only redacted,
  scrubbed content: keys were moved to the vault first (UC-04). Payload nulled after 7 days, row deleted after 30;
  a `received` row is never touched.
- `poll_offsets`: the next `getUpdates` offset per bot, persisted after each fed batch. Kept forever (one row).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class InboundUpdate(Base):
    __tablename__ = "inbound_updates"
    __table_args__ = (
        UniqueConstraint("bot", "update_id"),
        Index("ix_inbound_updates_bot_status_update_id", "bot", "status", "update_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    update_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # text | voice | audio | photo | video | document | link | …
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    payload_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16))  # received | attached | ignored | failed
    # T16 (burst) adds ForeignKey("bursts.id") once that table exists.
    burst_id: Mapped[int | None] = mapped_column(Integer)
    received_at: Mapped[datetime]


class PollOffset(Base):
    __tablename__ = "poll_offsets"

    bot: Mapped[str] = mapped_column(String(16), primary_key=True)
    next_offset: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime]
