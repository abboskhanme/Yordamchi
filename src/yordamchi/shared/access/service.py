"""Owner admission use cases (SPEC §3.2.1 step 2; UC-01, D12, NFR-SEC-01).

`admit_update` is the one call `bots/common.OwnerOnly` makes for every update, before any handler runs. It reads the
policy from this update's settings snapshot (a change to `access.*` applies to the next update), then:
- **Drop**: logs `update_dropped` with numeric ids and a reason code only, and does nothing else: no reply, no
  reaction, no row. A stranger's update leaves no trace but that log line.
- **Leave**: calls `leaveChat` at once, directly (never the outbox, never posting there), bounded by
  `domain.LEAVE_TIMEOUT_S`; then appends one `access_dropped` audit row, so a foreign group the bot was put in shows in
  the audit trail. A failed leave is logged and the next update from that chat tries again.
- **Admit**: returns the verdict with the minted `OwnerOrigin`.
"""

import time

from yordamchi.core.errors import ExternalError
from yordamchi.core.log import LogValue, get_log
from yordamchi.shared.access import domain
from yordamchi.shared.access.domain import AccessPolicy, Arrival, Drop, Leave, Verdict
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.settings import service as settings

log = get_log(__name__)


async def load_policy(deps: SharedDeps) -> AccessPolicy:
    """This bot's admission rule from the current settings."""
    return domain.policy_for(await settings.snapshot(deps))


async def resolve_home_chat(deps: SharedDeps) -> int:
    """The chat where this bot tells the owner about a job: the dev group, or the owner's DM."""
    return (await load_policy(deps)).home_chat_id


async def admit_update(deps: SharedDeps, arrival: Arrival) -> Verdict:
    """Judge one update (UC-01): drop it silently, leave its foreign chat, or admit it with its `OwnerOrigin`."""
    verdict = domain.admit(arrival, await load_policy(deps))
    if isinstance(verdict, Drop):
        log.info(
            "update_dropped",
            bot=arrival.bot.value,
            update_id=arrival.update_id,
            update_type=arrival.event.value,
            user_id=arrival.user_id,
            chat_id=arrival.chat_id,
            reason=verdict.reason.value,
        )
    elif isinstance(verdict, Leave):
        await _leave(deps, arrival, verdict)
    return verdict


async def _leave(deps: SharedDeps, arrival: Arrival, verdict: Leave) -> None:
    started = time.monotonic()
    error_code: str | None = None
    try:
        await deps.messenger.leave(verdict.chat_id, timeout=domain.LEAVE_TIMEOUT_S)
    except ExternalError as error:
        error_code = error.code.value
    duration_ms = round((time.monotonic() - started) * 1000)
    async with deps.db.uow() as s:
        await audit.record_event(
            s,
            bot=deps.bot,
            at=deps.clock.now(),
            actor=AuditActor.SYSTEM,
            kind=AuditKind.ACCESS_DROPPED,
            ref_type="chat",
            ref_id=str(verdict.chat_id),
            data={
                "update_id": arrival.update_id,
                "update_type": arrival.event.value,
                "user_id": arrival.user_id,
                "chat_id": verdict.chat_id,
                "left": error_code is None,
                "error_code": error_code,
            },
        )
    fields: dict[str, LogValue] = {
        "bot": arrival.bot.value,
        "update_id": arrival.update_id,
        "update_type": arrival.event.value,
        "user_id": arrival.user_id,
        "chat_id": verdict.chat_id,
        "duration_ms": duration_ms,
    }
    if error_code is None:
        log.warning("foreign_chat_left", **fields)
    else:
        log.warning("foreign_chat_leave_failed", error_code=error_code, **fields)
