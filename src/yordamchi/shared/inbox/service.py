"""Inbound intake (SPEC §3.2.1, §2.7, §4.6; UC-04; NFR-REL-05, NFR-SEC-03) — the reference feature (CONVENTIONS §4).

`accept_update` is the one service call a Telegram handler makes (A7). In one unit of work it writes the
`(bot, update_id)` idempotency key, moves every key into the vault and stores only the redacted payload; after the
commit it acknowledges the owner. Later stages pick `received` rows up in `update_id` order with
`list_received_updates` — the normal path and the restart path (SPEC §2.7) are the same query.

The ack: a message holding a key is deleted first (UC-04: ≤ 5 s), then the masked reply follows; the "seen" reaction
and typing go out at the same time. Each Telegram call is bounded by `domain.ACK_TIMEOUT_S`. A failed ack is logged,
never raised: the update is already durable. (T15 moves these calls into the durable outbox, priority 0.)
"""

import asyncio
import time
from collections.abc import Awaitable, Collection
from dataclasses import dataclass
from datetime import datetime

from yordamchi.core import redact
from yordamchi.core.errors import DomainError, ExternalError
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.keystore import Keystore
from yordamchi.ports.messenger import ChatAction
from yordamchi.shared.access.domain import OwnerOrigin
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
    s: Session, keystore: Keystore, *, origin: OwnerOrigin, update: IncomingUpdate, scan: SecretScan, at: datetime
) -> _Recorded | None:
    """Inside the caller's unit of work: the idempotency key, then the vault, then the redacted payload.

    Returns None when `(bot, update_id)` was already there: a redelivered update changes nothing.
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
    return _Recorded(status=status, stored=stored, vault_error=vault_error)


async def accept_update(deps: SharedDeps, origin: OwnerOrigin, update: IncomingUpdate) -> Accepted:
    """Take one owner update in: processed exactly once per `(bot, update_id)`, keys vaulted before any write.

    `origin` is the proof from `shared/access` that this is the owner's update; a mismatch is a wiring bug and
    raises `ValueError` before anything is written. A missing master key does not raise: the update is stored as
    `failed` without its keys, the message is still deleted, and the owner is told what to fix. A failed ack call is
    logged, never raised. Only a failure to write `secret-hashes.json` (a disk problem) raises, after the ack.
    """
    started = time.monotonic()
    domain.require_matching_origin(origin, update, deps.bot)
    snap = await settings.snapshot(deps)
    scan = domain.scan_secrets(update)  # in memory only: nothing is written while a key is still in the text
    now = deps.clock.now()
    async with deps.db.uow() as s:
        recorded = await _record(s, deps.keystore, origin=origin, update=update, scan=scan, at=now)
    if recorded is None:
        log.info("update_duplicate", bot=deps.bot.value, update_id=update.update_id)
        return Accepted(update_id=update.update_id, duplicate=True)
    if recorded.vault_error is not None:
        log.warning("vault_unavailable", update_id=update.update_id, error_code=recorded.vault_error.code.value)

    plan = domain.plan_ack(
        update, scan, status=recorded.status, stored=recorded.stored, vault_error=recorded.vault_error
    )
    await _acknowledge(
        deps,
        plan,
        reaction=snap[defaults.UI_SEEN_REACTION],
        timeout=domain.ACK_TIMEOUT_S,
        update_id=update.update_id,
    )
    if recorded.stored:
        # After the ack, which is time-bound: refresh secret-hashes.json for the pre-push scan.
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


# --- ack ----------------------------------------------------------------------------------------------------------
async def _attempt[T](call: Awaitable[T], *, name: str, update_id: int) -> T | None:
    """One Telegram call of the ack. An `ExternalError` (network, rate limit, timeout) is logged and becomes None."""
    try:
        return await call
    except ExternalError as error:
        log.warning("ack_failed", call=name, update_id=update_id, error_code=error.code.value)
        return None


async def _delete_then_reply(deps: SharedDeps, plan: AckPlan, *, timeout: float, update_id: int) -> None:
    deleted = False
    if plan.delete_message and plan.message_id is not None:
        result = await _attempt(
            deps.messenger.delete(plan.chat_id, plan.message_id, timeout=timeout), name="delete", update_id=update_id
        )
        deleted = result is True
        if not deleted:
            log.warning("secret_message_kept", update_id=update_id)
    reply = domain.ack_reply(plan, deleted=deleted)
    if reply is not None:
        sent = deps.messenger.send_text(plan.chat_id, reply, timeout=timeout)
        await _attempt(sent, name="reply", update_id=update_id)


async def _acknowledge(deps: SharedDeps, plan: AckPlan, *, reaction: str, timeout: float, update_id: int) -> None:
    async with asyncio.TaskGroup() as group:
        if plan.replies:
            group.create_task(_delete_then_reply(deps, plan, timeout=timeout, update_id=update_id))
        if plan.react and plan.message_id is not None:
            seen = deps.messenger.react(plan.chat_id, plan.message_id, reaction, timeout=timeout)
            group.create_task(_attempt(seen, name="react", update_id=update_id))
        if plan.typing:
            typing = deps.messenger.chat_action(plan.chat_id, ChatAction.TYPING, timeout=timeout)
            group.create_task(_attempt(typing, name="typing", update_id=update_id))


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
