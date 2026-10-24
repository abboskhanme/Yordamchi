"""inbox.service on a real SQLite file with fakes: exactly-once intake, keys vaulted before any write, the delete and
the masked reply, refused Claude tokens, the ack, the durable queue, the polling offset and retention
(SPEC §3.2.1, §2.7, §4.2, §4.5, §4.6; UC-04; NFR-REL-05, NFR-SEC-02/03).

The ack is queued in the outbox inside intake's transaction; `_accept` runs one sender pass after it, as
`outbox.run_sender` would within 0.25 s."""

import asyncio
import json
import logging
import time
from datetime import timedelta

import pytest

from tests.factories import (
    CLAUDE_LOGIN_TOKEN,
    FAKE_KEYS,
    UNKNOWN_FORMAT_KEY,
    Fakes,
    db_file_bytes,
    drain_outbox,
    make_burst_row,
    make_owner_origin,
    make_update,
    origin_of,
    send_outbox,
)
from tests.fakes.messenger import Request
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, RateLimited, VaultKeyMissing
from yordamchi.core.log import JsonFormatter
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.messenger import ChatAction
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.inbox import service, store
from yordamchi.shared.inbox.domain import Accepted, Attachment, IncomingUpdate, UpdateKind, UpdateStatus
from yordamchi.shared.inbox.models import InboundUpdate
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.vault import domain as vault_domain
from yordamchi.shared.vault import service as vault

GOOGLE = FAKE_KEYS["google_api_key"]
ACK_TIMEOUT = outbox_domain.DEFAULT_CONFIG.ack_timeout_s
SEEN = defaults.UI_SEEN_REACTION.default


async def _accept(deps: DevDeps | PersonalDeps, update: IncomingUpdate) -> Accepted:
    accepted = await service.accept_update(deps, origin_of(update), update)
    await send_outbox(deps)
    return accepted


async def _rows(deps: DevDeps | PersonalDeps) -> list[InboundUpdate]:
    async with deps.db.read() as s:
        return await store.list_inbound_updates(s, deps.bot.value)


def _stored_text(row: InboundUpdate) -> object:
    assert isinstance(row.payload_json, dict)
    return row.payload_json["text"]


def _calls(fakes: Fakes) -> list[tuple[str, dict[str, object]]]:
    return [(request.method, request.payload) for request in fakes.messenger.requests]


def _everything_sent(requests: list[Request]) -> str:
    return json.dumps([request.payload for request in requests], default=str, ensure_ascii=False)


# --- the happy path -------------------------------------------------------------------------------------------------
async def test_a_text_update_is_stored_received_and_acknowledged(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(1, text="BonviHR'da login sahifasini tuzat")
    accepted = await _accept(dev_deps, update)

    assert accepted == Accepted(update_id=1, duplicate=False, status=UpdateStatus.RECEIVED)
    [row] = await _rows(dev_deps)
    assert (row.bot, row.update_id, row.chat_id, row.user_id, row.message_id) == (
        "dev",
        1,
        update.chat_id,
        update.user_id,
        update.message_id,
    )
    assert (row.kind, row.status, row.synthetic, row.received_at) == ("text", "received", False, fakes.clock.now())
    assert row.payload_json == {
        "text": "BonviHR'da login sahifasini tuzat",
        "caption": None,
        "attachment": None,
        "secret_ids": [],
    }
    assert sorted(_calls(fakes), key=lambda call: call[0]) == [
        ("chat_action", {"action": ChatAction.TYPING, "timeout": ACK_TIMEOUT}),
        ("react", {"message_id": update.message_id, "emoji": SEEN, "timeout": ACK_TIMEOUT}),
    ]


async def test_the_seen_reaction_is_read_from_settings_at_use_time(dev_deps: DevDeps, fakes: Fakes) -> None:
    origin = make_owner_origin(update_id=900)
    await settings.record_version(dev_deps, origin, defaults.UI_SEEN_REACTION, "\U0001f44d", reason="change")
    await _accept(dev_deps, make_update(2))
    [react] = fakes.messenger.sent("react")
    assert react.payload["emoji"] == "\U0001f44d"
    assert {request.payload["timeout"] for request in fakes.messenger.requests} == {ACK_TIMEOUT}


async def test_the_ack_goes_out_well_within_one_second(dev_deps: DevDeps, fakes: Fakes) -> None:
    started = time.monotonic()
    await _accept(dev_deps, make_update(3))
    assert time.monotonic() - started < 1.0  # NFR SPD ack ≤ 1 s, on a real SQLite file
    assert {request.method for request in fakes.messenger.requests} == {"react", "chat_action"}
    assert fakes.clock.sleeps == []  # nothing waits before the ack


# --- exactly once (NFR-REL-05) --------------------------------------------------------------------------------------
async def test_a_redelivered_update_is_processed_exactly_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(4, text=f"kalit: {GOOGLE}")
    first = await _accept(dev_deps, update)
    second = await _accept(dev_deps, update)

    assert (first.duplicate, second) == (False, Accepted(update_id=4, duplicate=True))
    assert len(await _rows(dev_deps)) == 1
    assert len(await vault.list_secrets(dev_deps)) == 1
    assert [request.method for request in fakes.messenger.requests].count("delete") == 1
    assert len(fakes.messenger.sent("send_text")) == 1
    async with dev_deps.db.read() as s:
        assert len(await audit_store.list_audit_events(s, kind="secret_added")) == 1


async def test_concurrent_deliveries_of_one_update_are_processed_exactly_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(5, text=f"kalit: {GOOGLE}")
    results = await asyncio.gather(*(_accept(dev_deps, update) for _ in range(3)))
    assert sorted(result.duplicate for result in results) == [False, True, True]
    assert len(await _rows(dev_deps)) == 1
    assert len(await vault.list_secrets(dev_deps)) == 1
    assert len(fakes.messenger.sent("delete")) == 1


async def test_the_personal_bot_takes_updates_into_its_own_db(dev_deps: DevDeps, personal_deps: PersonalDeps) -> None:
    await _accept(dev_deps, make_update(6))
    accepted = await _accept(personal_deps, make_update(6, bot=BotName.PERSONAL))
    assert not accepted.duplicate
    [row] = await _rows(personal_deps)
    assert (row.bot, row.chat_id) == ("personal", make_update(6, bot=BotName.PERSONAL).chat_id)


# --- keys (UC-04) ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("kind", "value"), sorted(FAKE_KEYS.items()), ids=sorted(FAKE_KEYS))
async def test_each_key_format_is_vaulted_deleted_and_answered_masked(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, kind: str, value: str
) -> None:
    update = make_update(7, text=f"mana kalit: {value} saqlab qo'y")
    accepted = await _accept(dev_deps, update)

    [ref] = accepted.secrets
    assert (ref.name, ref.kind, accepted.status) == (kind, kind, UpdateStatus.RECEIVED)
    masked = f"{kind} {redact.MASK}{value[-4:]}"
    # Stored: only the redacted text, and the key only as ciphertext.
    [row] = await _rows(dev_deps)
    assert row.payload_json == {
        "text": f"mana kalit: [kalit: {masked}] saqlab qo'y",
        "caption": None,
        "attachment": None,
        "secret_ids": [ref.id],
    }
    assert value.encode() not in db_file_bytes(paths.db_file(BotName.DEV))
    assert await vault.open_secret(dev_deps, kind) == value
    # Shown: the message deleted, then one masked reply; no reaction on a message that is gone.
    assert fakes.messenger.sent("delete")[0].payload["message_id"] == update.message_id
    [reply] = fakes.messenger.sent("send_text")
    assert reply.payload["text"] == S.SECRETS_STORED(keys=masked)
    assert fakes.messenger.sent("react") == []
    assert value not in _everything_sent(fakes.messenger.requests)
    # The pre-push scan sees its hash.
    hashes = json.loads(paths.secret_hashes.read_text(encoding="utf-8"))
    assert vault_domain.sha256_hex(value) in hashes["sha256"]


async def test_a_key_in_a_caption_is_handled_like_one_in_the_text(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths
) -> None:
    photo = Attachment(file_id="AgAD-photo", mime="image/jpeg", size_bytes=2048)
    update = make_update(8, text=None, caption=f"shu kalit bilan: {GOOGLE}", attachment=photo)
    accepted = await _accept(dev_deps, update)
    [row] = await _rows(dev_deps)
    assert row.kind == UpdateKind.PHOTO.value
    assert row.payload_json == {
        "text": None,
        "caption": f"shu kalit bilan: [kalit: google_api_key {accepted.secrets[0].masked}]",
        "attachment": {
            "file_id": "AgAD-photo",
            "mime": "image/jpeg",
            "size_bytes": 2048,
            "duration_s": None,
            "file_name": None,
        },
        "secret_ids": [accepted.secrets[0].id],
    }
    assert GOOGLE.encode() not in db_file_bytes(paths.db_file(BotName.DEV))
    assert len(fakes.messenger.sent("delete")) == 1


async def test_a_claude_login_token_is_refused_deleted_and_never_stored(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths
) -> None:
    accepted = await _accept(dev_deps, make_update(9, text=f"mana token: {CLAUDE_LOGIN_TOKEN}"))

    assert (accepted.refused, accepted.secrets, accepted.status) == (1, (), UpdateStatus.RECEIVED)
    [row] = await _rows(dev_deps)
    assert _stored_text(row) == "mana token: [rad etildi]"
    assert await vault.list_secrets(dev_deps) == ()
    assert redact.known_secret_count() == 0  # not even held in memory
    assert CLAUDE_LOGIN_TOKEN.encode() not in db_file_bytes(paths.db_file(BotName.DEV))
    assert len(fakes.messenger.sent("delete")) == 1
    assert [request.payload["text"] for request in fakes.messenger.sent("send_text")] == [S.CLAUDE_TOKEN_REFUSED()]
    assert not paths.secret_hashes.exists()


async def test_an_unknown_format_key_never_appears_in_a_reply_or_a_log(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    """UC-04 AC: not detectable, so it is stored as text — but logs carry no content and replies echo none."""
    text = f"bu {UNKNOWN_FORMAT_KEY} ni eslab qol, mana kalit ham: {GOOGLE}"
    formatter = JsonFormatter(process="dev", version="test")
    with caplog.at_level(logging.INFO):  # the production level (core.log.configure)
        await _accept(dev_deps, make_update(10, text=text))
        await _accept(dev_deps, make_update(10, text=text))  # the duplicate path logs too

    lines = [formatter.format(record) for record in caplog.records]
    assert any('"event": "update_accepted"' in line for line in lines)
    assert all(UNKNOWN_FORMAT_KEY not in line and GOOGLE not in line for line in lines)
    assert fakes.messenger.sent("send_text")  # a reply was sent, and it carries neither value
    sent = _everything_sent(fakes.messenger.requests)
    assert UNKNOWN_FORMAT_KEY not in sent
    assert GOOGLE not in sent


async def test_a_message_that_cannot_be_deleted_asks_to_rotate_the_key(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(11, text=f"kalit: {GOOGLE}")
    assert update.message_id is not None
    fakes.messenger.undeletable.add((update.chat_id, update.message_id))
    await _accept(dev_deps, update)
    [reply] = fakes.messenger.sent("send_text")
    assert str(reply.payload["text"]).endswith(S.SECRET_NOT_DELETED())


async def test_a_delete_call_that_keeps_failing_ends_in_the_rotate_advice(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(outbox_domain.ACK_MAX_ATTEMPTS):
        fakes.messenger.fail_next("delete", CallTimedOut(operation="delete"))
    with caplog.at_level(logging.WARNING):
        accepted = await _accept(dev_deps, make_update(12, text=f"kalit: {GOOGLE}"))
        assert accepted.status is UpdateStatus.RECEIVED
        assert fakes.messenger.sent("send_text") == []  # the reply waits for the delete's outcome
        await drain_outbox(dev_deps, fakes.clock, seconds=10)
    [reply] = fakes.messenger.sent("send_text")
    assert S.SECRET_NOT_DELETED() in str(reply.payload["text"])
    assert {record.getMessage() for record in caplog.records} >= {"outbox_call_failed", "outbox_gave_up"}


async def test_a_delete_call_that_fails_once_is_retried_and_the_reply_stays_plain(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    fakes.messenger.fail_next("delete", CallTimedOut(operation="delete"))
    update = make_update(19, text=f"kalit: {GOOGLE}")
    accepted = await _accept(dev_deps, update)
    await drain_outbox(dev_deps, fakes.clock, seconds=2)
    [delete] = fakes.messenger.sent("delete")
    assert delete.payload["message_id"] == update.message_id
    [reply] = fakes.messenger.sent("send_text")
    assert reply.payload["text"] == S.SECRETS_STORED(keys=f"google_api_key {accepted.secrets[0].masked}")


async def test_without_a_master_key_the_update_fails_safely(dev_deps: DevDeps, fakes: Fakes, paths: Paths) -> None:
    fakes.keystore.unavailable = VaultKeyMissing()
    accepted = await _accept(dev_deps, make_update(13, text=f"kalit: {GOOGLE}"))

    assert (accepted.status, accepted.secrets) == (UpdateStatus.FAILED, ())
    [row] = await _rows(dev_deps)
    assert row.status == "failed"
    assert _stored_text(row) == "kalit: [kalit saqlanmadi]"
    assert GOOGLE.encode() not in db_file_bytes(paths.db_file(BotName.DEV))
    assert len(fakes.messenger.sent("delete")) == 1  # the key still leaves the chat
    assert [request.payload["text"] for request in fakes.messenger.sent("send_text")] == [S.ERR_VAULT_KEY_MISSING()]
    assert fakes.messenger.sent("chat_action") == []  # nothing is pending


# --- the ack is durable with the update (T15 closed the crash-after-commit gap) -------------------------------------
async def test_a_crash_after_the_commit_still_deletes_the_key_message(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(26, text=f"kalit: {GOOGLE}")
    accepted = await service.accept_update(dev_deps, origin_of(update), update)
    assert fakes.messenger.requests == []  # the process dies here, before any ack call
    await dev_deps.db.dispose()  # the next start is a new engine on the same file

    assert await outbox.recover_queue(dev_deps) == 0
    await send_outbox(dev_deps)
    assert [request.method for request in fakes.messenger.requests] == ["delete", "send_text", "chat_action"]
    assert fakes.messenger.sent("delete")[0].payload["message_id"] == update.message_id
    assert fakes.messenger.sent("send_text")[0].payload["text"] == S.SECRETS_STORED(
        keys=f"google_api_key {accepted.secrets[0].masked}"
    )


async def test_a_crash_during_the_delete_call_sends_it_again_after_the_restart(dev_deps: DevDeps, fakes: Fakes) -> None:
    update = make_update(27, text=f"kalit: {GOOGLE}")
    await service.accept_update(dev_deps, origin_of(update), update)
    async with dev_deps.db.uow() as s:  # the dead sender had claimed the delete; whether it reached Telegram is unknown
        delete_row = await outbox_store.find_outbox_message(s, "ack:update-27:delete")
        assert delete_row is not None
        assert await outbox_store.claim_message(s, delete_row.id, at=fakes.clock.now()) is not None
    await dev_deps.db.dispose()

    assert await outbox.recover_queue(dev_deps) == 1  # SPEC §2.7: sending → resent
    await send_outbox(dev_deps)
    assert [request.method for request in fakes.messenger.requests][:2] == ["delete", "send_text"]


# --- the ack never fails intake -------------------------------------------------------------------------------------
async def test_a_failed_reaction_does_not_fail_intake(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("react", RateLimited(provider="telegram", retry_after=3))
    accepted = await _accept(dev_deps, make_update(14))
    assert accepted.status is UpdateStatus.RECEIVED
    assert len(await _rows(dev_deps)) == 1
    assert [request.method for request in fakes.messenger.requests] == ["chat_action"]


async def test_an_origin_for_another_update_is_refused_before_anything_is_written(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    update = make_update(15, text=f"kalit: {GOOGLE}")
    with pytest.raises(ValueError, match="does not match"):
        await service.accept_update(dev_deps, make_owner_origin(update_id=16), update)
    assert await _rows(dev_deps) == []
    assert await vault.list_secrets(dev_deps) == ()
    assert fakes.messenger.requests == []


async def test_a_membership_update_is_kept_as_ignored_without_any_ack(dev_deps: DevDeps, fakes: Fakes) -> None:
    accepted = await _accept(dev_deps, make_update(17, text=None, kind=UpdateKind.MEMBER))
    assert accepted.status is UpdateStatus.IGNORED
    assert fakes.messenger.requests == []


async def test_a_synthetic_update_only_shows_typing(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _accept(dev_deps, make_update(18, text="selftest", synthetic=True))
    assert [request.method for request in fakes.messenger.requests] == ["chat_action"]
    [row] = await _rows(dev_deps)
    assert row.synthetic is True


# --- the durable queue (SPEC §2.7) ----------------------------------------------------------------------------------
async def test_received_updates_are_read_in_update_id_order_and_attached_once(dev_deps: DevDeps) -> None:
    for update_id in (23, 21, 22):
        await _accept(dev_deps, make_update(update_id, text=f"#{update_id} kalit: {GOOGLE}"))
    await _accept(dev_deps, make_update(24, text=None, kind=UpdateKind.MEMBER))  # ignored: not in the queue

    waiting = await service.list_received_updates(dev_deps, limit=10)
    assert [item.update_id for item in waiting] == [21, 22, 23]
    first = waiting[0]
    assert first.content.text is not None
    assert first.content.text.startswith("#21 kalit: [kalit: google_api_key ")
    assert GOOGLE not in first.content.text
    assert (first.kind, first.status, first.content.secret_ids) == (UpdateKind.TEXT, UpdateStatus.RECEIVED, (1,))

    async with dev_deps.db.uow() as s:  # `burst_id` is a real foreign key into `bursts`
        first_burst, second_burst = await make_burst_row(s), await make_burst_row(s)
    async with dev_deps.db.uow() as s:
        assert await service.mark_updates_attached(s, bot=dev_deps.bot, update_ids=[21, 22], burst_id=first_burst) == 2
    async with dev_deps.db.uow() as s:
        assert await service.mark_updates_attached(s, bot=dev_deps.bot, update_ids=[21, 24], burst_id=second_burst) == 0
    assert [item.update_id for item in await service.list_received_updates(dev_deps, limit=10)] == [23]
    by_id = {row.update_id: row for row in await _rows(dev_deps)}
    assert (by_id[21].status, by_id[21].burst_id, by_id[24].status) == ("attached", first_burst, "ignored")


async def test_the_queue_survives_a_restart(dev_deps: DevDeps) -> None:
    await _accept(dev_deps, make_update(25))
    await dev_deps.db.dispose()  # the next open is a new process's engine on the same file
    assert [item.update_id for item in await service.list_received_updates(dev_deps, limit=10)] == [25]


# --- polling offset -------------------------------------------------------------------------------------------------
async def test_the_polling_offset_moves_past_each_batch_and_never_back(dev_deps: DevDeps) -> None:
    assert await service.get_poll_offset(dev_deps) == 0
    assert await service.advance_poll_offset(dev_deps, []) == 0
    assert await service.advance_poll_offset(dev_deps, [5, 3]) == 6
    assert await service.advance_poll_offset(dev_deps, [2]) == 6
    await dev_deps.db.dispose()
    assert await service.get_poll_offset(dev_deps) == 6


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def test_retention_deletes_finished_rows_after_30_days_and_nulls_payloads_after_7(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    start = fakes.clock.now()
    fakes.clock.set(start - timedelta(days=31))
    await _accept(dev_deps, make_update(31))  # attached below → deleted
    await _accept(dev_deps, make_update(32))  # still received → untouched, however old
    fakes.clock.set(start - timedelta(days=8))
    await _accept(dev_deps, make_update(33, text=None, kind=UpdateKind.MEMBER))  # ignored → payload nulled
    fakes.clock.set(start - timedelta(days=3))
    await _accept(dev_deps, make_update(34))  # attached, recent → untouched
    async with dev_deps.db.uow() as s:
        burst_id = await make_burst_row(s)
        await service.mark_updates_attached(s, bot=dev_deps.bot, update_ids=[31, 34], burst_id=burst_id)
    fakes.clock.set(start)

    assert await service.apply_retention(dev_deps) == 2
    by_id = {row.update_id: row for row in await _rows(dev_deps)}
    assert sorted(by_id) == [32, 33, 34]
    assert by_id[32].payload_json is not None
    assert by_id[33].payload_json is None
    assert by_id[34].payload_json is not None
    assert await service.apply_retention(dev_deps) == 0


# --- origin_of_update (T34: work done after the handler returned) -----------------------------------------------
async def test_a_stored_owner_update_gets_its_origin_back(dev_deps: DevDeps) -> None:
    update = make_update(41)
    await service.accept_update(dev_deps, origin_of(update), update)
    assert await service.origin_of_update(dev_deps, 41) == origin_of(update)


async def test_an_unknown_synthetic_or_no_longer_admitted_update_has_no_origin(dev_deps: DevDeps, fakes: Fakes) -> None:
    from tests.factories import add_setting_row

    synthetic = make_update(42, synthetic=True)
    await service.accept_update(dev_deps, origin_of(synthetic), synthetic)
    plain = make_update(43)
    await service.accept_update(dev_deps, origin_of(plain), plain)
    assert await service.origin_of_update(dev_deps, 999) is None
    assert await service.origin_of_update(dev_deps, 42) is None
    await add_setting_row(dev_deps.db, "access.owner_id", 777, at=fakes.clock.now())  # the owner changed since
    assert await service.origin_of_update(dev_deps, 43) is None
