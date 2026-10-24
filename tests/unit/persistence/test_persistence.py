"""persistence: UTC datetimes, JSON text, WAL engine, read-only bridge handle, units of work, insert_or_ignore."""

import asyncio
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, StatementError

from tests.factories import open_test_db
from yordamchi.core.paths import BotName, Paths
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.schema import head_revisions
from yordamchi.persistence.uow import Database, ReadOnlyDatabase
from yordamchi.persistence.upsert import insert_or_ignore
from yordamchi.shared.health.models import HealthEvent, KvState

AT = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


async def _scalar(db: Database, query: str) -> object:
    async with db.engine.connect() as connection:
        return (await connection.exec_driver_sql(query)).scalar()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_the_writer_runs_wal_with_foreign_keys_and_a_0600_file(dev_db: Database, paths: Paths) -> None:
    assert await _scalar(dev_db, "PRAGMA journal_mode") == "wal"
    assert await _scalar(dev_db, "PRAGMA foreign_keys") == 1
    assert await _scalar(dev_db, "PRAGMA busy_timeout") == 5000
    db_file = paths.db_file(BotName.DEV)
    assert _mode(db_file) == 0o600
    assert _mode(db_file.parent) == 0o700


async def test_datetimes_are_stored_as_utc_and_come_back_aware(dev_db: Database) -> None:
    tashkent = timezone(timedelta(hours=5))
    async with dev_db.uow() as s:
        s.add(HealthEvent(bot="dev", at=AT.astimezone(tashkent), kind="started", data_json={"pid": 1}))
    async with dev_db.read() as s:
        row = (await s.scalars(select(HealthEvent))).one()
    assert row.at == AT
    assert row.at.tzinfo is UTC


async def test_a_naive_datetime_is_refused(dev_db: Database) -> None:
    naive = datetime(2026, 10, 23, 9, 0)  # noqa: DTZ001  # the point of the test

    async def write() -> None:
        async with dev_db.uow() as s:
            s.add(HealthEvent(bot="dev", at=naive, kind="started", data_json={}))

    with pytest.raises(StatementError, match="naive"):
        await write()


async def test_json_text_round_trips_nested_uzbek_values(dev_db: Database) -> None:
    value = {"matn": "Ma'lumot: 3 daqiqa oldin", "ids": [1, 2], "ok": True, "nested": {"x": None}}
    async with dev_db.uow() as s:
        s.add(KvState(bot="dev", key="speech_status", value_json=value, updated_at=AT))
    async with dev_db.read() as s:
        row = await s.get(KvState, ("dev", "speech_status"))
    assert row is not None
    assert row.value_json == value


async def test_a_unit_of_work_rolls_back_on_error(dev_db: Database) -> None:
    async def fail_after_write() -> None:
        async with dev_db.uow() as s:
            s.add(KvState(bot="dev", key="k", value_json=1, updated_at=AT))
            await s.flush()
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await fail_after_write()
    async with dev_db.read() as s:
        assert await s.scalar(select(func.count()).select_from(KvState)) == 0


async def test_concurrent_writers_wait_instead_of_failing(dev_db: Database) -> None:
    async def write(n: int) -> None:
        async with dev_db.uow() as s:
            s.add(HealthEvent(bot="dev", at=AT, kind="pressure", data_json={"n": n}))
            await s.flush()
            await asyncio.sleep(0)

    await asyncio.gather(*(write(n) for n in range(10)))
    async with dev_db.read() as s:
        assert await s.scalar(select(func.count()).select_from(HealthEvent)) == 10


async def test_insert_or_ignore_reports_whether_it_inserted(dev_db: Database) -> None:
    values = {"bot": "dev", "key": "pinned_message_id", "value_json": "77", "updated_at": AT}
    async with dev_db.uow() as s:
        assert await insert_or_ignore(s, KvState, values, conflict_columns=("bot", "key")) is True
        assert await insert_or_ignore(s, KvState, values, conflict_columns=("bot", "key")) is False


async def test_the_read_only_handle_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "Application Support" / "db" / "dev.db"  # a space, as in the real data dir
    writer = await open_test_db(path)
    async with writer.uow() as s:
        s.add(KvState(bot="dev", key="k", value_json=1, updated_at=AT))
    reader = open_database(path, readonly=True)
    try:
        async with reader.read() as s:
            assert (await s.get(KvState, ("dev", "k"))) is not None
        assert await _scalar(reader, "PRAGMA query_only") == 1
        with pytest.raises(ReadOnlyDatabase):
            async with reader.uow():
                pass
        async with reader.engine.connect() as connection:
            with pytest.raises(OperationalError, match=r"readonly|read-only"):
                await connection.exec_driver_sql("DELETE FROM kv_state")
    finally:
        await reader.dispose()
        await writer.dispose()


def test_there_is_exactly_one_migration_head() -> None:
    assert head_revisions(ROOT / "migrations") == ("0001",)
