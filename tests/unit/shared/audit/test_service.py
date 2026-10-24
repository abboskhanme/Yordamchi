"""audit.service on a real SQLite file: events join the caller's transaction, data is scrubbed, 90-day retention."""

from datetime import timedelta

import pytest

from tests.factories import FAKE_KEYS
from yordamchi.core import redact
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence import bulk
from yordamchi.shared.audit import service, store
from yordamchi.shared.audit.domain import AuditActor, AuditKind


async def test_an_event_is_appended_in_the_callers_transaction(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        event_id = await service.record_event(
            s,
            bot=dev_deps.bot,
            at=now,
            actor=AuditActor.OWNER,
            kind=AuditKind.SETTING_CHANGED,
            ref_type="setting",
            ref_id="burst.after_media_s",
            data={"version": 2, "origin_update_id": 901},
        )
    async with dev_deps.db.read() as s:
        [row] = await store.list_audit_events(s)
    assert (row.id, row.bot, row.at, row.actor, row.kind) == (event_id, "dev", now, "owner", "setting_changed")
    assert (row.ref_type, row.ref_id) == ("setting", "burst.after_media_s")
    assert row.data_json == {"version": 2, "origin_update_id": 901}


async def test_an_event_rolls_back_with_the_change_it_describes(dev_deps: DevDeps) -> None:
    async def audit_then_fail() -> None:
        async with dev_deps.db.uow() as s:
            await service.record_event(
                s,
                bot=dev_deps.bot,
                at=dev_deps.clock.now(),
                actor=AuditActor.SYSTEM,
                kind=AuditKind.GATE_VERDICT,
                data={},
            )
            raise RuntimeError("change failed")

    with pytest.raises(RuntimeError, match="change failed"):
        await audit_then_fail()
    async with dev_deps.db.read() as s:
        assert await store.list_audit_events(s) == []


async def test_keys_and_vault_values_in_data_are_masked(dev_deps: DevDeps) -> None:
    vault_value = "a-vault-value-of-some-length"
    redact.set_known_secrets([vault_value])
    command = f"curl -H 'Authorization: Bearer {vault_value}' --data {FAKE_KEYS['google_api_key']}"
    async with dev_deps.db.uow() as s:
        await service.record_event(
            s,
            bot=dev_deps.bot,
            at=dev_deps.clock.now(),
            actor=AuditActor.EXECUTOR,
            kind=AuditKind.GATE_VERDICT,
            data={"command": command},
        )
    async with dev_deps.db.read() as s:
        [row] = await store.list_audit_events(s)
    stored = str(row.data_json)
    assert vault_value not in stored
    assert FAKE_KEYS["google_api_key"] not in stored
    assert redact.MASK in stored


async def test_retention_deletes_only_events_older_than_90_days_in_batches(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        for minutes in range(bulk.RETENTION_BATCH + 1):  # more than one batch
            await service.record_event(
                s,
                bot=dev_deps.bot,
                at=now - timedelta(days=91, minutes=minutes),
                actor=AuditActor.SYSTEM,
                kind=AuditKind.SESSION_CLOSED,
                data={},
            )
        await service.record_event(
            s,
            bot=dev_deps.bot,
            at=now - timedelta(days=89),
            actor=AuditActor.SYSTEM,
            kind=AuditKind.SESSION_OPENED,
            data={},
        )
    assert await service.apply_retention(dev_deps) == bulk.RETENTION_BATCH + 1
    async with dev_deps.db.read() as s:
        assert [row.kind for row in await store.list_audit_events(s)] == ["session_opened"]
    assert await service.apply_retention(dev_deps) == 0
