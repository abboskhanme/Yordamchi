"""Tables of `shared/cost` (SPEC §4.2; UC-07, NFR-COST-02). Both are kept 400 days.

- `usage_records`: the usage ledger, one row per billable or metered call. `cost_usd_micros` is an integer (micro-USD),
  never a float.
- `cap_notices`: the 80 % / 100 % notices already sent, one per provider, month, level and cap. The cap is part of the
  key, so a cap the owner raised (or lowered) mid-month gets its own warning and its own pause notice.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_bot_at", "bot", "at"),
        Index("ix_usage_records_provider_at", "provider", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime]
    source: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))  # anthropic_plan | google | openrouter | openai | edge
    is_paid_api: Mapped[bool] = mapped_column(Boolean)
    model: Mapped[str] = mapped_column(String(96))
    project_id: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[int | None] = mapped_column(Integer)
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger)
    units_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer)


class CapNotice(Base):
    """`level` is 80 (the warning at `caps.warn_pct`) or 100 (paid calls paused); `month` is `YYYY-MM` in
    `ui.timezone`; `cap_usd` is the cap the notice was about."""

    __tablename__ = "cap_notices"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    cap_usd: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime]
