"""The "xarajat?" format (UC-07; CONVENTIONS §11 golden output): two fixed views through the real `report_text`.

- `report_month.txt`: a busy month on the dev bot — paid spend per provider against its cap and per use, the Claude
  plan apart as an estimate, turns and run time, ClaudeManager's limits and the top 3 of four projects.
- `report_capped.txt`: OpenRouter paused at its cap, the personal bot's share next to the dev bot's, no ClaudeManager.

After a reviewed format change, regenerate the files with
`pytest tests/unit/shared/cost/test_report.py --update-golden`
(or `uv run python -c "from tests.unit.shared.cost.test_report import write_goldens; write_goldens()"`).
"""

from pathlib import Path
from typing import Final

import pytest

from tests.factories import make_allowance, make_usage_total, usd
from tests.goldens import check_golden
from tests.owner_text import shown
from yordamchi.core.paths import BotName
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.cost import domain
from yordamchi.shared.cost.domain import CostView, ProjectTotal, Provider

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = ROOT / "tests" / "golden" / "cost"
PLAN: Final = Provider.ANTHROPIC_PLAN
MANAGER: Final = ClaudeManagerState(
    active_id="a",
    auto_switch=True,
    accounts=(
        ClaudeAccount("owner@example.com", "ok", 42.0, 13.0, None, None, is_active=True),
        ClaudeAccount("other@example.com", "ok", 90.0, 70.0, None, None),
    ),
)


def month_view() -> CostView:
    totals = (
        make_usage_total(Provider.GOOGLE, "stt", paid=True, today=usd(0, 30), week=usd(2), month=usd(9, 50)),
        make_usage_total(Provider.GOOGLE, "routing", paid=True, month=usd(0, 10)),
        make_usage_total(Provider.OPENROUTER, "stt", paid=True, today=usd(0, 12), week=usd(1, 10), month=usd(3, 30)),
        make_usage_total(
            PLAN,
            "turn",
            paid=False,
            today=usd(4, 10),
            week=usd(28),
            month=usd(96, 50),
            turns=(3, 21, 64),
            duration_ms=(65 * 60_000, 9 * 3_600_000, 31 * 3_600_000 + 12 * 60_000),
        ),
        make_usage_total(PLAN, "routing", paid=False, today=usd(0, 5), week=usd(0, 40), month=usd(1, 20)),
        make_usage_total(Provider.EDGE, "tts", paid=False, today=0, week=0, month=0),  # free: never shown
    )
    projects = (
        ProjectTotal("HRMentor", turns=12, plan_usd_micros=usd(20), paid_usd_micros=0),
        ProjectTotal("BonviHR", turns=23, plan_usd_micros=usd(40, 10), paid_usd_micros=0),
        ProjectTotal("Wunderkind", turns=2, plan_usd_micros=usd(1), paid_usd_micros=0),
        ProjectTotal("Yordamchi", turns=5, plan_usd_micros=usd(8), paid_usd_micros=usd(0, 10)),
    )
    allowances = (
        make_allowance(Provider.GOOGLE, cap_usd=40, spent=usd(9, 60)),
        make_allowance(Provider.OPENROUTER, cap_usd=10, spent=usd(3, 30)),
        make_allowance(Provider.OPENAI, cap_usd=10, spent=0),
    )
    return CostView(totals=totals, projects=projects, allowances=allowances, manager=MANAGER)


def capped_view() -> CostView:
    totals = (
        make_usage_total(Provider.OPENROUTER, "stt", paid=True, today=usd(1), week=usd(4), month=usd(10, 2)),
        make_usage_total(
            PLAN,
            "turn",
            paid=False,
            today=0,
            week=usd(2),
            month=usd(6),
            turns=(0, 1, 3),
            duration_ms=(0, 45 * 60_000, 2 * 3_600_000),
        ),
        make_usage_total(
            Provider.OPENROUTER, "tts", paid=True, today=usd(0, 20), week=usd(0, 80), month=usd(1), bot=BotName.PERSONAL
        ),
        make_usage_total(Provider.GOOGLE, "stt", paid=True, today=4_000, week=4_000, month=4_000, bot=BotName.PERSONAL),
    )
    allowances = (
        make_allowance(Provider.GOOGLE, cap_usd=40, spent=4_000),
        make_allowance(Provider.OPENROUTER, cap_usd=10, spent=usd(11, 2)),
        make_allowance(Provider.OPENAI, cap_usd=0, spent=0),  # the owner turned OpenAI off
    )
    return CostView(totals=totals, projects=(), allowances=allowances, manager=None)


def write_goldens() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / "report_month.txt").write_text(domain.report_text(month_view()) + "\n", encoding="utf-8")
    (GOLDEN / "report_capped.txt").write_text(domain.report_text(capped_view()) + "\n", encoding="utf-8")


def test_a_busy_month_matches_the_golden_file(pytestconfig: pytest.Config) -> None:
    check_golden(pytestconfig, "cost/report_month.txt", domain.report_text(month_view()) + "\n")


def test_a_paused_provider_and_both_bots_match_the_golden_file(pytestconfig: pytest.Config) -> None:
    check_golden(pytestconfig, "cost/report_capped.txt", domain.report_text(capped_view()) + "\n")


def test_the_golden_month_keeps_paid_spend_and_the_plan_estimate_apart() -> None:
    lines = shown(domain.report_text(month_view())).splitlines()
    paid = next(line for line in lines if line.startswith("Pullik API:"))
    plan = next(line for line in lines if line.startswith("Claude obunasi"))
    assert paid == "Pullik API: $0.42 / $3.10 / $12.90"  # Google STT + routing + OpenRouter STT; no Claude
    assert "obuna doirasida, pul to'lanmaydi" in plan
    assert plan.endswith("~$4.15 / ~$28.40 / ~$97.70")
    assert not any("OpenAI" in line for line in lines)  # nothing spent, nothing paused: no line
    assert not any("Wunderkind" in line for line in lines)  # only the top 3 projects
