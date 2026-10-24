"""Tables of `shared/health` (SPEC §4.2): `health_events` (90 d) and `kv_state` (small per-bot state).

T32 builds the feature (`holat?`, heartbeat, wake gap, self-checks) on these tables and owns their retention.
"""

from datetime import datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class HealthEvent(Base):
    """started | clean_exit | unclean_restart | wake_gap | network_gap | smoke_ok | smoke_failed | cli_version |
    pressure | disk_low."""

    __tablename__ = "health_events"
    __table_args__ = (Index("ix_health_events_bot_at", "bot", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))
    data_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)


class KvState(Base):
    """Pinned "Onlayn" message id, last CLI version, speech status, …"""

    __tablename__ = "kv_state"

    bot: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    updated_at: Mapped[datetime]
