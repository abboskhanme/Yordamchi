"""SQLite engines for `dev.db` and `personal.db` (SPEC §2.3; CONVENTIONS §8).

- Read-write (the owning bot): `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`;
  the file is created 0600 in a 0700 directory. A unit of work starts with `BEGIN IMMEDIATE`, so two concurrent
  writers in one process wait on `busy_timeout` instead of failing on a lock upgrade.
- Read-only (the other bot's file: the personal bridge, the dev numbers/masking read):
  `file:…?mode=ro&uri=true` plus `PRAGMA query_only=ON`. A write raises.

Engines are built in `bots/<bot>/app.py` and handed out through `Database`; never at module level.
"""

import urllib.parse
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy import URL, event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from yordamchi.persistence.uow import BEGIN_OPTION, Database

BUSY_TIMEOUT_MS: Final = 5000
WRITE_PRAGMAS: Final = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
)
READ_ONLY_PRAGMAS: Final = (
    "PRAGMA query_only=ON",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
)


class _DbapiCursor(Protocol):
    def execute(self, sql: str, /) -> object: ...
    def close(self) -> None: ...


class _HasCursor(Protocol):
    def cursor(self) -> _DbapiCursor: ...


class _DbapiConnection(_HasCursor, Protocol):
    isolation_level: str | None


def apply_pragmas(dbapi_connection: _HasCursor, pragmas: tuple[str, ...]) -> None:
    """Run connection PRAGMAs on a raw DBAPI connection (also used by `migrations/env.py`)."""
    cursor = dbapi_connection.cursor()
    try:
        for pragma in pragmas:
            cursor.execute(pragma)
    finally:
        cursor.close()


def ensure_db_file(path: Path) -> None:
    """Create the DB directory (0700) and file (0600) before SQLite does, so the modes never depend on umask."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


def sqlite_url(path: Path, *, readonly: bool, driver: str = "aiosqlite") -> URL:
    drivername = f"sqlite+{driver}" if driver else "sqlite"
    if not readonly:
        return URL.create(drivername, database=str(path))
    # A URI filename must be percent-encoded ("Application Support" has a space).
    uri = "file:" + urllib.parse.quote(str(path.resolve()))
    return URL.create(drivername, database=uri, query={"mode": "ro", "uri": "true"})


def create_engine(path: Path, *, readonly: bool = False) -> AsyncEngine:
    if not readonly:
        ensure_db_file(path)
    engine = create_async_engine(sqlite_url(path, readonly=readonly))
    pragmas = READ_ONLY_PRAGMAS if readonly else WRITE_PRAGMAS

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: _DbapiConnection, record: ConnectionPoolEntry) -> None:
        del record
        # Hand transaction control to SQLAlchemy's "begin" event below (the pysqlite/aiosqlite recipe).
        dbapi_connection.isolation_level = None
        apply_pragmas(dbapi_connection, pragmas)

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(connection: Connection) -> None:
        immediate = connection.get_execution_options().get(BEGIN_OPTION) == "IMMEDIATE"
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")

    return engine


def open_database(path: Path, *, readonly: bool = False) -> Database:
    return Database(create_engine(path, readonly=readonly), readonly=readonly)
