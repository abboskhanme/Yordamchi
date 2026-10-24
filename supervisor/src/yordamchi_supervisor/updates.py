"""Read side of `DATA/updates/<id>/` (SPEC §2.2, §3.2.12): is a self-update in progress?

The watchdog stays silent while one is, because draining, switching and probation restart the bots on purpose. An
update is in progress from `request.json` until `status.json` says a terminal status. Statuses are the
`self_updates.status` values of SPEC §4.2.

An update whose files have not changed for `STALE_AFTER` is not trusted to be in progress. This covers a request that
nothing picks up (the updater is P2) or an updater that died: either way the watchdog must not stay muted forever.

Seam for T57: the updater writes these files; it does not need to change this module.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi_supervisor.clock import from_timestamp

TERMINAL: Final = frozenset({"healthy", "merged", "rolled_back", "failed", "refused"})
STALE_AFTER: Final = timedelta(hours=2)  # drain ≤ 30 min + build + switch + probation 120 s, with margin


@dataclass(frozen=True, slots=True)
class UpdateScan:
    active: str | None = None  # the id of the update in progress
    status: str | None = None
    stale: tuple[str, ...] = field(default=())  # non-terminal but untouched for STALE_AFTER


def _status(directory: Path) -> str:
    try:
        raw = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "requested"
    except (OSError, ValueError):
        return "unknown"  # mid-write or damaged: treated as in progress, bounded by STALE_AFTER
    value = raw.get("status") if isinstance(raw, dict) else None
    return value if isinstance(value, str) else "unknown"


def _touched(directory: Path) -> datetime | None:
    times = [p.stat().st_mtime for p in (directory / "request.json", directory / "status.json") if p.exists()]
    return from_timestamp(max(times)) if times else None


def scan(updates_dir: Path, now: datetime) -> UpdateScan:
    if not updates_dir.is_dir():
        return UpdateScan()
    active: str | None = None
    active_status: str | None = None
    stale: list[str] = []
    for directory in sorted(p for p in updates_dir.iterdir() if p.is_dir()):
        if not (directory / "request.json").exists():
            continue
        status = _status(directory)
        if status in TERMINAL:
            continue
        touched = _touched(directory)
        if touched is None or now - touched > STALE_AFTER:
            stale.append(directory.name)
            continue
        active, active_status = directory.name, status
    return UpdateScan(active=active, status=active_status, stale=tuple(stale))
