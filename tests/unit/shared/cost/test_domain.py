"""cost.domain: what a usage row may say (integer micro-USD, paid only on a payable API), the 400-day cutoff, the
owner's windows, the cap levels and notices (integers only), and the report's parts (the golden format is in
`test_report.py`)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.factories import make_allowance, make_default_snapshot, make_usage, make_usage_total, usd
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.cost import domain
from yordamchi.shared.cost.domain import CapLevel, CostView, Period, ProjectTotal, Provider, SentNotice, Usage


def test_providers_are_the_spec_set_and_only_apis_can_be_paid() -> None:
    assert {provider.value for provider in Provider} == {"anthropic_plan", "google", "openrouter", "openai", "edge"}
    assert {Provider.GOOGLE, Provider.OPENROUTER, Provider.OPENAI} == domain.PAYABLE


def test_a_well_formed_usage_has_no_problems() -> None:
    assert domain.problems(make_usage()) == []
    domain.require_valid(make_usage())


def test_a_claude_turn_records_its_api_equivalent_cost_as_unpaid() -> None:
    turn = make_usage(
        source="turn",
        provider=Provider.ANTHROPIC_PLAN,
        model="opus",
        is_paid_api=False,
        cost_usd_micros=412_000,
        units={"input_tokens": 1200, "output_tokens": 300},
        project_id="BonviHR",
        turn_id=17,
    )
    assert domain.problems(turn) == []


@pytest.mark.parametrize(
    ("usage", "problem"),
    [
        pytest.param(make_usage(source="Not A Source"), "bad_source", id="source"),
        pytest.param(make_usage(model=""), "bad_model", id="empty_model"),
        pytest.param(make_usage(model="m" * (domain.MODEL_MAX_LEN + 1)), "bad_model", id="long_model"),
        pytest.param(make_usage(cost_usd_micros=-1), "negative_cost", id="cost"),
        pytest.param(make_usage(duration_ms=-5), "negative_duration", id="duration"),
        pytest.param(make_usage(provider=Provider.ANTHROPIC_PLAN), "not_a_paid_api", id="plan_paid"),
        pytest.param(make_usage(provider=Provider.EDGE), "not_a_paid_api", id="edge_paid"),
        pytest.param(make_usage(units={"audio_ms": -1}), "bad_units", id="negative_unit"),
        pytest.param(make_usage(units={"Bad-Name": 1}), "bad_units", id="unit_name"),
    ],
)
def test_an_impossible_usage_is_refused(usage: Usage, problem: str) -> None:
    assert problem in domain.problems(usage)
    with pytest.raises(ValueError, match=problem):
        domain.require_valid(usage)


def test_usage_is_kept_400_days() -> None:
    now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    assert domain.retention_cutoff(now) == now - timedelta(days=400)
    assert domain.notice_cutoff(now) == now - timedelta(days=400)


# --- windows (UC-07: today, 7 days, this month in ui.timezone) -----------------------------------------------------
TASHKENT = "Asia/Tashkent"  # UTC+5, no DST


def test_windows_are_the_owners_day_last_7_days_and_month() -> None:
    now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)  # 14:00 in Tashkent
    span = domain.windows(now, TASHKENT)
    assert span.starts == {
        Period.TODAY: datetime(2026, 10, 22, 19, 0, tzinfo=UTC),  # 23 Oct 00:00 local
        Period.WEEK: datetime(2026, 10, 16, 19, 0, tzinfo=UTC),  # 17 Oct 00:00 local: today and the 6 days before
        Period.MONTH: datetime(2026, 9, 30, 19, 0, tzinfo=UTC),  # 1 Oct 00:00 local
        Period.DAY: datetime(2026, 10, 22, 9, 0, tzinfo=UTC),  # the last 24 hours
    }
    assert span.month == "2026-10"


def test_the_month_rolls_over_at_the_owners_midnight_not_utcs() -> None:
    before = datetime(2026, 10, 31, 18, 59, 59, tzinfo=UTC)  # 31 Oct 23:59:59 local
    after = datetime(2026, 10, 31, 19, 0, tzinfo=UTC)  # 1 Nov 00:00 local, still 31 Oct in UTC
    assert (domain.month_key(before, TASHKENT), domain.month_key(after, TASHKENT)) == ("2026-10", "2026-11")
    span = domain.windows(after, TASHKENT)
    assert span.starts[Period.MONTH] == span.starts[Period.TODAY] == after
    assert span.starts[Period.WEEK] < span.starts[Period.MONTH]  # the last 7 days still reach into October


# --- caps (NFR-COST-02): integer micro-USD only ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("spent", "level", "allowed", "pct"),
    [
        pytest.param(0, None, True, 0, id="nothing"),
        pytest.param(usd(8) - 1, None, True, 79, id="one_micro_under_80"),
        pytest.param(usd(8), CapLevel.WARN, True, 80, id="exactly_80"),
        pytest.param(usd(10) - 1, CapLevel.WARN, True, 99, id="one_micro_left"),
        pytest.param(usd(10), CapLevel.REACHED, False, 100, id="exactly_100"),
        pytest.param(usd(12, 50), CapLevel.REACHED, False, 125, id="over"),
    ],
)
def test_the_cap_levels_are_exact_on_integer_micros(
    spent: int, level: CapLevel | None, allowed: bool, pct: int
) -> None:
    allowance = make_allowance(cap_usd=10, spent=spent)
    assert (allowance.level, allowance.allowed, allowance.used_pct) == (level, allowed, pct)
    assert allowance.cap_usd_micros == 10_000_000
    assert allowance.remaining_usd_micros == max(0, 10_000_000 - spent)
    for value in (allowance.cap_usd_micros, allowance.remaining_usd_micros, allowance.used_pct):
        assert type(value) is int  # never a float, not even for the share


def test_the_warning_share_is_the_setting() -> None:
    assert make_allowance(cap_usd=10, spent=usd(8), warn_pct=90).level is None
    assert make_allowance(cap_usd=10, spent=usd(9), warn_pct=90).level is CapLevel.WARN


def test_a_cap_of_zero_allows_no_paid_call_but_owes_no_notice_before_spend() -> None:
    off = make_allowance(cap_usd=0, spent=0)
    assert (off.allowed, off.level, off.used_pct) == (False, CapLevel.REACHED, 100)
    assert domain.due_notice(off, set()) is None


def test_the_allowance_reads_the_cap_and_share_from_the_snapshot() -> None:
    snap = make_default_snapshot()
    allowance = domain.allowance_of(Provider.GOOGLE, snap, month="2026-10", spent_usd_micros=usd(32))
    assert (allowance.cap_usd, allowance.warn_pct, allowance.level) == (40, 80, CapLevel.WARN)
    with pytest.raises(ValueError, match="never bills"):
        domain.allowance_of(Provider.ANTHROPIC_PLAN, snap, month="2026-10", spent_usd_micros=0)


def test_each_notice_is_owed_once_per_cap_and_month() -> None:
    warn = make_allowance(cap_usd=10, spent=usd(8))
    assert domain.due_notice(warn, set()) is CapLevel.WARN
    sent = {SentNotice("openrouter", 80, 10)}
    assert domain.due_notice(warn, sent) is None
    reached = make_allowance(cap_usd=10, spent=usd(10))
    assert domain.due_notice(reached, sent) is CapLevel.REACHED  # the pause is its own notice
    sent.add(SentNotice("openrouter", 100, 10))
    assert domain.due_notice(reached, sent) is None
    raised = make_allowance(cap_usd=20, spent=usd(16))  # the owner raised the cap: its own warning
    assert domain.due_notice(raised, sent) is CapLevel.WARN


def test_a_jump_past_100_owes_only_the_pause() -> None:
    [notice] = domain.due_notices([make_allowance(cap_usd=10, spent=usd(30))], set())
    assert (notice.level, notice.ref) == (CapLevel.REACHED, "cap-openrouter-2026-10-100-10")


def test_the_notices_say_how_much_and_what_happens_next() -> None:
    [warn] = domain.due_notices([make_allowance(cap_usd=10, spent=usd(8, 4))], set())
    assert warn.text == (
        "OpenRouter: oylik limitning 80 foizi ishlatildi ($8.04 / $10). $10 ga yetganda pullik chaqiruvlar to'xtaydi."
    )
    [reached] = domain.due_notices([make_allowance(Provider.GOOGLE, cap_usd=40, spent=usd(40, 12))], set())
    assert reached.text.startswith("Google Gemini: oylik limit ($40) tugadi — $40.12 sarflandi. ")
    assert reached.text.endswith("masalan: «Google Gemini limitini 80 dollar qil».")


# --- money and estimates --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("micros", "shown"),
    [(0, "$0"), (1, "<$0.01"), (4_999, "<$0.01"), (5_000, "$0.01"), (12_804_999, "$12.80"), (12_805_000, "$12.81")],
)
def test_amounts_round_half_up_to_cents_and_tiny_spend_is_not_zero(micros: int, shown: str) -> None:
    assert domain.usd_text(micros) == shown


def test_an_estimate_is_marked_as_one() -> None:
    assert (domain.estimate_text(0), domain.estimate_text(usd(96, 50))) == ("$0", "~$96.50")


@pytest.mark.parametrize(
    ("audio_ms", "price", "micros"),
    [(60_000, 2_000, 2_000), (1, 2_000, 1), (90_500, 2_000, 3_017), (60_000, 0, 0), (-5, 2_000, 0)],
)
def test_audio_is_priced_per_minute_rounded_up_to_a_micro(audio_ms: int, price: int, micros: int) -> None:
    cost = domain.audio_cost_micros(audio_ms, price)
    assert (cost, type(cost)) == (micros, int)


# --- the report's parts ---------------------------------------------------------------------------------------------
def test_plan_usage_is_never_paid_spend() -> None:
    totals = [
        make_usage_total(Provider.ANTHROPIC_PLAN, "turn", paid=False, today=usd(50), week=usd(50), month=usd(500)),
        make_usage_total(Provider.GOOGLE, "stt", paid=False, month=usd(3)),  # a free-tier key: not money either
    ]
    assert [domain.paid_spend(totals, period) for period in domain.REPORT_PERIODS] == [0, 0, 0]
    assert domain.plan_estimate(totals, Period.MONTH) == usd(500)


def test_the_top_projects_are_ranked_by_cost_then_turns() -> None:
    rows = [
        ProjectTotal("A", turns=10, plan_usd_micros=usd(1), paid_usd_micros=0),
        ProjectTotal("B", turns=2, plan_usd_micros=usd(30), paid_usd_micros=0),
        ProjectTotal("C", turns=4, plan_usd_micros=usd(1), paid_usd_micros=0),
        ProjectTotal("B", turns=1, plan_usd_micros=usd(5), paid_usd_micros=usd(1)),  # the other bot's rows
        ProjectTotal("D", turns=0, plan_usd_micros=0, paid_usd_micros=0),
    ]
    top = domain.top_projects(rows)
    assert [(row.project_id, row.turns, row.weight_usd_micros) for row in top] == [
        ("B", 3, usd(36)),
        ("A", 10, usd(1)),
        ("C", 4, usd(1)),
    ]


def test_run_time_is_whole_minutes() -> None:
    shown = [domain.duration_text(ms) for ms in (0, 59_999, 45 * 60_000, 3 * 3_600_000 + 5 * 60_000)]
    assert shown == ["0 daq", "0 daq", "45 daq", "3 soat 5 daq"]


def test_the_claude_limits_line_needs_an_active_account() -> None:
    accounts = (ClaudeAccount("owner@example.com", "ok", 42.4, None, None, None, is_active=True),)
    view = CostView(totals=(), projects=(), allowances=(), manager=ClaudeManagerState("a", True, accounts))
    assert "Claude limiti (ClaudeManager): 5 soat 42% · 7 kun noma'lum" in domain.report_text(view).splitlines()
    idle = CostView(totals=(), projects=(), allowances=(), manager=ClaudeManagerState(None, True, ()))
    assert not any(line.startswith("Claude limiti") for line in domain.report_text(idle).splitlines())


def test_an_empty_ledger_still_answers_with_zeros() -> None:
    view = CostView(totals=(), projects=(), allowances=(make_allowance(cap_usd=10),))
    assert domain.report_text(view).splitlines() == [
        "Xarajat · bugun / 7 kun / shu oy",
        "Pullik API: $0 / $0 / $0",
        "Claude obunasi (API narxida taxmin; obuna doirasida, pul to'lanmaydi): $0 / $0 / $0",
    ]


# --- the daily summary (schedule.cost_summary_at) -------------------------------------------------------------------
def test_the_summary_is_one_line_about_the_last_24_hours() -> None:
    plan = make_usage_total(
        Provider.ANTHROPIC_PLAN, "turn", paid=False, today=usd(4, 10), month=usd(90), turns=(3, 3, 40)
    )
    paid = make_usage_total(Provider.OPENROUTER, "stt", paid=True, today=usd(0, 42), month=usd(12, 80))
    assert domain.summary_text([paid, plan]) == (
        "Xarajat, so'nggi 24 soat: pullik API $0.42 (shu oy $12.80) · Claude ~$4.10 — obuna doirasida, pul "
        "to'lanmaydi · 3 ta ish"
    )


@pytest.mark.parametrize(
    ("hour", "minute", "at", "due"),
    [
        pytest.param(21, 0, "", False, id="off"),
        pytest.param(20, 59, "21:00", False, id="early"),
        pytest.param(21, 0, "21:00", True, id="on_time"),
        pytest.param(21, 59, "21:00", True, id="late_within_an_hour"),
        pytest.param(22, 0, "21:00", False, id="too_late_skips_the_day"),
    ],
)
def test_the_summary_is_due_from_its_time_for_an_hour(hour: int, minute: int, at: str, due: bool) -> None:
    now = datetime(2026, 10, 23, hour - 5, minute, tzinfo=UTC)  # the owner's HH:MM; Tashkent is UTC+5
    assert domain.summary_day(now, at, TASHKENT) == (date(2026, 10, 23) if due else None)
