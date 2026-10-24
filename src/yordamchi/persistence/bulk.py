"""Bulk deletes and updates: retention runs and conditional writes (SPEC §4.2, §4.6).

- `affected_rows(s, statement)`: run a DELETE or UPDATE and return how many rows it touched (a conditional UPDATE
  that must hit exactly one row, a batch delete).
- `run_in_batches(db, step)`: SPEC §4.2 retention "deletes in batches of 500". Each batch is its own short unit of
  work, so the single SQLite writer is never held for long; a crash mid-run only leaves the rest for the next run.

    async def apply_retention(deps: SharedDeps) -> int:
        cutoff = domain.retention_cutoff(deps.clock.now())
        return await bulk.run_in_batches(deps.db, lambda s, limit: store.delete_x_before(s, cutoff, limit=limit))
"""

from collections.abc import Awaitable, Callable
from typing import Final

from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Executable

from yordamchi.persistence.uow import Database

RETENTION_BATCH: Final = 500

# One batch: touch at most `limit` rows, return how many were touched.
type BatchStep = Callable[[AsyncSession, int], Awaitable[int]]


async def affected_rows(s: AsyncSession, statement: Executable) -> int:
    result = await s.execute(statement)
    return result.rowcount if isinstance(result, CursorResult) else 0


async def run_in_batches(db: Database, step: BatchStep, *, batch: int = RETENTION_BATCH) -> int:
    """Repeat `step` in fresh units of work until a batch touches fewer than `batch` rows. Returns the total."""
    total = 0
    while True:
        async with db.uow() as s:
            touched = await step(s, batch)
        total += touched
        if touched < batch:
            return total
