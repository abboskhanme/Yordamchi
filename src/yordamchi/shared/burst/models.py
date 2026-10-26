"""`bursts`: one row per conversational burst, the owner's parts gathered into one turn (SPEC §4.2, §4.4; UC-02).

A burst is `open` or `holding` while it assembles, `closed` once it is one turn input waiting for pre-parse and
routing, then `routed`, `merged` (it joined a turn that was already working) or `cancelled`. `input_json` holds the
parts, with the inbox's already redacted content, scrubbed again before the write (✱). `inbound_updates.burst_id`
points here. Terminal rows are deleted after 30 days; an assembling or closed row is never touched.

`thread_id` (since `0002`) is the forum topic its parts were written in (NULL: General, or a group with no Topics): a
burst never holds parts of two topics (D4 revised 2026-10-26).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText
from yordamchi.shared.burst.domain import ASSEMBLING


class Burst(Base):
    __tablename__ = "bursts"
    __table_args__ = (Index("ix_bursts_bot_status", "bot", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))  # open | holding | closed | routed | merged | cancelled
    opened_at: Mapped[datetime]  # when its first part was received
    last_part_at: Mapped[datetime]
    closes_at: Mapped[datetime]  # assembling: when it closes unless a part or a transcript arrives first
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(String(16))  # quiet | cap | hold_expired | interrupted | …
    input_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)
    route_id: Mapped[int | None] = mapped_column(Integer)  # `route_decisions.id` (routing), once routed
    thread_id: Mapped[int | None] = mapped_column(BigInteger)  # the forum topic of its parts; NULL: General (0002)


# At most one assembling burst per chat, frozen in `0001`. With topics, a part from another topic closes it first
# (`domain.place`), so a burst still never mixes topics; relaxing the index to (chat, topic) is a contraction.
_ASSEMBLING = sorted(status.value for status in ASSEMBLING)
Index(
    "uq_bursts_bot_chat_id_assembling",
    Burst.bot,
    Burst.chat_id,
    unique=True,
    sqlite_where=Burst.status.in_(_ASSEMBLING),
    postgresql_where=Burst.status.in_(_ASSEMBLING),
)
