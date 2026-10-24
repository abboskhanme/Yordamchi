"""Usage ledger use cases (SPEC §3.2.11 step 6, §4.2; UC-07, NFR-COST-01/02).

- `record_usage(s, …)`: append one call inside the caller's transaction (T08). A turn records its cost in the same
  unit of work that closes the turn, so a turn and its usage commit together:

      async with deps.db.uow() as s:
          await store.mark_turn_done(s, …)
          await cost.record_usage(s, bot=deps.bot, at=now, usage=Usage(source="turn", provider=Provider.ANTHROPIC_PLAN,
                                  model="opus", is_paid_api=False, cost_usd_micros=result.cost_usd_micros, …))

- `allowance(deps, provider)`: the narrow check a caller makes before a paid call. `Allowance.allowed` is False once
  this month's spend reached the provider's cap: skip the call (STT tries the other provider, TTS Edge, else text only).
- `notify_caps(deps)`: the 80 % warning and the 100 % pause notice, each once per provider, month and cap.
- `show_cost(deps, …)` / `cost_report(deps)`: "xarajat?" (UC-07) from local data only, within 3 s.
- `run_cost_watch(deps)`: the long-lived loop for the notices and the optional daily summary.
- `apply_retention(deps)`: 400 days for both tables.

The caps are one budget for both bots (ASSUMPTIONS: caps BOTH). Each bot writes only its own DB, so spend is this
bot's ledger plus the other bot's, read through `deps.peer_db` (read-only; SPEC §2.3 lists `usage_records` for both
directions). Ports are awaited only outside units of work (A3).
"""

import asyncio
import time
from datetime import date, datetime

from yordamchi.core.errors import ExternalError
from yordamchi.core.json_value import JsonObject
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Database, Session
from yordamchi.ports.mac_host import ClaudeManagerState
from yordamchi.shared.access import service as access
from yordamchi.shared.cost import domain, store
from yordamchi.shared.cost.domain import (
    Allowance,
    CostView,
    DueNotice,
    Period,
    ProjectTotal,
    Provider,
    SentNotice,
    Usage,
    UsageTotal,
    Windows,
)
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import domain as settings_domain
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)


# --- recording ------------------------------------------------------------------------------------------------------
async def record_usage(s: Session, *, bot: BotName, at: datetime, usage: Usage) -> int:
    """Append one usage row in the caller's transaction; returns its id. An impossible `usage` raises `ValueError`."""
    domain.require_valid(usage)
    units: JsonObject = dict(usage.units)
    row = await store.add_usage_record(
        s,
        bot=bot.value,
        at=at,
        source=usage.source,
        provider=usage.provider.value,
        is_paid_api=usage.is_paid_api,
        model=usage.model,
        project_id=usage.project_id,
        turn_id=usage.turn_id,
        cost_usd_micros=usage.cost_usd_micros,
        units=units,
        duration_ms=usage.duration_ms,
    )
    return row.id


# --- both bots' ledgers ---------------------------------------------------------------------------------------------
def _peer_bot(bot: BotName) -> BotName:
    return BotName.PERSONAL if bot is BotName.DEV else BotName.DEV


def _ledgers(deps: SharedDeps) -> list[tuple[Database, BotName]]:
    """This bot's DB and, when attached, the other bot's (read-only), with the bot whose rows each one holds."""
    ledgers = [(deps.db, deps.bot)]
    if deps.peer_db is not None:
        ledgers.append((deps.peer_db, _peer_bot(deps.bot)))
    return ledgers


def _windows(deps: SharedDeps, snap: Snapshot) -> Windows:
    return domain.windows(deps.clock.now(), snap[defaults.UI_TIMEZONE])


async def _read_ledger(db: Database, bot: BotName, span: Windows) -> tuple[list[UsageTotal], list[ProjectTotal]]:
    async with db.read() as s:
        totals = await store.list_usage_totals(s, bot=bot.value, starts=span.starts)
        projects = await store.list_project_totals(s, bot=bot.value, since=span.starts[Period.MONTH])
    return totals, projects


async def _read_ledgers(deps: SharedDeps, span: Windows) -> tuple[tuple[UsageTotal, ...], tuple[ProjectTotal, ...]]:
    totals: list[UsageTotal] = []
    projects: list[ProjectTotal] = []
    for db, bot in _ledgers(deps):
        own_totals, own_projects = await _read_ledger(db, bot, span)
        totals.extend(own_totals)
        projects.extend(own_projects)
    return tuple(totals), tuple(projects)


# --- caps (NFR-COST-02) ---------------------------------------------------------------------------------------------
async def allowance(deps: SharedDeps, provider: Provider, *, snap: Snapshot | None = None) -> Allowance:
    """May a paid call to `provider` go now? This month's spend of both bots against its cap (`caps.*`, read at
    use time, so a raised cap applies to the next call). Two indexed sums on local files; no port is called.

    Callers ask before every paid call and skip the provider when `allowed` is False: `shared/voice` moves STT to the
    other provider, or tells the owner; TTS (P3) moves to Edge, else sends text only."""
    current = await settings.snapshot(deps) if snap is None else snap
    span = _windows(deps, current)
    spent = 0
    for db, _bot in _ledgers(deps):
        async with db.read() as s:
            spent += await store.get_paid_spend(s, provider=provider.value, since=span.starts[Period.MONTH])
    return domain.allowance_of(provider, current, month=span.month, spent_usd_micros=spent)


async def notify_caps(deps: SharedDeps) -> tuple[DueNotice, ...]:
    """Tell the owner, in this bot's home chat, about each cap at `caps.warn_pct` or used up: once per provider,
    month, level and cap. The `cap_notices` row and the message are written in one unit of work, so a restart neither
    loses nor repeats one. Returns the notices queued now."""
    snap = await settings.snapshot(deps)
    allowances = [await allowance(deps, provider, snap=snap) for provider in domain.PAYABLE_ORDER]
    month = allowances[0].month
    async with deps.db.read() as s:
        rows = await store.list_cap_notices(s, month=month)
    due = domain.due_notices(allowances, {SentNotice(row.provider, row.level, row.cap_usd) for row in rows})
    if not due:
        return ()
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    queued: list[DueNotice] = []
    async with deps.db.uow() as s:
        for notice in due:
            item = notice.allowance
            first = await store.add_cap_notice(
                s, provider=item.provider.value, month=month, level=notice.level, cap_usd=item.cap_usd, sent_at=now
            )
            if not first:
                continue
            await outbox.enqueue_text(
                s,
                bot=deps.bot,
                chat_id=chat,
                text=notice.text,
                kind=Kind.SYSTEM,
                ref=notice.ref,
                at=now,
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            )
            queued.append(notice)
    for notice in queued:
        log.warning(
            "cost_cap_notified",
            bot=deps.bot.value,
            provider=notice.allowance.provider.value,
            level=notice.level.value,
            month=month,
            used_pct=notice.allowance.used_pct,
        )
    return tuple(queued)


# --- "xarajat?" (UC-07) ---------------------------------------------------------------------------------------------
async def _manager_state(deps: SharedDeps) -> ClaudeManagerState | None:
    """ClaudeManager's state file for the Claude-limit line, bounded: unreadable in time → the line is left out."""
    try:
        async with asyncio.timeout(domain.MANAGER_TIMEOUT_S):
            return await deps.mac_host.claude_manager_state(timeout=domain.MANAGER_TIMEOUT_S)
    except (TimeoutError, ExternalError, OSError, ValueError) as error:
        log.warning("cost_manager_unread", error_type=type(error).__name__)
        return None


async def _view(deps: SharedDeps, snap: Snapshot) -> CostView:
    span = _windows(deps, snap)
    async with asyncio.TaskGroup() as group:
        manager = group.create_task(_manager_state(deps))
        ledgers = group.create_task(_read_ledgers(deps, span))
    totals, projects = ledgers.result()
    allowances = tuple(
        domain.allowance_of(
            provider,
            snap,
            month=span.month,
            spent_usd_micros=domain.paid_spend(totals, Period.MONTH, provider=provider),
        )
        for provider in domain.PAYABLE_ORDER
    )
    return CostView(totals=totals, projects=projects, allowances=allowances, manager=manager.result())


async def _report(deps: SharedDeps, snap: Snapshot) -> BotText:
    started = time.monotonic()
    view = await _view(deps, snap)
    log.info(
        "cost_report_built",
        bot=deps.bot.value,
        groups=len(view.totals),
        projects=len(view.projects),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return domain.report_text(view)


async def cost_report(deps: SharedDeps) -> BotText:
    """The "xarajat?" text, from local data only: both bots' ledgers (two grouped queries per DB file), the caps, and
    ClaudeManager's state file bounded by `domain.MANAGER_TIMEOUT_S`, read at once, so it answers within 3 s."""
    return await _report(deps, await settings.snapshot(deps))


async def show_cost(deps: SharedDeps, *, chat_id: int, ref: str, reply_to: int | None = None) -> BotText:
    """ "xarajat?" / `/xarajat`: the report, queued as the reply in `chat_id`. `ref` keys it (e.g. `route-<id>`)."""
    snap = await settings.snapshot(deps)
    text = await _report(deps, snap)
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=Kind.REPORT,
            ref=f"cost-{ref}",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            reply_to=reply_to,
        )
    return text


# --- the watch loop: notices and the daily summary ------------------------------------------------------------------
async def send_daily_summary(deps: SharedDeps, *, day: date) -> bool:
    """The optional daily line (`schedule.cost_summary_at`) for the owner's date `day`, in this bot's home chat.
    Queued once per day whatever the restarts; True when it was queued now."""
    snap = await settings.snapshot(deps)
    totals, _projects = await _read_ledgers(deps, _windows(deps, snap))
    chat = await access.resolve_home_chat(deps)
    async with deps.db.uow() as s:
        queued = await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat,
            text=domain.summary_text(totals),
            kind=Kind.REPORT,
            ref=f"cost-summary-{day.isoformat()}",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
    if queued:
        log.info("cost_summary_queued", bot=deps.bot.value, day=day.isoformat())
    return bool(queued)


async def watch_costs(deps: SharedDeps, *, last_summary: date | None = None) -> date | None:
    """One pass of the watch loop: the cap notices, then the daily summary if `schedule.cost_summary_at` (dev bot
    only; empty = off) is due and `last_summary` is not today. Returns the date the summary was last sent for."""
    await notify_caps(deps)
    snap = await settings.snapshot(deps)
    if not settings_domain.in_scope(defaults.SCHEDULE_COST_SUMMARY_AT, deps.bot):
        return last_summary
    day = domain.summary_day(deps.clock.now(), snap[defaults.SCHEDULE_COST_SUMMARY_AT], snap[defaults.UI_TIMEZONE])
    if day is None or day == last_summary:
        return last_summary
    await send_daily_summary(deps, day=day)
    return day


async def run_cost_watch(deps: SharedDeps) -> None:
    """The long-lived loop: `aio.forever("cost", lambda: cost.run_cost_watch(deps))`, every `domain.WATCH_EVERY_S`,
    so a cap crossed by any paid call (STT, routing, reports) is told within minutes."""
    last_summary: date | None = None
    while True:
        last_summary = await watch_costs(deps, last_summary=last_summary)
        await deps.clock.sleep(domain.WATCH_EVERY_S)


# --- retention ------------------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """Delete usage rows and cap notices older than 400 days, in batches (SPEC §4.2). Returns how many were deleted."""
    now = deps.clock.now()
    cutoff, notices_cutoff = domain.retention_cutoff(now), domain.notice_cutoff(now)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_usage_records_before(s, cutoff, limit=limit)
    )
    notices = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_cap_notices_before(s, notices_cutoff, limit=limit)
    )
    log.info("cost_retention_applied", deleted=deleted, notices_deleted=notices)
    return deleted + notices
