"""SQLite connections (CONVENTIONS §8; SPEC §2.3): the pragmas on every connection, and the read-only bridge handle.

- Every write connection (each one in the pool, a fresh one after the pool is reset, the units of work) runs
  `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`; every read-only one runs
  `query_only=ON`, `foreign_keys=ON`, `busy_timeout=5000`. The migration connection runs WAL with the busy timeout.
- The bridge URI (`file:…?mode=ro&uri=true` + `query_only`) refuses every kind of write. The two layers cover each
  other: `query_only` alone still lets `PRAGMA journal_mode` rewrite the file header, and `mode=ro` alone still lets an
  `ATTACH` of the same file write through it.
"""

import sqlite3
import urllib.parse
from contextlib import AsyncExitStack, closing
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.factories import open_test_db
from tests.migrations.harness import SYNTHETIC, alembic_config, read_tables, scratch_chain, synthetic_revision
from yordamchi.persistence.engine import BUSY_TIMEOUT_MS, READ_ONLY_PRAGMAS, apply_pragmas, open_database
from yordamchi.persistence.uow import Database

PRAGMAS: Final = ("journal_mode", "foreign_keys", "busy_timeout", "synchronous", "query_only")
WRITER: Final = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": BUSY_TIMEOUT_MS,
    "synchronous": 1,
    "query_only": 0,
}
READER: Final = {"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": BUSY_TIMEOUT_MS, "query_only": 1}
ROW: Final = "INSERT INTO kv_state (bot, key, value_json, updated_at) VALUES ('dev', 'k', '1', '2026-10-23 09:00:00')"


async def _pragmas(connection: AsyncConnection) -> dict[str, object]:
    return {name: (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar() for name in PRAGMAS}


async def _concurrent(db: Database, count: int = 3) -> list[dict[str, object]]:
    """Hold `count` pooled connections at once, so each is a distinct SQLite connection."""
    async with AsyncExitStack() as stack:
        connections = [await stack.enter_async_context(db.engine.connect()) for _ in range(count)]
        return [await _pragmas(connection) for connection in connections]


async def _dev_db(path: Path) -> None:
    """A migrated-shape `dev.db` with one row, closed again (the dev bot's file the bridge opens)."""
    writer = await open_test_db(path)
    try:
        async with writer.engine.begin() as connection:
            await connection.exec_driver_sql(ROW)
    finally:
        await writer.dispose()


async def test_every_write_connection_runs_wal_foreign_keys_and_a_busy_timeout(tmp_path: Path) -> None:
    db = open_database(tmp_path / "db" / "dev.db")
    try:
        seen = await _concurrent(db)
        await db.engine.dispose()  # a reset pool: the next connection is new and must get them too
        seen += await _concurrent(db, 1)
        async with db.uow() as s:
            seen.append(await _pragmas(await s.connection()))
        async with db.read() as s:
            seen.append(await _pragmas(await s.connection()))
    finally:
        await db.dispose()
    assert seen == [WRITER] * len(seen)
    assert len(seen) == 6


async def test_every_read_only_connection_runs_query_only_foreign_keys_and_a_busy_timeout(tmp_path: Path) -> None:
    path = tmp_path / "db" / "dev.db"
    await _dev_db(path)
    reader = open_database(path, readonly=True)
    try:
        seen = await _concurrent(reader)
        await reader.engine.dispose()
        seen += await _concurrent(reader, 1)
        async with reader.read() as s:
            seen.append(await _pragmas(await s.connection()))
    finally:
        await reader.dispose()
    assert [{name: pragmas[name] for name in READER} for pragmas in seen] == [READER] * 5


PRAGMA_PROBE: Final = """bind = op.get_bind()
seen = {name: bind.exec_driver_sql(f"PRAGMA {name}").scalar() for name in ("journal_mode", "busy_timeout")}
op.get_context().config.attributes["pragmas"] = seen"""


def test_the_migration_connection_runs_wal_with_a_busy_timeout(tmp_path: Path) -> None:
    """The installer and the supervisor migrate while nothing else writes, but a reader (the bridge, `holat?` via the
    socket) may hold the file: WAL lets it read, the busy timeout makes the migration wait instead of failing."""
    location = scratch_chain(tmp_path, {f"{SYNTHETIC}.py": synthetic_revision(PRAGMA_PROBE)})
    config = alembic_config(tmp_path / "db" / "dev.db", script_location=location)
    command.upgrade(config, "head")
    assert config.attributes["pragmas"] == {"journal_mode": "wal", "busy_timeout": BUSY_TIMEOUT_MS}


WRITES: Final = (
    ROW.replace("'k'", "'k2'"),
    "UPDATE kv_state SET value_json = '2'",
    "DELETE FROM kv_state",
    "CREATE TABLE stolen (x INTEGER)",
    "DROP TABLE kv_state",
    "CREATE INDEX ix_stolen ON kv_state (value_json)",
    "PRAGMA user_version = 7",
    "PRAGMA journal_mode = DELETE",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
)


@pytest.mark.parametrize("statement", WRITES, ids=lambda statement: statement.split(" (")[0][:40])
async def test_the_read_only_bridge_refuses_every_write(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "Application Support" / "db" / "dev.db"  # a space, as in the real data dir
    await _dev_db(path)
    before, header = read_tables(path), path.read_bytes()[:100]
    reader = open_database(path, readonly=True)
    try:
        async with reader.engine.connect() as connection:
            with pytest.raises(OperationalError):
                await connection.exec_driver_sql(statement)
            assert (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar() == "wal"
    finally:
        await reader.dispose()
    assert read_tables(path) == before
    assert path.read_bytes()[:100] == header


async def test_the_read_only_bridge_cannot_write_through_an_attach(tmp_path: Path) -> None:
    path = tmp_path / "db" / "dev.db"
    await _dev_db(path)
    before = read_tables(path)
    reader = open_database(path, readonly=True)
    try:
        async with reader.engine.connect() as connection:
            await connection.exec_driver_sql("ATTACH DATABASE ? AS again", (str(path),))
            with pytest.raises(OperationalError, match="readonly"):
                await connection.exec_driver_sql(ROW.replace("kv_state", "again.kv_state").replace("'k'", "'k3'"))
    finally:
        await reader.dispose()
    assert read_tables(path) == before


def _uri(path: Path) -> str:
    return "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro"


async def test_query_only_is_the_layer_that_stops_an_attach(tmp_path: Path) -> None:
    """Why the bridge needs `query_only` on top of `mode=ro`: without it, an ATTACH of the same file writes."""
    path = tmp_path / "db" / "dev.db"
    await _dev_db(path)
    with closing(sqlite3.connect(_uri(path), uri=True, isolation_level=None)) as connection:
        connection.execute("ATTACH DATABASE ? AS again", (str(path),))
        connection.execute(ROW.replace("kv_state", "again.kv_state").replace("'k'", "'k3'"))
    assert len(read_tables(path)["kv_state"].rows) == 2


async def test_mode_ro_is_the_layer_that_keeps_the_header(tmp_path: Path) -> None:
    """Why the bridge needs `mode=ro` on top of `query_only`: without it, a journal-mode change rewrites the file."""
    path = tmp_path / "db" / "dev.db"
    await _dev_db(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        apply_pragmas(connection, READ_ONLY_PRAGMAS)
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
    assert path.read_bytes()[18:20] == b"\x01\x01"  # no longer WAL: the "read-only" connection changed the file


async def test_the_read_only_handle_never_creates_a_missing_db(tmp_path: Path) -> None:
    path = tmp_path / "db" / "dev.db"
    path.parent.mkdir()
    reader = open_database(path, readonly=True)
    try:
        with pytest.raises(OperationalError):
            async with reader.engine.connect():
                pass
    finally:
        await reader.dispose()
    assert not path.exists()


@pytest.mark.parametrize("folder", ["Application Support", "a#b?c%20d", "Maʼlumotlar «dev»"])
async def test_the_read_only_uri_opens_paths_with_uri_characters(tmp_path: Path, folder: str) -> None:
    path = tmp_path / folder / "dev.db"
    await _dev_db(path)
    reader = open_database(path, readonly=True)
    try:
        async with reader.engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT count(*) FROM kv_state")).scalar() == 1
    finally:
        await reader.dispose()
