import asyncio
from datetime import timedelta

import pytest

from tests.fakes.clock import FakeClock
from yordamchi.adapters.system_clock import SystemClock
from yordamchi.ports.clock import Clock


class ClockContract:
    @pytest.fixture
    def clock(self) -> Clock:
        raise NotImplementedError

    def test_now_is_utc_aware(self, clock: Clock) -> None:
        now = clock.now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_now_never_goes_backwards(self, clock: Clock) -> None:
        first = clock.now()
        assert clock.now() >= first

    async def test_sleep_zero_returns_at_once(self, clock: Clock) -> None:
        await clock.sleep(0)


class TestFakeClock(ClockContract):
    @pytest.fixture
    def clock(self) -> Clock:
        return FakeClock()

    async def test_sleepers_wake_only_when_time_advances(self) -> None:
        clock = FakeClock()
        woke: list[float] = []

        async def sleeper(seconds: float) -> None:
            await clock.sleep(seconds)
            woke.append(seconds)

        async with asyncio.TaskGroup() as group:
            group.create_task(sleeper(5))
            group.create_task(sleeper(2))
            await clock.advance(0)  # let both tasks start sleeping
            assert clock.pending_sleepers == 2
            await clock.advance(1)
            assert woke == []
            await clock.advance(1)
            assert woke == [2]
            await clock.advance(10)
        assert woke == [2, 5]
        assert clock.pending_sleepers == 0


class TestSystemClock(ClockContract):
    @pytest.fixture
    def clock(self) -> Clock:
        return SystemClock()
