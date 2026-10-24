"""Idempotency keys are written before acting (CONVENTIONS §8; SPEC §4.6).

`insert_or_ignore` is dialect-dispatched (SQLite now, PostgreSQL by config later, D1) and reports whether the row was
inserted: `inbound_updates (bot, update_id)` is processed only if it was.
"""

from collections.abc import Mapping, Sequence

from sqlalchemy import CursorResult
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Executable

from yordamchi.persistence.base import Base


async def insert_or_ignore(
    s: AsyncSession, model: type[Base], values: Mapping[str, object], *, conflict_columns: Sequence[str]
) -> bool:
    """Insert one row unless `conflict_columns` already hold these values. True if a row was inserted."""
    dialect = s.get_bind().dialect.name
    statement: Executable
    if dialect == "sqlite":
        statement = sqlite.insert(model).values(dict(values)).on_conflict_do_nothing(index_elements=conflict_columns)
    elif dialect == "postgresql":
        statement = (
            postgresql.insert(model).values(dict(values)).on_conflict_do_nothing(index_elements=conflict_columns)
        )
    else:
        msg = f"insert_or_ignore has no implementation for dialect {dialect!r}"
        raise NotImplementedError(msg)
    result = await s.execute(statement)
    return isinstance(result, CursorResult) and result.rowcount == 1
