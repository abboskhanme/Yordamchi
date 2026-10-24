"""Wall-clock time, injectable so tests run on virtual time. Every instant is timezone-aware UTC."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Final, Protocol

# Asia/Tashkent is UTC+5 with no DST since 1992, so a fixed offset works without tzdata. Backups run at 04:00 local.
TASHKENT: Final = timezone(timedelta(hours=5), "Asia/Tashkent")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def from_timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
