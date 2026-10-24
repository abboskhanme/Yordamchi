"""vault.service on real SQLite files: ciphertext only, dedupe, audit, open/remove, the masking set and
`secret-hashes.json`, peer visibility, and the 30-day wipe (SPEC §4.5; UC-04; NFR-SEC-02/03/10)."""

import json
import logging
import stat
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.factories import FAKE_KEYS, Fakes, db_file_bytes, make_dev_deps, make_owner_origin
from yordamchi.core import redact
from yordamchi.core.errors import VaultDecryptFailed, VaultKeyMissing, VaultSecretNotFound
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.vault import domain, service
from yordamchi.shared.vault.domain import NewSecret, SecretRef
from yordamchi.shared.vault.models import VaultSecret

GOOGLE = FAKE_KEYS["google_api_key"]
GITHUB = FAKE_KEYS["github_token"]
OTHER_GOOGLE = "AIza" + "SyZ9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J"


async def _add(deps: SharedDeps, *secrets: tuple[str, str], update_id: int = 1) -> tuple[SecretRef, ...]:
    origin = make_owner_origin(deps.bot, update_id=update_id)
    new = [NewSecret(kind=kind, value=value) for kind, value in secrets]
    async with deps.db.uow() as s:
        return await service.add_secrets(s, deps.keystore, origin=origin, secrets=new, at=deps.clock.now())


async def _rows(db: Database) -> list[VaultSecret]:
    async with db.read() as s:
        return list((await s.scalars(select(VaultSecret).order_by(VaultSecret.id))).all())


def _hash_file(paths: Paths) -> dict[str, list[str]]:
    document: dict[str, list[str]] = json.loads(paths.secret_hashes.read_text(encoding="utf-8"))
    return document


@pytest.fixture
async def dev_deps_with_peer(
    dev_db: Database, personal_db: Database, paths: Paths, fakes: Fakes
) -> AsyncIterator[DevDeps]:
    """The dev bot with its read-only view of personal.db (masking only, SPEC §2.3)."""
    del personal_db  # created first, so the file and schema exist
    peer = open_database(paths.db_file(BotName.PERSONAL), readonly=True)
    yield make_dev_deps(db=dev_db, paths=paths, fakes=fakes, peer_db=peer)
    await peer.dispose()


# --- add ------------------------------------------------------------------------------------------------------------
async def test_a_secret_is_stored_as_ciphertext_only(dev_deps: DevDeps, paths: Paths) -> None:
    [ref] = await _add(dev_deps, ("google_api_key", GOOGLE))
    assert (ref.name, ref.kind, ref.last4, ref.bots) == (
        "google_api_key",
        "google_api_key",
        GOOGLE[-4:],
        (BotName.DEV,),
    )
    [row] = await _rows(dev_deps.db)
    assert row.ciphertext is not None
    assert row.nonce is not None
    assert len(row.nonce) == 12
    assert row.key_fp == dev_deps.keystore.fingerprint()
    assert row.origin_update_id == 1
    assert GOOGLE.encode() not in db_file_bytes(paths.db_file(BotName.DEV))
    assert await service.open_secret(dev_deps, "google_api_key") == GOOGLE


async def test_adding_a_secret_is_audited_by_name_only(dev_deps: DevDeps) -> None:
    [ref] = await _add(dev_deps, ("github_token", GITHUB), update_id=77)
    async with dev_deps.db.read() as s:
        [event] = await audit_store.list_audit_events(s, kind="secret_added")
    assert (event.actor, event.ref_type, event.ref_id) == ("owner", "vault_secret", str(ref.id))
    assert event.data_json == {"name": "github_token", "kind": "github_token", "origin_update_id": 77}
    assert GITHUB not in json.dumps(event.data_json)


async def test_the_same_value_is_stored_once(dev_deps: DevDeps) -> None:
    first = await _add(dev_deps, ("google_api_key", GOOGLE), ("google_api_key", GOOGLE))
    again = await _add(dev_deps, ("google_api_key", GOOGLE), update_id=2)
    assert first[0] == first[1] == again[0]
    assert len(await _rows(dev_deps.db)) == 1


async def test_a_second_key_of_the_same_kind_gets_a_numbered_name(dev_deps: DevDeps) -> None:
    refs = await _add(dev_deps, ("google_api_key", GOOGLE), ("google_api_key", OTHER_GOOGLE), ("github_token", GITHUB))
    assert [ref.name for ref in refs] == ["google_api_key", "google_api_key_2", "github_token"]
    assert await service.open_secret(dev_deps, "google_api_key_2") == OTHER_GOOGLE


async def test_nothing_is_written_when_the_master_key_is_unavailable(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.keystore.unavailable = VaultKeyMissing()
    with pytest.raises(VaultKeyMissing):
        await _add(dev_deps, ("google_api_key", GOOGLE))
    assert await _rows(dev_deps.db) == []
    async with dev_deps.db.read() as s:
        assert await audit_store.list_audit_events(s) == []


async def test_a_new_value_is_masked_from_the_moment_it_is_added(dev_deps: DevDeps) -> None:
    await _add(dev_deps, ("generic_secret", FAKE_KEYS["generic_secret"]))
    shown = redact.scrubbed(f"value is {FAKE_KEYS['generic_secret']} here")  # no keyword: masked by value
    assert FAKE_KEYS["generic_secret"] not in shown
    assert redact.MASK in shown


async def test_adding_nothing_writes_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.keystore.unavailable = VaultKeyMissing()  # not even the key is consulted
    assert await _add(dev_deps) == ()


# --- open, list, remove ---------------------------------------------------------------------------------------------
async def test_opening_an_unknown_name_fails(dev_deps: DevDeps) -> None:
    with pytest.raises(VaultSecretNotFound) as raised:
        await service.open_secret(dev_deps, "google_api_key")
    assert raised.value.params == {"name": "google_api_key"}


async def test_a_ciphertext_copied_onto_another_row_does_not_open(dev_deps: DevDeps) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB))
    async with dev_deps.db.uow() as s:
        google, github = (await s.scalars(select(VaultSecret).order_by(VaultSecret.id))).all()
        github.ciphertext, github.nonce = google.ciphertext, google.nonce
    with pytest.raises(VaultDecryptFailed):
        await service.open_secret(dev_deps, "github_token")


async def test_listing_shows_active_secrets_without_values(dev_deps: DevDeps) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB))
    await service.remove_secret(dev_deps, make_owner_origin(update_id=2), "github_token")
    [ref] = await service.list_secrets(dev_deps)
    assert ref.name == "google_api_key"
    assert GOOGLE not in repr(ref)


async def test_removing_a_secret_audits_it_and_closes_it(dev_deps: DevDeps) -> None:
    [added] = await _add(dev_deps, ("google_api_key", GOOGLE))
    removed = await service.remove_secret(dev_deps, make_owner_origin(update_id=9), "google_api_key")
    assert removed.id == added.id
    with pytest.raises(VaultSecretNotFound):
        await service.open_secret(dev_deps, "google_api_key")
    async with dev_deps.db.read() as s:
        [event] = await audit_store.list_audit_events(s, kind="secret_removed")
    assert event.data_json == {"name": "google_api_key", "kind": "google_api_key", "origin_update_id": 9}
    [row] = await _rows(dev_deps.db)
    assert row.removed_at == dev_deps.clock.now()
    assert row.ciphertext is not None  # wiped only after 30 days


async def test_removing_an_unknown_secret_changes_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(VaultSecretNotFound):
        await service.remove_secret(dev_deps, make_owner_origin(), "github_token")
    async with dev_deps.db.read() as s:
        assert await audit_store.list_audit_events(s) == []


async def test_an_origin_from_the_other_bot_cannot_remove(dev_deps: DevDeps) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE))
    with pytest.raises(ValueError, match="origin"):
        await service.remove_secret(dev_deps, make_owner_origin(BotName.PERSONAL), "google_api_key")


async def test_a_removed_name_is_free_again(dev_deps: DevDeps) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE))
    await service.remove_secret(dev_deps, make_owner_origin(update_id=2), "google_api_key")
    [ref] = await _add(dev_deps, ("google_api_key", OTHER_GOOGLE), update_id=3)
    assert ref.name == "google_api_key"
    assert await service.open_secret(dev_deps, "google_api_key") == OTHER_GOOGLE


# --- publish: masking set and secret-hashes.json --------------------------------------------------------------------
async def test_publishing_on_the_dev_bot_masks_every_value_and_writes_the_hash_file(
    dev_deps: DevDeps, paths: Paths
) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB))
    await service.remove_secret(dev_deps, make_owner_origin(update_id=2), "github_token")
    redact.set_known_secrets([])  # as after a restart
    assert await service.publish_secrets(dev_deps) == 2  # a removed key, not yet wiped, stays masked
    shown = redact.scrubbed(f"{GOOGLE[:-1]}|{GOOGLE}|{GITHUB}")
    assert GOOGLE not in shown
    assert GITHUB not in shown
    assert _hash_file(paths) == {"sha256": sorted([domain.sha256_hex(GOOGLE), domain.sha256_hex(GITHUB)])}
    assert stat.S_IMODE(paths.secret_hashes.stat().st_mode) == 0o600
    assert [path.name for path in paths.secret_hashes.parent.iterdir()] == ["secret-hashes.json"]  # no temp left


async def test_the_dev_bot_also_masks_the_personal_bots_secrets(
    dev_deps_with_peer: DevDeps, personal_deps: PersonalDeps, paths: Paths
) -> None:
    await _add(personal_deps, ("google_api_key", GOOGLE))
    redact.set_known_secrets([])
    assert await service.publish_secrets(dev_deps_with_peer) == 1
    assert GOOGLE not in redact.scrubbed(GOOGLE)
    assert _hash_file(paths) == {"sha256": [domain.sha256_hex(GOOGLE)]}


async def test_the_personal_bot_masks_only_dev_secrets_assigned_to_it_and_writes_no_file(
    dev_deps: DevDeps, personal_deps: PersonalDeps, paths: Paths
) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB))
    async with dev_deps.db.uow() as s:
        google = await s.scalar(select(VaultSecret).where(VaultSecret.name == "google_api_key"))
        assert google is not None
        google.bots_json = ["dev", "personal"]
    redact.set_known_secrets([])
    assert await service.publish_secrets(personal_deps) == 1
    assert GOOGLE not in redact.scrubbed(GOOGLE)
    assert redact.known_secret_count() == 1  # the dev-only key is not the personal bot's to hold
    assert not paths.secret_hashes.exists()


async def test_publishing_skips_a_row_that_does_not_open(
    dev_deps: DevDeps, paths: Paths, caplog: pytest.LogCaptureFixture
) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB))
    async with dev_deps.db.uow() as s:
        github = await s.scalar(select(VaultSecret).where(VaultSecret.name == "github_token"))
        assert github is not None
        assert github.ciphertext is not None
        github.ciphertext = bytes([github.ciphertext[0] ^ 1]) + github.ciphertext[1:]
    with caplog.at_level(logging.WARNING):
        assert await service.publish_secrets(dev_deps) == 1
    assert any(record.getMessage() == "secret_unreadable" for record in caplog.records)
    assert _hash_file(paths) == {"sha256": [domain.sha256_hex(GOOGLE)]}


async def test_publishing_without_a_master_key_raises(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _add(dev_deps, ("google_api_key", GOOGLE))
    fakes.keystore.unavailable = VaultKeyMissing()
    with pytest.raises(VaultKeyMissing):
        await service.publish_secrets(dev_deps)


# --- retention ------------------------------------------------------------------------------------------------------
async def test_retention_wipes_a_ciphertext_30_days_after_removal(dev_deps: DevDeps, fakes: Fakes) -> None:
    start = fakes.clock.now()
    await _add(
        dev_deps, ("google_api_key", GOOGLE), ("github_token", GITHUB), ("openai_api_key", FAKE_KEYS["openai_api_key"])
    )
    await service.remove_secret(dev_deps, make_owner_origin(update_id=2), "github_token")
    fakes.clock.set(start + timedelta(days=20))
    await service.remove_secret(dev_deps, make_owner_origin(update_id=3), "openai_api_key")
    fakes.clock.set(start + timedelta(days=31))

    assert await service.apply_retention(dev_deps) == 1
    by_name = {row.name: row for row in await _rows(dev_deps.db)}
    assert (by_name["github_token"].ciphertext, by_name["github_token"].nonce) == (None, None)
    assert by_name["github_token"].last4 == GITHUB[-4:]  # the audit trail keeps name and mask
    assert by_name["openai_api_key"].ciphertext is not None  # removed 11 days ago
    assert by_name["google_api_key"].ciphertext is not None  # active
    assert await service.apply_retention(dev_deps) == 0


async def test_a_wiped_secret_is_no_longer_published(dev_deps: DevDeps, fakes: Fakes, paths: Paths) -> None:
    await _add(dev_deps, ("github_token", GITHUB))
    await service.remove_secret(dev_deps, make_owner_origin(update_id=2), "github_token")
    fakes.clock.set(fakes.clock.now() + timedelta(days=31))
    await service.apply_retention(dev_deps)
    assert await service.publish_secrets(dev_deps) == 0
    assert _hash_file(paths) == {"sha256": []}
