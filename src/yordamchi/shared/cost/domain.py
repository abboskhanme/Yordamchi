"""Usage ledger rules, pure (SPEC §3.2.11 step 6, §4.2 `usage_records` / `cap_notices`, §4.3 CAPS; UC-07,
NFR-COST-01/02).

Money is integer micro-USD end to end: ports already deliver `cost_usd_micros` (adapters convert the vendor's float
once, `core.money.usd_float_to_micros`), and the ledger stores exactly that. On the Claude plan the amount is the SDK's
API-equivalent estimate (UC-07), so `is_paid_api` — not the amount — separates spend from usage. A `Usage` that
cannot be true — a negative amount, or a paid API call on the Claude plan or Edge TTS — is a bug: `ValueError`.

On top of the ledger (T51):
- **Windows.** "xarajat?" shows today, the last 7 days (today included) and this month; the daily summary the last
  24 hours. Days and months are the owner's (`ui.timezone`), so the report and the caps agree on what "this month" is.
- **Caps.** Each payable provider has a monthly cap in whole USD (`caps.<provider>_monthly_usd`), shared by both bots.
  `Allowance.allowed` is False from 100 %: that provider's paid calls pause until the month ends or the owner raises
  the cap (a weakening change: one question). `due_notice` says which notice is owed — the warning at
  `caps.warn_pct` or the pause at 100 % — once per provider, month, level and cap. Every comparison is on integers.
- **Text.** `report_text` and `summary_text` build the owner's lines. Paid spend is money; the Claude plan's figure is
  always shown apart, as an estimate, labelled as no money paid (ClaudeManager rotates the subscription accounts).
- **Estimates.** `audio_cost_micros` prices a provider that reports no cost (Google STT) per audio minute.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Final

from yordamchi.core import money, timefmt
from yordamchi.core import strings as S
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText, join
from yordamchi.ports.mac_host import ClaudeManagerState
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.settings.registry import Setting

USAGE_KEEP: Final = timedelta(days=400)
NOTICE_KEEP: Final = timedelta(days=400)  # SPEC §4.2 `cap_notices`
MODEL_MAX_LEN: Final = 96
SOURCE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,31}")
UNIT_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
TURN_SOURCE: Final = "turn"
TOP_PROJECTS: Final = 3
WEEK_DAYS: Final = 7
LAST_DAY: Final = timedelta(hours=24)
# How often the watch loop looks at the caps and the summary time: a crossing is told within 5 minutes.
WATCH_EVERY_S: Final = 300.0
# A daily summary that could not go at its time (the Mac slept) goes within this window, else it skips that day.
SUMMARY_LATE: Final = timedelta(hours=1)
# ClaudeManager's state file, read for the report's Claude-limit line; the report stays within 3 s when it hangs.
MANAGER_TIMEOUT_S: Final = 1.0
_MS_PER_MIN: Final = 60_000


class Provider(StrEnum):
    ANTHROPIC_PLAN = "anthropic_plan"  # the owner's Claude subscription: usage, not money
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    EDGE = "edge"  # free TTS fallback


# Providers that can bill per call; a Google or OpenRouter key on a free tier records is_paid_api=False.
PAYABLE: Final = frozenset({Provider.GOOGLE, Provider.OPENROUTER, Provider.OPENAI})
PAYABLE_ORDER: Final = (Provider.GOOGLE, Provider.OPENROUTER, Provider.OPENAI)  # as the report lists them
CAP_SETTINGS: Final[Mapping[Provider, Setting[int]]] = {
    Provider.GOOGLE: defaults.CAPS_GOOGLE_MONTHLY_USD,
    Provider.OPENROUTER: defaults.CAPS_OPENROUTER_MONTHLY_USD,
    Provider.OPENAI: defaults.CAPS_OPENAI_MONTHLY_USD,
}


@dataclass(frozen=True, slots=True)
class Usage:
    """One billable or metered call: a finished turn, an STT or TTS request, a routing ask.

    `source` names the path (`turn`, `routing`, `report`, `stt`, `tts`, `video`, `qa`); `units` are the provider's own
    counters (`input_tokens`, `output_tokens`, `audio_ms`, `characters`).
    """

    source: str
    provider: Provider
    model: str
    is_paid_api: bool
    cost_usd_micros: int
    duration_ms: int
    units: Mapping[str, int] = field(default_factory=dict)
    project_id: str | None = None
    turn_id: int | None = None


def problems(usage: Usage) -> list[str]:
    """Why `usage` cannot be recorded; empty when it can."""
    found: list[str] = []
    if not SOURCE_PATTERN.fullmatch(usage.source):
        found.append("bad_source")
    if not usage.model or len(usage.model) > MODEL_MAX_LEN:
        found.append("bad_model")
    if usage.cost_usd_micros < 0:
        found.append("negative_cost")
    if usage.duration_ms < 0:
        found.append("negative_duration")
    if usage.is_paid_api and usage.provider not in PAYABLE:
        found.append("not_a_paid_api")
    if any(not UNIT_PATTERN.fullmatch(name) or count < 0 for name, count in usage.units.items()):
        found.append("bad_units")
    return found


def require_valid(usage: Usage) -> None:
    found = problems(usage)
    if found:
        msg = f"usage cannot be recorded: {', '.join(found)}"
        raise ValueError(msg)


def retention_cutoff(now: datetime) -> datetime:
    """Records strictly older than this may be deleted."""
    return now - USAGE_KEEP


def notice_cutoff(now: datetime) -> datetime:
    """Cap notices sent strictly before this may be deleted."""
    return now - NOTICE_KEEP


def audio_cost_micros(audio_ms: int, usd_micros_per_min: int) -> int:
    """The estimated cost of `audio_ms` of audio at a per-minute price, rounded up to a whole micro-dollar."""
    billable = max(0, audio_ms) * max(0, usd_micros_per_min)
    return -(-billable // _MS_PER_MIN)


# --- windows --------------------------------------------------------------------------------------------------------
class Period(StrEnum):
    TODAY = "today"  # since the owner's midnight
    WEEK = "week"  # the last 7 days, today included
    MONTH = "month"  # since the 1st, the owner's midnight
    DAY = "day"  # the last 24 hours: the daily summary


REPORT_PERIODS: Final = (Period.TODAY, Period.WEEK, Period.MONTH)


@dataclass(frozen=True, slots=True)
class Windows:
    """Where each period starts (UTC instants; every period ends now), and this month as `YYYY-MM`."""

    starts: Mapping[Period, datetime]
    month: str


def windows(now: datetime, timezone: str) -> Windows:
    """The periods as the owner counts them, in `timezone` (`ui.timezone`)."""
    local = timefmt.local(now, timezone)
    zone = local.tzinfo
    today = datetime(local.year, local.month, local.day, tzinfo=zone)
    week_first = local.date() - timedelta(days=WEEK_DAYS - 1)
    week = datetime(week_first.year, week_first.month, week_first.day, tzinfo=zone)
    month = datetime(local.year, local.month, 1, tzinfo=zone)
    starts = {
        Period.TODAY: today.astimezone(UTC),
        Period.WEEK: week.astimezone(UTC),
        Period.MONTH: month.astimezone(UTC),
        Period.DAY: now.astimezone(UTC) - LAST_DAY,
    }
    return Windows(starts=starts, month=month_key(now, timezone))


def month_key(now: datetime, timezone: str) -> str:
    """`YYYY-MM` of `now` in `timezone`: the month a cap counts against."""
    local = timefmt.local(now, timezone)
    return f"{local.year:04d}-{local.month:02d}"


# --- totals ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Figures:
    """What a group of ledger rows adds up to in one period."""

    cost_usd_micros: int = 0
    calls: int = 0
    turns: int = 0  # distinct turns among the rows
    duration_ms: int = 0

    def __add__(self, other: "Figures") -> "Figures":
        return Figures(
            cost_usd_micros=self.cost_usd_micros + other.cost_usd_micros,
            calls=self.calls + other.calls,
            turns=self.turns + other.turns,
            duration_ms=self.duration_ms + other.duration_ms,
        )


NO_FIGURES: Final = Figures()


@dataclass(frozen=True, slots=True)
class UsageTotal:
    """One bot's ledger rows of one (provider, source, paid) group, added up per period."""

    bot: BotName
    provider: str
    source: str
    is_paid_api: bool
    figures: Mapping[Period, Figures]

    def of(self, period: Period) -> Figures:
        return self.figures.get(period, NO_FIGURES)

    @property
    def is_plan(self) -> bool:
        return self.provider == Provider.ANTHROPIC_PLAN.value


@dataclass(frozen=True, slots=True)
class ProjectTotal:
    """One project's ledger rows this month. `plan_usd_micros` is the Claude plan's API-equivalent estimate."""

    project_id: str
    turns: int
    plan_usd_micros: int
    paid_usd_micros: int

    @property
    def weight_usd_micros(self) -> int:
        return self.plan_usd_micros + self.paid_usd_micros


def _sum(totals: Iterable[UsageTotal], period: Period) -> Figures:
    result = NO_FIGURES
    for total in totals:
        result = result + total.of(period)
    return result


def _paid(totals: Iterable[UsageTotal]) -> list[UsageTotal]:
    return [total for total in totals if total.is_paid_api]


def _plan(totals: Iterable[UsageTotal]) -> list[UsageTotal]:
    return [total for total in totals if total.is_plan and not total.is_paid_api]


def _turn_rows(totals: Iterable[UsageTotal]) -> list[UsageTotal]:
    return [total for total in totals if total.source == TURN_SOURCE]


def paid_spend(totals: Iterable[UsageTotal], period: Period, *, provider: Provider | None = None) -> int:
    """Money paid to the APIs in `period` (of one provider, or all), in micro-USD. The Claude plan is never in it."""
    rows = [total for total in _paid(totals) if provider is None or total.provider == provider.value]
    return _sum(rows, period).cost_usd_micros


def plan_estimate(totals: Iterable[UsageTotal], period: Period) -> int:
    """The Claude plan's API-equivalent estimate in `period`: usage within the subscription, not money."""
    return _sum(_plan(totals), period).cost_usd_micros


def merge_projects(rows: Iterable[ProjectTotal]) -> list[ProjectTotal]:
    """One entry per project, adding up both bots' ledgers."""
    merged: dict[str, ProjectTotal] = {}
    for row in rows:
        seen = merged.get(row.project_id)
        merged[row.project_id] = (
            row
            if seen is None
            else ProjectTotal(
                project_id=row.project_id,
                turns=seen.turns + row.turns,
                plan_usd_micros=seen.plan_usd_micros + row.plan_usd_micros,
                paid_usd_micros=seen.paid_usd_micros + row.paid_usd_micros,
            )
        )
    return list(merged.values())


def top_projects(rows: Iterable[ProjectTotal], *, limit: int = TOP_PROJECTS) -> list[ProjectTotal]:
    """The projects that used the most this month: by cost (the plan estimate plus paid), then turns, then name."""
    ranked = sorted(merge_projects(rows), key=lambda row: (-row.weight_usd_micros, -row.turns, row.project_id))
    return [row for row in ranked if row.weight_usd_micros > 0 or row.turns > 0][:limit]


# --- caps -----------------------------------------------------------------------------------------------------------
class CapLevel(IntEnum):
    """A cap notice, as `cap_notices.level` stores it."""

    WARN = 80  # `caps.warn_pct` reached (80 % by default): told once
    REACHED = 100  # the cap is used up: paid calls to the provider pause


@dataclass(frozen=True, slots=True)
class Allowance:
    """Whether a paid call to `provider` may go now: this month's spend of both bots against the cap."""

    provider: Provider
    month: str
    cap_usd: int
    warn_pct: int
    spent_usd_micros: int

    @property
    def cap_usd_micros(self) -> int:
        return self.cap_usd * money.MICROS_PER_USD

    @property
    def allowed(self) -> bool:
        """False from 100 %; a cap of 0 allows no paid call at all."""
        return self.spent_usd_micros < self.cap_usd_micros

    @property
    def remaining_usd_micros(self) -> int:
        return max(0, self.cap_usd_micros - self.spent_usd_micros)

    @property
    def used_pct(self) -> int:
        """Whole percent, rounded down (so 79.99 % is 79); a cap of 0 counts as used up."""
        if self.cap_usd_micros == 0:
            return 100
        return self.spent_usd_micros * 100 // self.cap_usd_micros

    @property
    def level(self) -> CapLevel | None:
        if not self.allowed:
            return CapLevel.REACHED
        if self.spent_usd_micros * 100 >= self.cap_usd_micros * self.warn_pct:
            return CapLevel.WARN
        return None


def allowance_of(provider: Provider, snap: Snapshot, *, month: str, spent_usd_micros: int) -> Allowance:
    """`provider`'s allowance under this snapshot's cap and warning share. Only a payable provider has a cap."""
    setting = CAP_SETTINGS.get(provider)
    if setting is None:
        msg = f"{provider} has no cap: it never bills"
        raise ValueError(msg)
    return Allowance(
        provider=provider,
        month=month,
        cap_usd=snap[setting],
        warn_pct=snap[defaults.CAPS_WARN_PCT],
        spent_usd_micros=spent_usd_micros,
    )


@dataclass(frozen=True, slots=True)
class SentNotice:
    """A `cap_notices` row of this month."""

    provider: str
    level: int
    cap_usd: int


def due_notice(allowance: Allowance, sent: Collection[SentNotice]) -> CapLevel | None:
    """The notice owed now: the highest level reached, unless it went out for this cap this month. Nothing is owed
    before any money was spent (a cap of 0 the owner set on purpose is not news)."""
    level = allowance.level
    if level is None or allowance.spent_usd_micros == 0:
        return None
    if SentNotice(allowance.provider.value, level.value, allowance.cap_usd) in sent:
        return None
    return level


def suggested_cap(cap_usd: int) -> int:
    """The example in the pause notice ("… limitini 20 dollar qil"): double, at least $10."""
    return max(10, cap_usd * 2)


@dataclass(frozen=True, slots=True)
class DueNotice:
    """One cap notice to send now."""

    allowance: Allowance
    level: CapLevel

    @property
    def ref(self) -> str:
        """The outbox ref: the same notice is never queued twice."""
        item = self.allowance
        return f"cap-{item.provider.value}-{item.month}-{self.level.value}-{item.cap_usd}"

    @property
    def text(self) -> BotText:
        item = self.allowance
        provider = provider_label(item.provider)
        spent, cap = usd_text(item.spent_usd_micros), cap_text(item.cap_usd)
        if self.level is CapLevel.WARN:
            return S.COST_CAP_WARNING(provider=provider, pct=item.used_pct, spent=spent, cap=cap)
        return S.COST_CAP_REACHED(provider=provider, spent=spent, cap=cap, suggested=suggested_cap(item.cap_usd))


def due_notices(allowances: Iterable[Allowance], sent: Collection[SentNotice]) -> tuple[DueNotice, ...]:
    return tuple(
        DueNotice(allowance, level) for allowance in allowances if (level := due_notice(allowance, sent)) is not None
    )


# --- text -----------------------------------------------------------------------------------------------------------
_PROVIDERS: Final[Mapping[str, S.Msg]] = {
    Provider.GOOGLE.value: S.COST_PROVIDER_GOOGLE,
    Provider.OPENROUTER.value: S.COST_PROVIDER_OPENROUTER,
    Provider.OPENAI.value: S.COST_PROVIDER_OPENAI,
}
# What each paid call was for, in the report's order; any other source is shown by its name, after these.
_USES: Final[Mapping[str, S.Msg]] = {
    "stt": S.COST_USE_STT,
    "tts": S.COST_USE_TTS,
    "routing": S.COST_USE_ROUTING,
    "report": S.COST_USE_REPORT,
    "video": S.COST_USE_VIDEO,
    "qa": S.COST_USE_QA,
}
_BOTS: Final[Mapping[BotName, S.Msg]] = {BotName.DEV: S.COST_BOT_DEV, BotName.PERSONAL: S.COST_BOT_PERSONAL}


def _use_rank(source: str) -> tuple[int, str]:
    order = tuple(_USES)
    return (order.index(source), "") if source in _USES else (len(order), source)


def provider_label(provider: str) -> BotText:
    label = _PROVIDERS.get(provider)
    return S.COST_NAME(name=provider) if label is None else label()


def use_label(source: str) -> BotText:
    label = _USES.get(source)
    return S.COST_NAME(name=source) if label is None else label()


def usd_text(micros: int) -> str:
    """`$12.80`; `$0` for nothing; `<$0.01` for a spend that rounds to zero cents (it is still not nothing)."""
    if micros == 0:
        return "$0"
    shown = money.format_usd(micros)
    return "<$0.01" if shown == "$0.00" else shown


def estimate_text(micros: int) -> str:
    """An API-equivalent estimate: `~$12.80`."""
    return "$0" if micros == 0 else f"~{usd_text(micros)}"


def cap_text(cap_usd: int) -> str:
    return f"${cap_usd}"


def duration_text(duration_ms: int) -> BotText:
    """`45 daq`, `3 soat 5 daq`."""
    minutes = max(0, duration_ms) // _MS_PER_MIN
    if minutes < 60:
        return S.COST_MINUTES(minutes=minutes)
    hours, minutes = divmod(minutes, 60)
    return S.COST_HOURS(hours=hours, minutes=minutes)


def _triple(values: Sequence[str]) -> Mapping[str, str]:
    today, week, month = values
    return {"today": today, "week": week, "month": month}


def _amounts(micros: Sequence[int], *, estimate: bool = False) -> Mapping[str, str]:
    shown = estimate_text if estimate else usd_text
    return _triple([shown(value) for value in micros])


@dataclass(frozen=True, slots=True)
class CostView:
    """Everything "xarajat?" shows, read from local data only: both bots' ledgers (this bot's own DB and the other
    bot's, read-only), the caps, and ClaudeManager's state when it could be read."""

    totals: tuple[UsageTotal, ...]
    projects: tuple[ProjectTotal, ...]
    allowances: tuple[Allowance, ...]  # one per payable provider, in `PAYABLE_ORDER`
    manager: ClaudeManagerState | None = None


def _provider_line(view: CostView, allowance: Allowance) -> BotText | None:
    spend = [paid_spend(view.totals, period, provider=allowance.provider) for period in REPORT_PERIODS]
    if not any(spend) and allowance.allowed:
        return None  # nothing spent and nothing paused: not worth a line
    amounts = _amounts(spend)
    provider, cap = provider_label(allowance.provider), cap_text(allowance.cap_usd)
    if allowance.cap_usd == 0:
        return S.COST_PROVIDER_OFF(provider=provider, **amounts)
    if not allowance.allowed:
        return S.COST_PROVIDER_PAUSED(provider=provider, cap=cap, **amounts)
    return S.COST_PROVIDER(provider=provider, cap=cap, pct=allowance.used_pct, **amounts)


def _uses_line(totals: Sequence[UsageTotal]) -> BotText | None:
    paid = _paid(totals)
    items = []
    for source in sorted({total.source for total in paid}, key=_use_rank):
        rows = [total for total in paid if total.source == source]
        spend = [_sum(rows, period).cost_usd_micros for period in REPORT_PERIODS]
        if any(spend):
            items.append(S.COST_USE_ITEM(use=use_label(source), **_amounts(spend)))
    return S.COST_USES(uses=join(items, " · ")) if items else None


def _turns_line(totals: Sequence[UsageTotal]) -> BotText | None:
    figures = [_sum(_turn_rows(totals), period) for period in REPORT_PERIODS]
    if not any(item.turns for item in figures):
        return None
    turns = _triple([str(item.turns) for item in figures])
    times = [duration_text(item.duration_ms) for item in figures]
    return S.COST_TURNS(**turns, time=join(times, " / "))


def _manager_line(manager: ClaudeManagerState | None) -> BotText | None:
    """The active subscription account's 5-hour and 7-day use, as ClaudeManager last wrote it (UC-07: if available)."""
    if manager is None:
        return None
    active = next((account for account in manager.accounts if account.is_active), None)
    if active is None or (active.five_hour_pct is None and active.seven_day_pct is None):
        return None
    return S.COST_CLAUDE_LIMITS(five=_pct_text(active.five_hour_pct), seven=_pct_text(active.seven_day_pct))


def _pct_text(value: float | None) -> BotText:
    return S.COST_UNKNOWN() if value is None else S.COST_PERCENT(value=round(value))


def _projects_line(projects: Sequence[ProjectTotal]) -> BotText | None:
    items = []
    for row in top_projects(projects):
        if row.paid_usd_micros:
            item = S.COST_PROJECT_WITH_PAID(
                project=row.project_id,
                turns=row.turns,
                estimate=estimate_text(row.plan_usd_micros),
                paid=usd_text(row.paid_usd_micros),
            )
        else:
            item = S.COST_PROJECT(project=row.project_id, turns=row.turns, estimate=estimate_text(row.plan_usd_micros))
        items.append(item)
    return S.COST_PROJECTS(projects=join(items, " · ")) if items else None


def _bots_line(totals: Sequence[UsageTotal]) -> BotText | None:
    """Each bot's share this month, only when both bots have used something (UC-07: per bot)."""
    bots = [bot for bot in BotName if any(total.bot is bot and total.of(Period.MONTH).calls for total in totals)]
    if len(bots) < 2:
        return None
    items = []
    for bot in bots:
        own = [total for total in totals if total.bot is bot]
        items.append(
            S.COST_BOT_ITEM(
                bot=_BOTS[bot](),
                paid=usd_text(paid_spend(own, Period.MONTH)),
                estimate=estimate_text(plan_estimate(own, Period.MONTH)),
            )
        )
    return S.COST_BOTS(bots=join(items, " · "))


def report_text(view: CostView) -> BotText:
    """The "xarajat?" answer (UC-07): paid spend per provider (against its cap) and per use, then the Claude plan
    apart — an estimate within the subscription, never counted as money — with turns, run time and the account's
    limits, then the top projects and each bot's share. Lines with nothing to say are left out."""
    paid = [paid_spend(view.totals, period) for period in REPORT_PERIODS]
    plan = [plan_estimate(view.totals, period) for period in REPORT_PERIODS]
    lines: list[BotText | None] = [
        S.COST_HEAD(),
        S.COST_PAID(**_amounts(paid)),
        *(_provider_line(view, allowance) for allowance in view.allowances),
        _uses_line(view.totals),
        S.COST_PLAN(**_amounts(plan, estimate=True)),
        _turns_line(view.totals),
        _manager_line(view.manager),
        _projects_line(view.projects),
        _bots_line(view.totals),
    ]
    return join(line for line in lines if line is not None)


def summary_text(totals: Sequence[UsageTotal]) -> BotText:
    """The optional daily line (`schedule.cost_summary_at`): the last 24 hours, with this month's paid spend."""
    turns = _sum(_turn_rows(totals), Period.DAY).turns
    return S.COST_SUMMARY(
        paid=usd_text(paid_spend(totals, Period.DAY)),
        month=usd_text(paid_spend(totals, Period.MONTH)),
        estimate=estimate_text(plan_estimate(totals, Period.DAY)),
        turns=turns,
    )


def summary_day(now: datetime, at: str, timezone: str) -> date | None:
    """The owner's date whose daily summary is due at `now`: from `at` (`HH:MM`, `ui.timezone`) for up to
    `SUMMARY_LATE`. None when the summary is off (`at` is empty) or not due."""
    if not at:
        return None
    local = timefmt.local(now, timezone)
    hour, minute = (int(part) for part in at.split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.date() if due <= local < due + SUMMARY_LATE else None
