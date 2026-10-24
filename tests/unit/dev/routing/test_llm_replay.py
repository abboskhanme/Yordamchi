"""The LLM route on a given answer: each scenario scripts what the routing model says, so every branch of the
decision is reached on purpose (a confident pick, a small margin, a pick outside the family, a worktree in context,
a projectless intent). These answers were authored for T20.

What the real model says is the routing eval's job (T26, `tests/eval/routing/`): its answers are recorded live into
`tests/fixtures/llm/routing/<fixture_key(request)>.json`, keyed by the system prompt, the input, the model and the
schema. A changed prompt or input is a stale key and fails loudly (the last test here); `pytest -m live --record-llm
tests/eval/routing` re-records.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from tests.factories import (
    ROUTING_FIXTURES,
    Fakes,
    add_registered_projects,
    make_closed_burst,
    make_part,
    routing_reply,
)
from tests.fakes.assistant import load_fixtures
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.routing import domain, service, store
from yordamchi.dev.routing.domain import Intent, Outcome

DESCRIPTIONS = {
    "BonviHR": "HR tizimi: nomzodlar, xodimlar, ta'tillar va ish haqi.",
    "HRMentor": "HR bo'yicha maslahatchi agent: mentorlik va tavsiyalar.",
    "TezTel": "Telefon do'koni uchun onlayn katalog va buyurtmalar.",
    "WunderkindLC": "Wunderkind o'quv markazi: guruhlar, davomat va to'lovlar.",
    "WunderkindLMS": "Wunderkind maktabi LMS: darslar, uy vazifalari va baholar.",
    "OvozYoz": "O'zbekcha ovozni matnga aylantiruvchi ilova.",
}
PROJECTS = (*DESCRIPTIONS, "HRMentor-wt-agent-api")
EVAL_CASES = Path(__file__).resolve().parents[3] / "eval" / "routing" / "cases.yaml"


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    text: str
    answer: dict[str, JsonValue]  # what the routing model answered (the recorded reply)
    outcome: Outcome
    project: str | None = None
    candidates: tuple[str, ...] = ()
    intent: Intent = Intent.INSTRUCT
    spoken: tuple[tuple[str, int], ...] = ()  # (project, minutes ago) routed before the message


SCENARIOS = (
    Scenario(
        "content_clear",
        "nomzodlar formasi ishlamayapti",
        routing_reply("BonviHR", 0.93, candidates={"HRMentor": 0.05}),
        Outcome.ROUTE,
        "BonviHR",
    ),
    Scenario(
        "context_follow_up",
        "testlarini ham yoz",
        routing_reply("HRMentor", 0.9),
        Outcome.ROUTE,
        "HRMentor",
        spoken=(("HRMentor", 3),),
    ),
    Scenario(
        "ambiguous_login",
        "login sahifasini tuzat",
        routing_reply("BonviHR", 0.5, candidates={"HRMentor": 0.45}),
        Outcome.ASK,
        candidates=("BonviHR", "HRMentor"),
    ),
    Scenario(
        "family_picked",
        "Wunderkind'dagi to'lov xatosini tuzat",
        routing_reply("WunderkindLC", 0.88, candidates={"WunderkindLMS": 0.08}),
        Outcome.ROUTE,
        "WunderkindLC",
    ),
    Scenario(
        "family_outside_choice",
        "Wunderkind'da ovozli xabarlarni matnga aylantir",
        routing_reply("OvozYoz", 0.7, candidates={"WunderkindLMS": 0.2}),
        Outcome.ASK,
        candidates=("WunderkindLMS", "WunderkindLC"),
    ),
    Scenario(
        "report_all",
        "bugun nima o'zgardi?",
        routing_reply(None, 0.9, intent="report_all"),
        Outcome.NO_PROJECT,
        intent=Intent.REPORT_ALL,
    ),
    Scenario(
        "explicit_report",
        "BonviHR qanday?",
        routing_reply("BonviHR", 0.99, intent="report_project"),
        Outcome.ROUTE,
        "BonviHR",
        intent=Intent.REPORT,
    ),
    Scenario(
        "worktree_in_context",
        "endi testlarni yoz",
        routing_reply("HRMentor", 0.9),
        Outcome.ASK,
        candidates=("HRMentor", "HRMentor-wt-agent-api"),
        spoken=(("HRMentor-wt-agent-api", 2),),
    ),
    Scenario(
        "new_project",
        "yangi loyiha: kafe uchun buyurtma boti",
        routing_reply(None, 0.95, intent="new_project", is_new_project=True),
        Outcome.NO_PROJECT,
        intent=Intent.NEW_PROJECT,
    ),
)


async def run_scenario(deps: DevDeps, fakes: Fakes, scenario: Scenario) -> service.Routed | None:
    """The registry, the routes before the message, then the message itself, on virtual time. The recorder that
    writes the fixtures runs exactly this, so the asks match."""
    now = fakes.clock.now()
    async with deps.db.uow() as s:
        await add_registered_projects(s, *PROJECTS, descriptions=DESCRIPTIONS)
        for number, (project, minutes) in enumerate(scenario.spoken, start=1):
            await store.add_route_decision(
                s,
                burst_id=None,
                at=now - timedelta(minutes=minutes),
                method="explicit",
                intent="instruct_project",
                project_id=project,
                candidates=domain.candidates_json([domain.Candidate(project, 1.0)]),
                confidence_milli=1000 - number,
                asked=False,
                model=None,
                latency_ms=0,
            )
        item = await make_closed_burst(s, make_part(1, text=scenario.text, start=now))
    return await service.route_burst(deps, item)


@pytest.fixture
def replay(fakes: Fakes) -> Fakes:
    fakes.assistant.fixtures = load_fixtures(ROUTING_FIXTURES)
    return fakes


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario.id for scenario in SCENARIOS])
async def test_the_llm_route_on_a_scripted_answer(dev_deps: DevDeps, fakes: Fakes, scenario: Scenario) -> None:
    fakes.assistant.reply_with("routing", scenario.answer)

    routed = await run_scenario(dev_deps, fakes, scenario)

    assert routed is not None
    decision = routed.decision
    assert (decision.outcome, decision.project_id, decision.intent) == (
        scenario.outcome,
        scenario.project,
        scenario.intent,
    )
    if scenario.outcome is Outcome.ASK:
        assert set(scenario.candidates) <= set(decision.candidate_ids)
        assert len(decision.candidate_ids) <= domain.MAX_CANDIDATES
    [request] = fakes.assistant.requests
    assert request.kind == "routing"


def test_every_routing_recording_is_live_and_names_its_eval_case() -> None:
    """The recordings are the real model's answers to the routing eval (T26), never authored ones: each file says it
    was recorded live and which case asked it. The eval itself checks that no recording is stale."""
    cases = yaml.safe_load(EVAL_CASES.read_text(encoding="utf-8"))["cases"]
    case_ids = {str(case["id"]) for case in cases}
    recordings = [json.loads(path.read_text(encoding="utf-8")) for path in ROUTING_FIXTURES.glob("*.json")]
    assert len(recordings) == len(case_ids)
    assert all(str(data["source"]).startswith("live") for data in recordings)
    assert {data["case"] for data in recordings} == case_ids


async def test_a_changed_input_is_a_stale_key_and_fails_loudly(dev_deps: DevDeps, replay: Fakes) -> None:
    changed = Scenario("changed", "nomzodlar formasi umuman ishlamayapti", {}, Outcome.ROUTE)
    with pytest.raises(LookupError, match="re-record"):
        await run_scenario(dev_deps, replay, changed)
