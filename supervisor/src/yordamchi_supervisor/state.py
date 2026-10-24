"""`DATA/supervisor/state.json`: what the supervisor must remember across its own restarts.

One JSON object with one section per job: `watchdog` (when each crash-loop alert went out) and `backups` (the last
good day). The `update` section is reserved for the self-updater (T57, SPEC §3.2.12), which journals every step
there before it runs. Writes are atomic (temp file, fsync, `os.replace`), so `kill -9` leaves the old or the new
file, never half of one. An unreadable file is moved aside as `state.json.corrupt` and the supervisor starts fresh:
the worst case is one repeated alert.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

type Section = dict[str, object]

RESERVED_UPDATE_SECTION: Final = "update"


def parse_instant(value: object) -> datetime | None:
    """An ISO instant saved in a section, or None for anything damaged (a hand edit must not stop a job)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class StateFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Section] = self._load()

    def _load(self) -> dict[str, Section]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self._path.replace(self._path.with_name(self._path.name + ".corrupt"))
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if isinstance(value, dict)}

    def section(self, name: str) -> Section:
        """A copy; change it and pass it to `save_section`."""
        return dict(self._data.get(name, {}))

    def save_section(self, name: str, value: Section) -> None:
        self._data[name] = dict(value)
        self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self._path)
