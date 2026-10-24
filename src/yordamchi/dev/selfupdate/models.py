"""Tables of `dev/selfupdate` (SPEC §4.2).

- `self_updates`: one row per "O'zingga X qo'sh" (UC-19). The bot owns `working` → `awaiting_core` → `requested`;
  from there the row mirrors `updates/<id>/status.json`, which only the supervisor writes. `base_commit` is `main`
  when the work branch was cut; `commit` is the branch head the request names. `summary` is the turn's result,
  redacted before the write. `reported_at` marks the owner's outcome message as queued (exactly once). Kept forever:
  it is the history of what Yordamchi did to itself. `turn_id` and `decision_id` name rows of other features without
  a foreign key (A2).
"""

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class SelfUpdate(Base):
    __tablename__ = "self_updates"
    __table_args__ = (Index("ix_self_updates_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))  # working | awaiting_core | requested | building | … | failed
    kind: Mapped[str] = mapped_column(String(16))  # app | supervisor
    branch: Mapped[str] = mapped_column(String(160))
    base_commit: Mapped[str | None] = mapped_column(String(40))
    commit: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[str | None] = mapped_column(String(40))
    changed_files_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    touches_core: Mapped[bool] = mapped_column(Boolean, default=False)
    decision_id: Mapped[int | None] = mapped_column(Integer)
    turn_id: Mapped[int | None] = mapped_column(Integer)
    tests_count: Mapped[int | None] = mapped_column(Integer)
    result_code: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    requested_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    reported_at: Mapped[datetime | None]
