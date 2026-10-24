"""Every one-off job of the dev bot runs here: `common.guarded()` (logged, told to the owner, never raised), and an
expected failure's code noted for `holat?`'s "Oxirgi xatolar" line (SPEC §6.2; T32 `health.note_error`).

    await jobs.run(deps, health_state, work(), job="route_burst", chat_id=item.chat_id)
"""

from collections.abc import Awaitable

from yordamchi.bots import common
from yordamchi.core.errors import YordamchiError
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.health import service as health
from yordamchi.shared.health.service import HealthState


async def _noted[T](deps: DevDeps, state: HealthState, work: Awaitable[T]) -> T:
    try:
        return await work
    except YordamchiError as error:
        health.note_error(deps, state, error.code.value)
        raise


async def run[T](
    deps: DevDeps, state: HealthState, work: Awaitable[T], *, job: str, chat_id: int | None = None
) -> T | None:
    """`work`'s value, or None when it failed (already logged, noted and told)."""
    return await common.guarded(deps, _noted(deps, state, work), job=job, chat_id=chat_id)
