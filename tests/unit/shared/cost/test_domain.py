"""cost.domain: what a usage row may say (integer micro-USD, paid only on a payable API) and the 400-day cutoff."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.factories import make_usage
from yordamchi.shared.cost import domain
from yordamchi.shared.cost.domain import Provider, Usage


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
