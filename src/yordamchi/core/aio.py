"""Background tasks and long-lived loops (CONVENTIONS §6).

- `spawn(name, coro)`: the only `create_task`. The task is kept referenced until done, and a failure is logged.
- `forever(name, body)`: runs a long-lived loop (poller, outbox, scheduler, heartbeat). If the body raises or
  returns, it is logged and restarted after a backoff (1 → 2 → 4 … ≤ 30 s, reset after a healthy run).
- `raise_if_cancelling()`: call it first inside any `except Exception` in a loop, so a cancellation that a library
  turned into another exception still stops the task (OvozYoz `engines/base.py`).

`CancelledError` is never swallowed here; it ends the loop.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Final, NoReturn

from yordamchi.core.log import get_log

log = get_log(__name__)

type Sleep = Callable[[float], Awaitable[None]]
type Body = Callable[[], Awaitable[None]]

FOREVER_INITIAL_BACKOFF_S: Final = 1.0
FOREVER_MAX_BACKOFF_S: Final = 30.0
FOREVER_HEALTHY_AFTER_S: Final = 60.0

_running: set[asyncio.Task[object]] = set()


def raise_if_cancelling() -> None:
    """Re-raise cancellation if the current task is being cancelled."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _on_done(task: asyncio.Task[object]) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("task_failed", task=task.get_name(), error_type=type(error).__name__)


def spawn[T](name: str, coro: Coroutine[object, object, T]) -> asyncio.Task[T]:
    """Start a background task that stays referenced until it finishes (RUF006)."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_on_done)
    return task


def running_tasks() -> int:
    return len(_running)


async def forever(
    name: str,
    body: Body,
    *,
    initial_backoff_s: float = FOREVER_INITIAL_BACKOFF_S,
    max_backoff_s: float = FOREVER_MAX_BACKOFF_S,
    healthy_after_s: float = FOREVER_HEALTHY_AFTER_S,
    sleep: Sleep = asyncio.sleep,
) -> NoReturn:
    """Run `body` for the life of the process, restarting it with backoff whenever it fails or returns."""
    backoff = initial_backoff_s
    while True:
        started = time.monotonic()
        try:
            await body()
        except Exception as error:
            raise_if_cancelling()
            log.exception("loop_failed", loop=name, error_type=type(error).__name__, backoff_s=backoff)
        else:
            log.warning("loop_returned", loop=name, backoff_s=backoff)
        if time.monotonic() - started >= healthy_after_s:
            backoff = initial_backoff_s
        await sleep(backoff)
        backoff = min(backoff * 2, max_backoff_s)
