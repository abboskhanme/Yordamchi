"""cost.service on a real SQLite file: usage joins the caller's transaction as integer micro-USD; 400-day retention."""

from datetime import timedelta

import pytest

from tests.factories import make_usage
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.cost import service, store
from yordamchi.shared.cost.domain import Provider


async def test_a_usage_row_is_recorded_in_the_callers_transaction(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    usage = make_usage(project_id="BonviHR", turn_id=17)
    async with dev_deps.db.uow() as s:
        row_id = await service.record_usage(s, bot=dev_deps.bot, at=now, usage=usage)
    async with dev_deps.db.read() as s:
        [row] = await store.list_usage_records(s)
    assert (row.id, row.bot, row.at, row.source, row.provider) == (row_id, "dev", now, "stt", "google")
    assert (row.is_paid_api, row.model, row.project_id, row.turn_id) == (True, "gemini-3.5-transcribe", "BonviHR", 17)
    assert row.cost_usd_micros == 1_250
    assert isinstance(row.cost_usd_micros, int)
    assert (row.units_json, row.duration_ms) == ({"audio_ms": 61_000}, 900)


async def test_an_impossible_usage_writes_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(ValueError, match="not_a_paid_api"):
        async with dev_deps.db.uow() as s:
            await service.record_usage(
                s, bot=dev_deps.bot, at=dev_deps.clock.now(), usage=make_usage(provider=Provider.EDGE)
            )
    async with dev_deps.db.read() as s:
        assert await store.list_usage_records(s) == []


async def test_retention_deletes_only_rows_older_than_400_days(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        await service.record_usage(s, bot=dev_deps.bot, at=now - timedelta(days=401), usage=make_usage(source="old"))
        await service.record_usage(s, bot=dev_deps.bot, at=now - timedelta(days=399), usage=make_usage(source="kept"))
    assert await service.apply_retention(dev_deps) == 1
    async with dev_deps.db.read() as s:
        assert [row.source for row in await store.list_usage_records(s)] == ["kept"]
