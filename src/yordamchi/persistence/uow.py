"""Units of work — the only code that commits (CONVENTIONS §3).

    async with deps.db.uow() as s:      # BEGIN IMMEDIATE … COMMIT (rollback on any exception)
        await store.add_x(s, …)         # only store.*, s.* and <feature>.service.*(s, …) inside (A3)
    async with deps.db.read() as s:     # a read snapshot, never committed
        rows = await store.list_x(s)

`expire_on_commit=False`, so rows stay usable after the block. Never await a port (Telegram, Claude, git, SSH) inside
`uow()`: SQLite has one writer per file and a turn runs for minutes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Execution option read by the engine's "begin" listener (persistence/engine.py).
BEGIN_OPTION: Final = "yordamchi_begin"

# The session a `(s: Session, …)` service function joins (CONVENTIONS §4, A3). Services annotate with this name, so
# only persistence, `*/store.py` and `*/models.py` import sqlalchemy (C5).
type Session = AsyncSession


class ReadOnlyDatabase(RuntimeError):
    """A unit of work was opened on the read-only handle of the other bot's DB file."""


class Database:
    def __init__(self, engine: AsyncEngine, *, readonly: bool) -> None:
        self.engine = engine
        self.readonly = readonly
        immediate = engine.execution_options(**{BEGIN_OPTION: "IMMEDIATE"})
        self._writer = async_sessionmaker(immediate, expire_on_commit=False)
        self._reader = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[AsyncSession]:
        if self.readonly:
            msg = "this database handle is read-only"
            raise ReadOnlyDatabase(msg)
        async with self._writer() as session, session.begin():
            yield session

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        async with self._reader() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
