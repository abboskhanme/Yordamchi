"""The routing eval seed (`tests/eval/routing/cases.yaml`, T20; T26 adds the runner and the live LLM answers).

This checks what holds without any model: the file's format, the registry it is written against (the owner's real
folders), and that the deterministic layer — names, context, and the fallback when the LLM is unavailable — never
routes a case to the wrong project without asking (UC-10 AC "silent wrong routes ≤ 1"; here 0).
"""

import tomllib
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
import yaml

from tests.factories import (
    OWNER_PROJECT_NAMES,
    make_activity,
    make_default_snapshot,
    make_part,
    make_project_views,
    make_running_turn,
    make_turn_input,
    make_voice_part,
)
from tests.fakes.clock import DEFAULT_START
from yordamchi.dev.routing import domain
from yordamchi.dev.routing.domain import ActivityKind, Outcome, RouteDecision, RouteFacts

EVAL: Final = Path(__file__).resolve().parents[3] / "eval"
SEED: Final = yaml.safe_load((EVAL / "routing" / "cases.yaml").read_text(encoding="utf-8"))
THRESHOLDS: Final = tomllib.loads((EVAL / "thresholds.toml").read_text(encoding="utf-8"))["routing"]
CASES: Final[list[dict[str, object]]] = SEED["cases"]
CATEGORIES: Final = {"explicit", "content", "context", "ambiguous"}
CASE_KEYS: Final = {"id", "category", "text", "voice", "recent", "expect", "intent"}
NOW: Final = DEFAULT_START
CFG: Final = domain.route_settings(make_default_snapshot())
REGISTRY: Final = make_project_views(*SEED["projects"], aliases=SEED["aliases"])


def _expected_projects(case: dict[str, object]) -> list[str]:
    expect = case["expect"]
    assert isinstance(expect, dict)
    found = [expect["project"]] if "project" in expect else list(expect.get("ask", []))
    return [str(project) for project in found]


def _decide(case: dict[str, object]) -> tuple[RouteDecision, domain.Mentions]:
    """The case through names and context with the LLM unavailable, as `routing.service` would decide it."""
    text = str(case["text"])
    part = make_voice_part(1, at_s=0, transcript=text) if case.get("voice") else make_part(1, text=text)
    item = make_turn_input(part)
    recent = case.get("recent") or []
    assert isinstance(recent, list)
    activity, running = [], []
    for entry in recent:
        kind = ActivityKind(entry["kind"])
        if kind is ActivityKind.RUNNING:
            running.append(
                make_running_turn(entry["project"], last_part_at=NOW - timedelta(minutes=entry["minutes_ago"]))
            )
        else:
            activity.append(
                make_activity(entry["project"], at=NOW - timedelta(minutes=entry["minutes_ago"]), kind=kind)
            )
    facts = RouteFacts(running=tuple(running))
    plan = domain.plan_route(item, REGISTRY, facts, activity, now=NOW, window=CFG.context_window)
    decision = domain.with_follow_up(domain.decide(plan, None, CFG), item, facts.running, CFG)
    return decision, plan.mentions


# --- format ---------------------------------------------------------------------------------------------------------
def test_the_seed_is_written_against_the_owners_real_folders() -> None:
    assert tuple(SEED["projects"]) == OWNER_PROJECT_NAMES
    assert set(SEED["aliases"].values()) <= set(OWNER_PROJECT_NAMES)


def test_the_seed_has_at_least_40_cases_over_15_projects_in_every_subset() -> None:
    assert len(CASES) >= 40
    assert Counter(case["category"] for case in CASES).keys() == CATEGORIES
    named = {project for case in CASES for project in _expected_projects(case)}
    assert len(named) >= THRESHOLDS["projects_min"]
    assert named <= set(OWNER_PROJECT_NAMES)


@pytest.mark.parametrize("case", CASES, ids=[str(case["id"]) for case in CASES])
def test_each_case_is_well_formed_and_never_a_reply(case: dict[str, object]) -> None:
    assert set(case) <= CASE_KEYS
    assert case["category"] in CATEGORIES
    assert isinstance(case["text"], str)
    assert case["text"].strip()
    expect = case["expect"]
    assert isinstance(expect, dict)
    assert len(expect) == 1
    assert set(expect) <= {"project", "ask", "asks", "no_project"}
    if "intent" in case:
        assert case["intent"] in {intent.value for intent in domain.LLM_INTENTS}
    assert "reply" not in str(case).lower()  # UC-10: the eval has no replies


def test_case_ids_are_unique() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))


# --- the deterministic layer ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=[str(case["id"]) for case in CASES])
def test_without_the_llm_no_case_is_routed_to_the_wrong_project(case: dict[str, object]) -> None:
    decision, mentions = _decide(case)
    expected = _expected_projects(case)
    expect = case["expect"]
    assert isinstance(expect, dict)
    routed = decision.outcome in {Outcome.ROUTE, Outcome.MERGE}
    if routed:
        assert "project" in expect, f"routed {decision.project_id} where a question or no project was expected"
        assert decision.project_id == expected[0]
    if case["category"] == "explicit" and mentions.named == (expected[0],):
        assert routed, "a case naming exactly its project routes with no model"
    if case["category"] == "context":
        assert routed, "one active project takes the follow-up when the model is unavailable"
    if case["category"] == "ambiguous":
        assert decision.outcome is Outcome.ASK
        precise = {*mentions.named, *mentions.lookalike, *mentions.ambiguous}
        if set(expected) <= precise:
            assert set(expected) <= set(decision.candidate_ids)
    if decision.outcome is Outcome.ASK:
        assert len(decision.candidate_ids) <= domain.MAX_CANDIDATES
