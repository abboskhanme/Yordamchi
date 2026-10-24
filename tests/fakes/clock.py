"""`FakeClock`: virtual time for windows, TTLs and schedules. Sleepers wake only when the test advances time."""

import asyncio
import heapq
from datetime import UTC, datetime, timedelta
from typing import Final

from yordamchi.ports.clock import Clock

DEFAULT_START: Final = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None:
            msg = "FakeClock needs a timezone-aware start"
            raise ValueError(msg)
        self._now = start.astimezone(UTC)
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._sequence = 0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(self._sleepers, (self._now + timedelta(seconds=seconds), self._sequence, future))
        await future

    @property
    def pending_sleepers(self) -> int:
        return sum(1 for _, _, future in self._sleepers if not future.done())

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking every sleeper that falls due, in order, and letting it run."""
        target = self._now + timedelta(seconds=seconds)
        while self._sleepers and self._sleepers[0][0] <= target:
            wake_at, _, future = heapq.heappop(self._sleepers)
            self._now = max(self._now, wake_at)
            if not future.done():
                future.set_result(None)
            await _settle()
        self._now = target
        await _settle()

    def set(self, moment: datetime) -> None:
        """Jump without waking anyone (e.g. to simulate a sleeping Mac)."""
        self._now = moment.astimezone(UTC)


async def _settle(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


_: Clock = FakeClock()
