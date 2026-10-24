"""Portable column types (SPEC §4.1, CONVENTIONS §8).

- `UTCDateTime`: every `*_at`. Refuses naive datetimes on write; stores naive UTC; returns UTC-aware values.
- `JSONText`: JSON stored as `Text` in the canonical encoding of `core.json_value`.

Both render as their plain `impl` in migrations (`migrations/env.py`), so revisions never import app code.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Dialect, Text
from sqlalchemy.types import TypeDecorator

from yordamchi.core import json_value
from yordamchi.core.json_value import JsonValue


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "naive datetime written to a UTCDateTime column; pass a timezone-aware value"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class JSONText(TypeDecorator[JsonValue]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: JsonValue, dialect: Dialect) -> str | None:
        del dialect
        return None if value is None else json_value.dumps(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> JsonValue:
        del dialect
        return None if value is None else json_value.loads(value)
