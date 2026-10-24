"""`audit_events`: append-only audit of executor inputs, gate verdicts, decisions, setting, integration and secret
changes, sessions and dropped strangers (SPEC §4.2, NFR-SEC-10). Kept 90 days; no UPDATE or DELETE before that.

`data_json` holds ids, codes, sha256 digests and scrubbed commands — never message text. T08 builds the feature.
"""

from datetime import datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_bot_at", "bot", "at"),
        Index("ix_audit_events_kind_at", "kind", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(16))  # owner | executor | system | supervisor
    kind: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(String(64))
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    data_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
