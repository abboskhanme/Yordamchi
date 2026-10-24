"""settings.service: snapshots on real SQLite files, the personal bridge read, versioned writes (SPEC §2.3, §4.3)."""

import logging

import pytest

from tests.factories import add_setting_row, make_owner_origin
from tests.fakes.clock import FakeClock
from yordamchi.core.errors import SettingInvalid, SettingPersonalOnly
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.settings import defaults, service, store
from yordamchi.shared.settings.domain import Source


async def test_a_fresh_dev_db_resolves_to_defaults(dev_deps: DevDeps) -> None:
    snap = await service.snapshot(dev_deps)
    assert snap.version == 0
    assert snap[defaults.TURNS_MAX_CONCURRENT] == 15
    assert snap[defaults.ACCESS_DEV_GROUP_ID] == dev_deps.bootstrap[defaults.ACCESS_DEV_GROUP_ID.key]


async def test_a_recorded_version_applies_to_the_next_snapshot(dev_deps: DevDeps) -> None:
    origin = make_owner_origin(update_id=901)
    assert await service.record_version(dev_deps, origin, defaults.BURST_AFTER_MEDIA_S, 6, reason="change") == 1
    assert await service.record_version(dev_deps, origin, defaults.BURST_AFTER_MEDIA_S, 8, reason="change") == 2
    snap = await service.snapshot(dev_deps)
    assert snap[defaults.BURST_AFTER_MEDIA_S] == 8.0
    assert snap.version == 2
    async with dev_deps.db.read() as s:
        history = await store.list_setting_history(s, defaults.BURST_AFTER_MEDIA_S.key)
    assert [(row.version, row.value_json, row.origin_update_id, row.actor) for row in history] == [
        (2, 8.0, 901, "owner"),
        (1, 6.0, 901, "owner"),
    ]
    assert history[0].created_at == FakeClock().now()


async def test_an_invalid_value_stores_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(SettingInvalid):
        await service.record_version(dev_deps, make_owner_origin(), defaults.BURST_AFTER_MEDIA_S, 999, reason="change")
    assert (await service.snapshot(dev_deps)).version == 0


async def test_the_dev_bot_refuses_a_personal_key(dev_deps: DevDeps) -> None:
    with pytest.raises(SettingPersonalOnly):
        await service.record_version(dev_deps, make_owner_origin(), defaults.PERSONA_PERSONAL, "x", reason="change")


async def test_an_origin_from_the_other_bot_is_a_bug(dev_deps: DevDeps) -> None:
    with pytest.raises(ValueError, match="origin"):
        await service.record_version(
            dev_deps, make_owner_origin(BotName.PERSONAL), defaults.BURST_AFTER_MEDIA_S, 6, reason="change"
        )


async def test_the_personal_bot_reads_shared_dev_values_through_the_read_only_bridge(
    dev_deps: DevDeps, personal_deps: PersonalDeps
) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.CAPS_WARN_PCT, 90, reason="change")
    await service.record_version(dev_deps, make_owner_origin(), defaults.VOICE_ENABLED, True, reason="change")
    snap = await service.snapshot(personal_deps)
    assert (snap[defaults.CAPS_WARN_PCT], snap.source(defaults.CAPS_WARN_PCT)) == (90, Source.DEV)
    assert snap[defaults.VOICE_ENABLED] is True  # its own PerBot default, not the dev row
    assert snap.source(defaults.VOICE_ENABLED) is Source.DEFAULT

    personal_origin = make_owner_origin(BotName.PERSONAL)
    await service.record_version(personal_deps, personal_origin, defaults.CAPS_WARN_PCT, 70, reason="change")
    again = await service.snapshot(personal_deps)
    assert (again[defaults.CAPS_WARN_PCT], again.source(defaults.CAPS_WARN_PCT)) == (70, Source.OWN)
    assert again.version > snap.version


async def test_settings_survive_a_restart(dev_deps: DevDeps) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.SERVERS_HOSTS, ["bonvi-srv"], reason="change")
    await dev_deps.db.dispose()  # the next open is a new process's engine on the same file
    assert (await service.snapshot(dev_deps))[defaults.SERVERS_HOSTS] == ["bonvi-srv"]


async def test_a_row_that_fails_todays_check_is_skipped_and_logged(
    dev_deps: DevDeps, caplog: pytest.LogCaptureFixture
) -> None:
    await add_setting_row(dev_deps.db, defaults.BURST_AFTER_MEDIA_S.key, 500, at=dev_deps.clock.now())
    with caplog.at_level(logging.WARNING):
        snap = await service.snapshot(dev_deps)
    assert snap[defaults.BURST_AFTER_MEDIA_S] == 10.0
    assert any(record.getMessage() == "setting_row_skipped" for record in caplog.records)
