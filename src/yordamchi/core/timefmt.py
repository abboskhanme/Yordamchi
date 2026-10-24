"""Time display for the owner. Instants come from `deps.clock` (UTC-aware); the zone is the `ui.timezone` setting.

Words ("daqiqa", "oldin") belong to `core/strings.py`; these helpers only produce numbers and `HH:MM`.
"""

from datetime import datetime
from zoneinfo import ZoneInfo


def require_aware(moment: datetime) -> datetime:
    """Refuse naive datetimes (CONVENTIONS §13)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        msg = "naive datetime; every instant must be timezone-aware"
        raise ValueError(msg)
    return moment


def local(moment: datetime, timezone: str) -> datetime:
    return require_aware(moment).astimezone(ZoneInfo(timezone))


def hhmm(moment: datetime, timezone: str) -> str:
    """`14:05` in the owner's zone."""
    return local(moment, timezone).strftime("%H:%M")


def whole_minutes(seconds: float) -> int:
    """Rounded minutes, never negative: for "N daqiqa oldin"."""
    return max(0, round(seconds / 60))
