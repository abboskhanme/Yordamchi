"""`setting_versions`: append-only history of every settings change (SPEC §4.2; kept forever).

A change is a new row with the next `version` of its key; a revert is a new row holding the older value. Rows are
never updated or deleted. Each bot writes only its own DB file; the personal bot reads the dev rows of shared keys
through a read-only connection.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class SettingVersion(Base):
    __tablename__ = "setting_versions"
    __table_args__ = (UniqueConstraint("key", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(96))
    value_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(16))  # change | revert | import
    actor: Mapped[str] = mapped_column(String(16))  # owner | system
    origin_update_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime]
