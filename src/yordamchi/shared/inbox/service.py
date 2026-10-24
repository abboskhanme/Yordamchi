"""Inbound intake (SPEC §3.2.1, §2.7, §4.6; UC-04; NFR-REL-05, NFR-SEC-03) — the reference feature (CONVENTIONS §4).

`accept_update` is the one service call a Telegram handler makes (A7). In one unit of work it writes the
`(bot, update_id)` idempotency key, moves every key into the vault, stores only the redacted payload and queues the
ack in the outbox. Later stages pick `received` rows up in `update_id` order with `list_received_updates` — the
normal path and the restart path (SPEC §2.7) are the same query.

The ack is priority-0 outbox rows written in that same transaction, so it is exactly as durable as the update: a
crash after the commit still deletes a key message once the sender runs again. A message holding a key is deleted
first (UC-04: ≤ 5 s); the masked reply waits for that delete and says "kalitni almashtiring" if it failed. Typing and
the "seen" reaction go out of band. The sender (`outbox.service.run_sender`) makes every call, bounded and retried.
"""

import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from yordamchi.core import redact
from yordamchi.core.errors import DomainError
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.keystore import Keystore
from yordamchi.shared.access import domain as access_domain
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import Admit, Arrival, ChatKind, EventType, OwnerOrigin
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.inbox import domain, store
from yordamchi.shared.inbox.domain import (
    Accepted,
    AckPlan,
    IncomingUpdate,
    SecretScan,
    StoredUpdate,
    UpdateKind,
    UpdateStatus,
)
from yordamchi.shared.inbox.models import InboundUpdate
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind, Op, op_key
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import SecretRef

log = get_log(__name__)


# --- intake -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Recorded:
    status: UpdateStatus
    stored: tuple[SecretRef, ...]
    vault_error: DomainError | None


async def _record(
    s: Session,
    keystore: Keystore,
    *,
    origin: OwnerOrigin,
    update: IncomingUpdate,
    scan: SecretScan,
    at: datetime,
    reaction: str,
    max_parts: int,
) -> _Recorded | None:
    """Inside the caller's unit of work: the idempotency key, then the vault, then the redacted payload, then the ack.

    Returns None when `(bot, update_id)` was already there: a redelivered update changes nothing and is not acked
    again.
    """
    row_id = await store.add_inbound_update(
        s,
        bot=update.bot.value,
        update_id=update.update_id,
        chat_id=update.chat_id,
        user_id=update.user_id,
        message_id=update.message_id,
        kind=update.kind.value,
        media_group_id=update.media_group_id,
        reply_to_message_id=update.reply_to_message_id,
        synthetic=update.synthetic,
        status=UpdateStatus.RECEIVED.value,
        received_at=at,
    )
    if row_id is None:
        return None
    to_store = scan.to_store()
    stored: tuple[SecretRef, ...] = ()
    vault_error: DomainError | None = None
    try:
        stored = await vault.add_secrets(s, keystore, origin=origin, secrets=to_store, at=at)
    except vault.KEY_UNAVAILABLE as error:  # raised before the vault wrote anything
        vault_error = error
    by_value = {} if vault_error is not None else dict(zip((secret.value for secret in to_store), stored, strict=True))
    status = domain.initial_status(update, vault_failed=vault_error is not None)
    content = domain.redacted_content(update, scan, by_value)
    payload = domain.payload_json(content, redact.scrubbed)
    await store.set_update_payload(s, row_id, payload=payload, status=status.value)
    plan = domain.plan_ack(update, scan, status=status, stored=stored, vault_error=vault_error)
    await _enqueue_ack(s, update, plan, reaction=reaction, max_parts=max_parts, at=at)
    return _Recorded(status=status, stored=stored, vault_error=vault_error)


async def _enqueue_ack(
    s: Session, update: IncomingUpdate, plan: AckPlan, *, reaction: str, max_parts: int, at: datetime
) -> None:
    """The ack as priority-0 outbox rows (SPEC §3.2.1 step 3), in the order the sender takes them: the delete, the
    reply that waits for it, typing, the "seen" reaction."""
    bot, chat_id, ref = update.bot, plan.chat_id, f"update-{update.update_id}"
    if plan.delete_message and plan.message_id is not None:
        await outbox.enqueue_delete(s, bot=bot, chat_id=chat_id, message_id=plan.message_id, ref=ref, at=at)
        # Always queued: if the delete fails, the reply is at least "kalitni almashtiring".
        await outbox.enqueue_text_after(
            s,
            bot=bot,
            chat_id=chat_id,
            after=op_key(Kind.ACK, ref, Op.DELETE),
            text=domain.ack_reply(plan, deleted=True),
            text_if_after_failed=domain.ack_reply(plan, deleted=False),
            kind=Kind.ACK,
            ref=ref,
            at=at,
        )
    elif (reply := domain.ack_reply(plan, deleted=True)) is not None:
        await outbox.enqueue_text(
            s, bot=bot, chat_id=chat_id, text=reply, kind=Kind.ACK, ref=ref, at=at, max_parts=max_parts
        )
    if plan.typing:
        await outbox.request_typing(s, bot=bot, chat_id=chat_id, ref=ref, at=at)
    if plan.react and plan.message_id is not None:
        await outbox.enqueue_reaction(
            s, bot=bot, chat_id=chat_id, message_id=plan.message_id, emoji=reaction, ref=ref, at=at
        )


async def accept_update(deps: SharedDeps, origin: OwnerOrigin, update: IncomingUpdate) -> Accepted:
    """Take one owner update in: processed exactly once per `(bot, update_id)`, keys vaulted before any write.

    `origin` is the proof from `shared/access` that this is the owner's update; a mismatch is a wiring bug and
    raises `ValueError` before anything is written. A missing master key does not raise: the update is stored as
    `failed` without its keys, the message is still deleted, and the owner is told what to fix. The ack is queued
    with the update and sent by the outbox, so no Telegram call happens here. Only a failure to write
    `secret-hashes.json` (a disk problem) raises, after the commit.
    """
    started = time.monotonic()
    domain.require_matching_origin(origin, update, deps.bot)
    snap = await settings.snapshot(deps)
    scan = domain.scan_secrets(update)  # in memory only: nothing is written while a key is still in the text
    now = deps.clock.now()
    async with deps.db.uow() as s:
        recorded = await _record(
            s,
            deps.keystore,
            origin=origin,
            update=update,
            scan=scan,
            at=now,
            reaction=snap[defaults.UI_SEEN_REACTION],
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
    if recorded is None:
        log.info("update_duplicate", bot=deps.bot.value, update_id=update.update_id)
        return Accepted(update_id=update.update_id, duplicate=True)
    if recorded.vault_error is not None:
        log.warning("vault_unavailable", update_id=update.update_id, error_code=recorded.vault_error.code.value)

    if recorded.stored:
        # After the commit: refresh secret-hashes.json for the pre-push scan.
        await vault.publish_secrets(deps)
    log.info(
        "update_accepted",
        bot=deps.bot.value,
        update_id=update.update_id,
        kind=update.kind.value,
        status=recorded.status.value,
        secrets=len(recorded.stored),
        refused=scan.refused,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return Accepted(
        update_id=update.update_id,
        duplicate=False,
        status=recorded.status,
        secrets=recorded.stored,
        refused=scan.refused,
    )


# --- the durable queue: read side ---------------------------------------------------------------------------------
def _stored_update(row: InboundUpdate) -> StoredUpdate:
    return StoredUpdate(
        update_id=row.update_id,
        chat_id=row.chat_id,
        kind=UpdateKind(row.kind),
        status=UpdateStatus(row.status),
        user_id=row.user_id,
        message_id=row.message_id,
        media_group_id=row.media_group_id,
        reply_to_message_id=row.reply_to_message_id,
        synthetic=row.synthetic,
        received_at=row.received_at,
        content=domain.content_from_json(row.payload_json),
    )


async def list_received_updates(deps: SharedDeps, *, limit: int) -> tuple[StoredUpdate, ...]:
    """Updates not yet attached to a burst, oldest `update_id` first: the live path and the restart path."""
    async with deps.db.read() as s:
        rows = await store.list_updates_with_status(s, deps.bot.value, UpdateStatus.RECEIVED.value, limit=limit)
    return tuple(_stored_update(row) for row in rows)


async def origin_of_update(deps: SharedDeps, update_id: int) -> OwnerOrigin | None:
    """The `OwnerOrigin` of an update intake stored, judged again by today's access policy: for work done after the
    handler returned (a burst routed later, or after a restart). Intake stores an update only with the origin minted
    for it, so its ids are an owner update's; a changed `access.*` setting since then means no origin. A synthetic
    update (the self-test) or an unknown one has none."""
    async with deps.db.read() as s:
        row = await store.get_inbound_update(s, deps.bot.value, update_id)
    if row is None or row.synthetic:
        return None
    arrival = Arrival(
        bot=deps.bot,
        update_id=row.update_id,
        event=EventType.EDITED_MESSAGE if row.kind == UpdateKind.EDITED.value else EventType.MESSAGE,
        user_id=row.user_id,
        chat_id=row.chat_id,
        chat_kind=ChatKind.PRIVATE if row.chat_id > 0 else ChatKind.SUPERGROUP,
    )
    verdict = access_domain.admit(arrival, await access.load_policy(deps))
    return verdict.origin if isinstance(verdict, Admit) else None


async def mark_updates_attached(s: Session, *, bot: BotName, update_ids: Collection[int], burst_id: int) -> int:
    """In the caller's (the burst's) transaction: `received` → `attached`. Other rows stay as they are."""
    return await store.set_update_status(
        s,
        bot=bot.value,
        update_ids=update_ids,
        from_status=UpdateStatus.RECEIVED.value,
        to_status=UpdateStatus.ATTACHED.value,
        burst_id=burst_id,
    )


# --- polling offset -----------------------------------------------------------------------------------------------
async def get_poll_offset(deps: SharedDeps) -> int:
    """The `getUpdates` offset to poll from; 0 before the first batch."""
    async with deps.db.read() as s:
        row = await store.get_poll_offset(s, deps.bot.value)
    return 0 if row is None else row.next_offset


async def advance_poll_offset(deps: SharedDeps, update_ids: Collection[int]) -> int:
    """After a batch was fed: persist `max(update_id) + 1`, never moving backwards. Returns the offset now stored."""
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.get_poll_offset(s, deps.bot.value)
        current = 0 if row is None else row.next_offset
        offset = domain.next_offset(current, update_ids)
        if offset != current:
            await store.set_poll_offset(s, bot=deps.bot.value, next_offset=offset, at=now)
    return offset


# --- retention ----------------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """SPEC §4.2: finished rows are deleted after 30 days and their payload nulled after 7, in batches. A `received`
    row is never touched. Returns how many rows changed."""
    now = deps.clock.now()
    finished = sorted(status.value for status in domain.TERMINAL)
    row_cutoff, payload_cutoff = domain.row_cutoff(now), domain.payload_cutoff(now)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_updates_before(s, row_cutoff, statuses=finished, limit=limit)
    )
    cleared = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.clear_payloads_before(s, payload_cutoff, statuses=finished, limit=limit)
    )
    log.info("inbox_retention_applied", rows_deleted=deleted, payloads_cleared=cleared)
    return deleted + cleared
