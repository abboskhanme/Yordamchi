"""The LLM route on replayed answers (CONVENTIONS §11): each scenario's routing ask is answered from
`tests/fixtures/llm/routing/<fixture_key(request)>.json`, keyed by the system prompt, the input, the model and the
schema. A changed prompt or input is a stale key and fails loudly; `pytest -m live --record-llm` (T26) re-records.
The answers in this suite were authored for T20 (`"source"` in each file); T26 records them live with the owner's
real registry.
"""

import json
from dataclasses import dataclass
from datetime import timedelta

import pytest

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
async def test_the_llm_route_on_a_recorded_answer(dev_deps: DevDeps, replay: Fakes, scenario: Scenario) -> None:
    routed = await run_scenario(dev_deps, replay, scenario)

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
    [request] = replay.assistant.requests
    assert request.kind == "routing"


def test_each_scenario_has_exactly_one_recorded_answer() -> None:
    """A fixture no scenario asks for is stale (its prompt or input changed); every file says which case it is."""
    cases = sorted(json.loads(path.read_text(encoding="utf-8"))["case"] for path in ROUTING_FIXTURES.glob("*.json"))
    assert cases == sorted(scenario.id for scenario in SCENARIOS)


async def test_a_changed_input_is_a_stale_key_and_fails_loudly(dev_deps: DevDeps, replay: Fakes) -> None:
    changed = Scenario("changed", "nomzodlar formasi umuman ishlamayapti", {}, Outcome.ROUTE)
    with pytest.raises(LookupError, match="re-record"):
        await run_scenario(dev_deps, replay, changed)
