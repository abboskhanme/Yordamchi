"""core/aio (forever, spawn) and core/retry (adapters only)."""

import asyncio

import pytest

from yordamchi.core import aio
from yordamchi.core.errors import AuthFailed, CallTimedOut, ProviderUnavailable, RateLimited
from yordamchi.core.retry import backoff_delay, retrying


async def test_forever_restarts_a_failing_body_with_doubling_backoff() -> None:
    runs: list[int] = []
    delays: list[float] = []

    async def body() -> None:
        runs.append(len(runs))
        raise RuntimeError("poll failed")

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 6:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await aio.forever("poller", body, initial_backoff_s=1, max_backoff_s=8, sleep=sleep)
    assert len(runs) == 6
    assert delays == [1, 2, 4, 8, 8, 8]


async def test_forever_restarts_a_body_that_returns() -> None:
    runs = 0

    async def body() -> None:
        nonlocal runs
        runs += 1

    async def sleep(seconds: float) -> None:
        del seconds
        if runs == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await aio.forever("outbox", body, sleep=sleep)
    assert runs == 2


async def test_cancellation_is_never_swallowed() -> None:
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        await asyncio.Event().wait()

    task = aio.spawn("loop", aio.forever("heartbeat", body))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_spawn_keeps_a_reference_until_the_task_ends() -> None:
    release = asyncio.Event()

    async def work() -> int:
        await release.wait()
        return 7

    before = aio.running_tasks()
    task = aio.spawn("work", work())
    assert aio.running_tasks() == before + 1
    release.set()
    assert await task == 7
    await asyncio.sleep(0)
    assert aio.running_tasks() == before


async def test_retrying_retries_only_retryable_errors() -> None:
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ProviderUnavailable(provider="google")
        return "ok"

    assert await retrying(flaky, timeout=5, operation="stt", base_delay_s=0.001, max_delay_s=0.002) == "ok"
    assert len(attempts) == 3

    async def denied() -> str:
        attempts.append(1)
        raise AuthFailed(provider="google")

    attempts.clear()
    with pytest.raises(AuthFailed):
        await retrying(denied, timeout=5, operation="stt")
    assert len(attempts) == 1


async def test_retrying_honours_retry_after_and_its_deadline() -> None:
    async def limited() -> str:
        raise RateLimited(provider="openrouter", retry_after=60)

    with pytest.raises(RateLimited):  # waiting 60 s would pass the 1 s budget: give up at once
        await retrying(limited, timeout=1, operation="tts")

    async def slow() -> str:
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(CallTimedOut) as caught:
        await retrying(slow, timeout=0.05, operation="tts")
    assert caught.value.params == {"operation": "tts"}


def test_backoff_has_jitter_within_its_ceiling() -> None:
    for attempt in range(1, 6):
        delay = backoff_delay(attempt, base_s=0.5, max_s=4)
        ceiling = min(4, 0.5 * 2 ** (attempt - 1))
        assert ceiling / 2 <= delay <= ceiling
