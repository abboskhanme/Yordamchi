"""Clock: the only source of "now" outside `adapters/system_clock.py` (CONVENTIONS §3, §6)."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, UTC-aware."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait `seconds` on this clock. The duration is its own bound (the one A4 exemption)."""
        ...
