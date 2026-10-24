"""routing.domain: names with Uzbek suffixes and typos, every look-alike pair of the owner's folders, the reply and
context signals, the LLM's structured answer, the decision thresholds and the one clarification, the UC-02 follow-up
merge, the owner's answer, and "bu X uchun edi" (SPEC §3.2.6, §6.1; UC-10, UC-02, UC-05)."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.factories import (
    FAKE_KEYS,
    OWNER_PROJECT_NAMES,
    make_activity,
    make_default_snapshot,
    make_part,
    make_project_views,
    make_running_turn,
    make_turn_input,
    make_voice_part,
    routing_reply,
)
from tests.fakes.clock import DEFAULT_START
from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.projects.domain import Project
from yordamchi.dev.routing import domain
from yordamchi.dev.routing.domain import (
    NO_FACTS,
    ActivityKind,
    Candidate,
    ChoiceKind,
    ClarificationKind,
    Intent,
    LlmRoute,
    Method,
    OpenClarification,
    Outcome,
    PastRoute,
    RouteDecision,
    RouteFacts,
    RoutePlan,
)
from yordamchi.shared.burst.domain import BurstPart, CloseReason, TurnInput
from yordamchi.shared.inbox.domain import UpdateKind

NOW = DEFAULT_START
REGISTRY = make_project_views(*OWNER_PROJECT_NAMES, aliases={"hr": "BonviHR"})
CFG = domain.route_settings(make_default_snapshot())
WUNDERKIND = tuple(name for name in OWNER_PROJECT_NAMES if name.startswith("Wunderkind"))


def _mentions(text: str, projects: tuple[Project, ...] = REGISTRY) -> domain.Mentions:
    return domain.find_mentions(text, projects)


def _item(*texts: str, burst_id: int = 1) -> TurnInput:
    parts = [make_part(index + 1, at_s=float(index), text=text) for index, text in enumerate(texts)]
    return make_turn_input(*parts, burst_id=burst_id)


def _plan(
    item: TurnInput,
    *,
    facts: RouteFacts = NO_FACTS,
    spoken: tuple[str, ...] = (),
    projects: tuple[Project, ...] = REGISTRY,
) -> RoutePlan:
    activity = [make_activity(project, at=NOW - timedelta(minutes=3)) for project in spoken]
    return domain.plan_route(item, projects, facts, activity, now=NOW, window=CFG.context_window)


def _llm(project: str | None, confidence: float, **others: float) -> LlmRoute:
    route = domain.parse_llm_route(routing_reply(project, confidence, candidates=others), REGISTRY)
    assert route is not None
    return route


def _decide(text: str, llm: LlmRoute | None, *, spoken: tuple[str, ...] = ()) -> RouteDecision:
    return domain.decide(_plan(_item(text), spoken=spoken), llm, CFG)


# --- text -----------------------------------------------------------------------------------------------------------
def test_folding_unifies_case_apostrophes_and_spaces() -> None:
    assert domain.fold("  BonviHR’da   O‘zbek ") == "bonvihr'da o'zbek"
    assert domain.fold("Toʻxta`") == "to'xta'"
    assert domain.compact("MegaPlus ERP") == domain.compact("megaplus-erp") == "megapluserp"


def test_words_keep_hyphens_and_suffix_apostrophes_inside() -> None:
    assert domain.tokens("HRMentor-wt-agent-api'da, «BonviHR»ga!") == ("hrmentor-wt-agent-api'da", "bonvihr", "ga")
    assert domain.tokens("@Yordamchi.") == ("@yordamchi",)


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [
        ("hrmentor", "hrmentor", 0),
        ("hrmentr", "hrmentor", 1),  # deletion
        ("hrmentorr", "hrmentor", 1),  # insertion
        ("hrmantor", "hrmentor", 1),  # substitution
        ("hrmetnor", "hrmentor", 1),  # neighbours swapped (Damerau)
        ("bonviimpo", "bonviimport", 2),
        ("wunderkindlc", "wunderkindlms", 2),
    ],
)
def test_typo_distance(a: str, b: str, distance: int) -> None:
    assert domain.typo_distance(a, b) == distance
    assert domain.within_one_typo(a, b) is (distance <= 1)


# --- signal 1: names ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "project"),
    [
        ("BonviHR'da login sahifasini tuzat", "BonviHR"),
        ("hrmentorga test yoz", "HRMentor"),  # unquoted suffix
        ("Yordamchi'ning testlarini yurgiz", "Yordamchi"),
        ("OvozYozdagi STT xatosi", "OvozYoz"),
        ("BonviHR-ga deploy", "BonviHR"),  # a hyphen sets the suffix off
        ("megaplus erp'da hisobot", "MegaPlus ERP"),  # a two-word folder name
        ("MegaPlus Platformda", "MegaPlus Platform"),
        ("ʻBonviHRʼ", "BonviHR"),
        ("hr'da xato bor", "BonviHR"),  # the owner's alias
        ("zinnur-v2'da", "zinnur-v2"),
        ("Wooden'ga rasm qo'sh", "Wooden"),
    ],
)
def test_a_name_with_an_uzbek_suffix_is_named(text: str, project: str) -> None:
    found = _mentions(text)
    assert found.named == (project,)
    assert (found.typo, found.lookalike, found.ambiguous) == ((), (), ())


@pytest.mark.parametrize(
    ("text", "project"),
    [
        ("hrmentr'da", "HRMentor"),  # one typo, ≥ 6 letters
        ("BonviImpor'da", "BonviImport"),
        ("megapluserp", "MegaPlus ERP"),  # written without its space
        ("Wunderkind-LC", "WunderkindLC"),
        ("bonvi platform", "Bonvi-platform"),  # written apart after a family word
        ("Wunderkind LMS'da", "WunderkindLMS"),
        ("megaplus platfrom", "MegaPlus Platform"),  # a two-word name with one typo
    ],
)
def test_one_typo_or_another_spelling_still_names_a_routable_project(text: str, project: str) -> None:
    found = _mentions(text)
    assert found.named == (project,)
    assert found.typo == (project,)


def test_a_typo_is_never_across_a_digit_and_short_words_are_never_typos() -> None:
    projects = make_project_views("BonviHR", "BonviHR2")
    assert _mentions("BonviHR2'da", projects).named == ("BonviHR2",)
    assert _mentions("bonvihr'da", projects).named == ("BonviHR",)
    assert _mentions("bonvihr3", projects).named == ()  # a third project the owner has not made
    assert _mentions("bonvhr", make_project_views("BonviHR")).named == ("BonviHR",)
    assert _mentions("teztl", make_project_views("TezTel")).named == ()  # 5 letters: too short for a typo


# --- look-alikes: never confused ---
def test_hrmentor_and_its_worktree_never_stand_in_for_each_other() -> None:
    assert _mentions("HRMentor'da testlarni yoz").named == ("HRMentor",)
    exact = _mentions("HRMentor-wt-agent-api'da xatoni tuzat")
    assert (exact.named, exact.lookalike) == (("HRMentor-wt-agent-api",), ())
    for near in ("HRMentor wt agent api'da", "HRMentor wt'da", "hrmentor-wt-agent-apj"):
        found = _mentions(near)
        assert found.lookalike == ("HRMentor-wt-agent-api",), near
        assert "HRMentor-wt-agent-api" not in found.named


@pytest.mark.parametrize("name", WUNDERKIND)
def test_each_wunderkind_folder_is_its_own_project(name: str) -> None:
    found = _mentions(f"{name}'da xatoni tuzat")
    assert found.named == (name,)
    assert (found.family, found.ambiguous, found.weak) == ((), (), ())


def test_a_bare_wunderkind_is_a_family_and_a_half_name_is_ambiguous() -> None:
    assert _mentions("Wunderkind'dagi to'lov xatosi").family == WUNDERKIND
    for half in ("WunderkindL", "wunderkindlm"):
        found = _mentions(half)
        assert (found.named, found.ambiguous) == ((), ("WunderkindLC", "WunderkindLMS")), half


@pytest.mark.parametrize(
    ("text", "named", "lookalike"),
    [
        ("bonvicall'da", ("BonviCall",), ()),
        ("BonviCall Clone Archive'dagi kod", ("BonviCall Clone Archive",), ()),
        ("BonviCall clone'da", ("BonviCall",), ("BonviCall Clone Archive",)),
        ("Bonvi Call'da", ("BonviCall",), ()),
    ],
)
def test_bonvicall_and_its_archive_never_stand_in_for_each_other(
    text: str, named: tuple[str, ...], lookalike: tuple[str, ...]
) -> None:
    found = _mentions(text)
    assert (found.named, found.lookalike) == (named, lookalike)


@pytest.mark.parametrize(
    ("text", "named", "family", "ambiguous"),
    [
        ("bonviimport'da", ("BonviImport",), (), ()),
        ("bonviimzo'da", ("BonviImzo",), (), ()),
        ("bonviimpo'da", (), (), ("BonviImzo", "BonviImport")),  # one typo from one, two from the other
        ("megaplus'da", (), ("MegaPlus ERP", "MegaPlus Platform"), ()),
        ("AIAgents'ga", ("AIAgents",), (), ()),
        ("AI-Phone'da", ("AI-Phone",), (), ()),
        ("ImportAgents", ("ImportAgents",), (), ()),
        ("AgentsRoom'da", ("AgentsRoom",), (), ()),
    ],
)
def test_other_look_alike_pairs_stay_apart(
    text: str, named: tuple[str, ...], family: tuple[str, ...], ambiguous: tuple[str, ...]
) -> None:
    found = _mentions(text)
    assert (found.named, found.family, found.ambiguous) == (named, family, ambiguous)


def test_a_short_name_is_named_only_with_a_suffix() -> None:
    assert _mentions("AI'da ovoz qo'sh").named == ("AI",)
    assert _mentions("BS'ga yangi sahifa").named == ("BS",)
    bare = _mentions("AI agentlar haqida o'yla")
    assert (bare.named, bare.weak[:1]) == ((), ("AI",))
    assert _mentions("bs bilan gaplash").weak == ("BS",)
    assert _mentions("Aida bilan uchrashuv").named == ()  # an unquoted suffix never leaves a short name


def test_a_name_written_apart_without_a_family_word_is_only_a_hint() -> None:
    assert _mentions("ai phone ilovasi").weak == ("AI", "AI-Phone")
    assert _mentions("russian speaking kurs").weak == ("Russian-Speaking",)
    assert _mentions("claude managerda rotatsiya").named == ()


def test_archived_and_missing_projects_answer_only_to_their_exact_name() -> None:
    projects = make_project_views("BonviGo", "TezTel", archived=("BonviGo",), missing=("TezTel",))
    assert _mentions("BonviGo'da", projects).named == ("BonviGo",)
    assert _mentions("bonviggo'da", projects).lookalike == ("BonviGo",)  # a typo of an archived name asks
    assert _mentions("TezTel'da", projects).named == ("TezTel",)
    assert _mentions("teztell'da", projects).named == ()
    assert _mentions("bonvi", projects).family == ()


def test_two_projects_named_come_back_in_text_order() -> None:
    assert _mentions("HRMentor'dagi kabi BonviHR'da ham qil").named == ("HRMentor", "BonviHR")


# --- signals 2 and 3: reply and context -----------------------------------------------------------------------------
def test_a_reply_routes_unless_another_name_is_said() -> None:
    reply = make_part(1, text="buni ham tuzat", reply_to_message_id=900)
    facts = RouteFacts(reply_projects={900: "BonviHR"})
    plan = _plan(make_turn_input(reply), facts=facts)
    assert (plan.fixed, plan.fixed_method) == (Candidate("BonviHR", domain.REPLY_CONFIDENCE), Method.REPLY)

    naming = make_part(1, text="HRMentor'dagi kabi qil", reply_to_message_id=900)
    conflict = _plan(make_turn_input(naming), facts=facts)
    assert (conflict.fixed, conflict.pool) == (None, ("HRMentor", "BonviHR"))  # the LLM picks, else ask

    same = make_part(1, text="BonviHR'da ham", reply_to_message_id=900)
    assert _plan(make_turn_input(same), facts=facts).fixed_method is Method.EXPLICIT


def test_replies_to_two_projects_are_no_signal() -> None:
    parts = (make_part(1, text="a", reply_to_message_id=900), make_part(2, at_s=1, text="b", reply_to_message_id=901))
    facts = RouteFacts(reply_projects={900: "BonviHR", 901: "HRMentor"})
    assert _plan(make_turn_input(*parts), facts=facts).reply_project is None


def test_recent_context_counts_spoken_reported_and_running_within_the_window() -> None:
    old = NOW - CFG.context_window - timedelta(seconds=1)
    activity = [
        make_activity("BonviHR", at=NOW - timedelta(minutes=2)),
        make_activity("HRMentor", at=NOW - timedelta(minutes=9), kind=ActivityKind.REPORTED),
        make_activity("TezTel", at=old),
    ]
    running = [make_running_turn("OvozYoz")]
    found = domain.recent_projects(activity, running, now=NOW, window=CFG.context_window)
    assert found == ("OvozYoz", "BonviHR", "HRMentor")


def test_spoken_activity_follows_corrections() -> None:
    past = [
        PastRoute(1, 10, NOW, Method.EXPLICIT, Intent.INSTRUCT, "BonviHR", "HRMentor", asked=False),
        PastRoute(2, 11, NOW, Method.LLM, Intent.INSTRUCT, None, None, asked=True),
    ]
    assert [entry.project_id for entry in domain.spoken_activity(past)] == ["HRMentor"]


# --- signal 4: the LLM answer ---------------------------------------------------------------------------------------
def test_the_llm_answer_resolves_ids_and_orders_the_alternatives() -> None:
    raw = routing_reply("bonvihr", 0.8, candidates={"HRMentor": 0.3, "Nowhere": 0.9, "TezTel": 0.5})
    route = domain.parse_llm_route(raw, REGISTRY)
    assert route is not None
    assert route.project_id == "BonviHR"
    assert route.ranked == (Candidate("BonviHR", 0.8), Candidate("TezTel", 0.5), Candidate("HRMentor", 0.3))


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "BonviHR",
        routing_reply("BonviHR", 0.9, intent="delete_everything"),
        {**routing_reply("BonviHR", 0.9), "confidence": "high"},
        {**routing_reply("BonviHR", 0.9), "confidence": True},
        {**routing_reply("BonviHR", 0.9), "intent": None},
    ],
)
def test_an_unusable_llm_answer_is_none(raw: JsonValue) -> None:
    assert domain.parse_llm_route(raw, REGISTRY) is None


def test_a_new_project_flag_makes_the_intent_new_project_and_values_are_clipped() -> None:
    route = domain.parse_llm_route(routing_reply(None, 1.7, is_new_project=True, intent="instruct_project"), REGISTRY)
    assert route is not None
    assert (route.intent, route.confidence, route.project_id) == (Intent.NEW_PROJECT, 1.0, None)


def test_the_output_schema_lists_the_routing_intents_and_requires_every_field() -> None:
    schema = domain.OUTPUT_SCHEMA
    properties = schema["properties"]
    assert isinstance(properties, dict)
    intent = properties["intent"]
    assert isinstance(intent, dict)
    assert intent["enum"] == [value.value for value in domain.LLM_INTENTS]
    required = schema["required"]
    assert isinstance(required, list)
    assert set(map(str, required)) == set(properties)


def test_the_system_prompt_renders_with_its_fixed_slots_only() -> None:
    text = prompts.render("dev/routing_system", **domain.prompt_slots())
    assert "{{" not in text
    for intent in domain.LLM_INTENTS:
        assert f"`{intent.value}`" in text
    assert "HRMentor-wt-agent-api" in text  # the look-alike rule names the pair
    assert set(domain.prompt_slots()) == {"intents", "max_candidates"}  # no persona, tone or owner text


def test_the_llm_input_is_canonical_json_with_the_allowed_table() -> None:
    projects = make_project_views("BonviHR", "HRMentor", "HRMentor-wt-agent-api", descriptions={"BonviHR": "HR tizimi"})
    item = _item("login sahifasi ishlamayapti")
    plan = _plan(item, projects=projects, spoken=("HRMentor",))
    text = domain.llm_prompt(
        item,
        plan,
        projects,
        NO_FACTS,
        activity=[make_activity("HRMentor", at=NOW - timedelta(minutes=3))],
        now=NOW,
        window=CFG.context_window,
    )
    payload = json.loads(text)
    assert [row["id"] for row in payload["projects"]] == ["BonviHR", "HRMentor"]  # the worktree is not routable
    assert payload["projects"][0]["description"] == "HR tizimi"
    assert payload["recent"] == [{"kind": "spoken", "minutes_ago": 3, "project": "HRMentor"}]
    assert payload["message"] == [{"kind": "text", "text": "login sahifasi ishlamayapti"}]
    assert "choose_from" not in payload
    assert text == domain.llm_prompt(
        item,
        plan,
        projects,
        NO_FACTS,
        activity=[make_activity("HRMentor", at=NOW - timedelta(minutes=3))],
        now=NOW,
        window=CFG.context_window,
    )


def test_with_the_project_fixed_the_llm_only_classifies_the_intent() -> None:
    item = _item("BonviHR qanday?")
    plan = _plan(item)
    payload = json.loads(
        domain.llm_prompt(item, plan, REGISTRY, NO_FACTS, activity=(), now=NOW, window=CFG.context_window)
    )
    assert payload["choose_from"] == ["BonviHR"]
    assert [row["id"] for row in payload["projects"]] == ["BonviHR"]


# --- the decision ---------------------------------------------------------------------------------------------------
def test_one_named_project_routes_without_the_llm() -> None:
    decision = _decide("BonviHR'da login sahifasini tuzat", None)
    assert (decision.outcome, decision.method, decision.project_id) == (Outcome.ROUTE, Method.EXPLICIT, "BonviHR")
    assert (decision.confidence, decision.intent) == (1.0, Intent.INSTRUCT)
    assert _decide("hrmentr'da test yoz", None).confidence == domain.TYPO_CONFIDENCE


def test_a_named_project_keeps_the_llms_intent_and_freshness() -> None:
    report = replace(_llm("BonviHR", 0.99), intent=Intent.REPORT, needs_fresh=True)
    decision = _decide("BonviHR qanday?", report)
    assert (decision.project_id, decision.intent, decision.needs_fresh) == ("BonviHR", Intent.REPORT, True)
    everything = replace(_llm(None, 0.9), intent=Intent.REPORT_ALL)
    assert _decide("BonviHR bo'yicha hammasi", everything).intent is Intent.REPORT  # one project: a report on it
    elsewhere = _llm("HRMentor", 0.99)
    assert _decide("BonviHR'da tuzat", elsewhere).project_id == "BonviHR"  # a name is not overruled


def test_a_look_alike_or_a_typo_between_two_names_asks_whatever_the_llm_says() -> None:
    sure = _llm("HRMentor", 0.99)
    near = _decide("HRMentor wt'da testlarni yoz", sure)
    assert (near.outcome, near.reason) == (Outcome.ASK, "lookalike")
    assert near.candidate_ids == ("HRMentor", "HRMentor-wt-agent-api")
    typo = _decide("bonviimpo'da xato", _llm("BonviImport", 0.99))
    assert (typo.outcome, typo.candidate_ids) == (Outcome.ASK, ("BonviImzo", "BonviImport"))


def test_two_named_projects_let_the_llm_pick_one_of_them_or_ask() -> None:
    text = "HRMentor'dagi login kodini BonviHR'ga ko'chir"
    picked = _decide(text, _llm("BonviHR", 0.9, HRMentor=0.1))
    assert (picked.outcome, picked.method, picked.project_id) == (Outcome.ROUTE, Method.LLM, "BonviHR")
    unsure = _decide(text, _llm("BonviHR", 0.6, HRMentor=0.5))
    assert (unsure.outcome, unsure.candidate_ids) == (Outcome.ASK, ("BonviHR", "HRMentor"))
    offline = _decide(text, None)
    assert (offline.outcome, offline.candidate_ids) == (Outcome.ASK, ("HRMentor", "BonviHR"))


def test_a_family_word_limits_the_pick_to_that_family() -> None:
    text = "Wunderkind'dagi to'lov xatosini tuzat"
    assert _decide(text, _llm("WunderkindLC", 0.9)).project_id == "WunderkindLC"
    outside = _decide(text, _llm("BonviFinance", 0.95))
    assert (outside.outcome, outside.reason) == (Outcome.ASK, "outside_choice")
    assert set(outside.candidate_ids) <= set(WUNDERKIND)
    assert len(outside.candidate_ids) == domain.MAX_CANDIDATES


@pytest.mark.parametrize(
    ("top", "second", "outcome", "reason"),
    [
        (0.75, 0.55, Outcome.ROUTE, "llm"),  # both thresholds met exactly (routing.min_confidence, min_margin)
        (0.74, 0.0, Outcome.ASK, "low_confidence"),
        (0.80, 0.61, Outcome.ASK, "small_margin"),
        (0.95, 0.0, Outcome.ROUTE, "llm"),
    ],
)
def test_the_llm_routes_only_above_the_confidence_and_margin_thresholds(
    top: float, second: float, outcome: Outcome, reason: str
) -> None:
    others = {"HRMentor": second} if second else {}
    decision = _decide("login sahifasi ishlamayapti", _llm("BonviHR", top, **others))
    assert (decision.outcome, decision.reason) == (outcome, reason)
    if outcome is Outcome.ASK:
        assert decision.candidate_ids[0] == "BonviHR"
        assert decision.project_id is None


def test_the_thresholds_are_read_from_settings() -> None:
    strict = replace(CFG, min_confidence=0.95)
    plan = _plan(_item("login sahifasi ishlamayapti"))
    assert domain.decide(plan, _llm("BonviHR", 0.9), strict).outcome is Outcome.ASK
    assert domain.decide(plan, _llm("BonviHR", 0.9), CFG).outcome is Outcome.ROUTE


def test_a_null_pick_never_routes_even_with_confident_candidates() -> None:
    decision = _decide("login sahifasi", _llm(None, 0.0, BonviHR=0.9, HRMentor=0.4))
    assert (decision.outcome, decision.reason, decision.candidate_ids) == (
        Outcome.ASK,
        "no_pick",
        ("BonviHR", "HRMentor"),
    )


def test_without_the_llm_one_active_project_takes_it_and_otherwise_the_bot_asks() -> None:
    lone = _decide("testlarini ham yoz", None, spoken=("HRMentor",))
    assert (lone.outcome, lone.method, lone.project_id) == (Outcome.ROUTE, Method.CONTEXT, "HRMentor")
    assert lone.confidence == domain.CONTEXT_CONFIDENCE
    two = _decide("testlarini ham yoz", None, spoken=("HRMentor", "BonviHR"))
    assert (two.outcome, set(two.candidate_ids)) == (Outcome.ASK, {"HRMentor", "BonviHR"})
    nothing = _decide("testlarini ham yoz", None)
    assert (nothing.outcome, nothing.candidates) == (Outcome.ASK, ())


def test_a_follow_up_never_slips_into_a_look_alike_of_the_worktree_just_used() -> None:
    """The owner worked on `HRMentor-wt-agent-api` by its exact name; "testlarni yoz" must not go to `HRMentor`."""
    offline = _decide("testlarni ham yoz", None, spoken=("HRMentor-wt-agent-api",))
    assert (offline.outcome, offline.candidate_ids) == (Outcome.ASK, ("HRMentor-wt-agent-api",))
    llm = _decide("testlarni ham yoz", _llm("HRMentor", 0.95), spoken=("HRMentor-wt-agent-api",))
    assert (llm.outcome, llm.reason) == (Outcome.ASK, "lookalike_context")
    assert llm.candidate_ids == ("HRMentor", "HRMentor-wt-agent-api")


def test_a_lone_screenshot_goes_to_the_active_project_as_a_read_only_look() -> None:
    item = make_turn_input(make_part(1, kind=UpdateKind.PHOTO))
    plan = _plan(item, spoken=("BonviHR",))
    for llm in (None, _llm("HRMentor", 0.3)):
        decision = domain.decide(plan, llm, CFG)
        assert (decision.outcome, decision.project_id) == (Outcome.ROUTE, "BonviHR")
    assert domain.decide(plan, None, CFG).intent is Intent.INVESTIGATE


@pytest.mark.parametrize("intent", [Intent.REPORT_ALL, Intent.SERVER_STATUS])
def test_projectless_reports_need_no_project_and_no_question(intent: Intent) -> None:
    decision = _decide("bugun nima o'zgardi?", replace(_llm(None, 0.4), intent=intent))
    assert (decision.outcome, decision.intent, decision.project_id) == (Outcome.NO_PROJECT, intent, None)


def test_a_confident_new_project_is_no_route_and_an_unsure_one_is_ordinary() -> None:
    new = replace(_llm(None, 0.9), intent=Intent.NEW_PROJECT)
    assert _decide("yangi loyiha: kafe uchun bot", new).outcome is Outcome.NO_PROJECT
    unsure = replace(_llm("BonviHR", 0.5), intent=Intent.NEW_PROJECT)
    assert _decide("kafe uchun bot", unsure).outcome is Outcome.ASK


def test_a_question_names_at_most_three_candidates_most_likely_first() -> None:
    decision = _decide("hisobot sahifasi", _llm("BonviHR", 0.4, HRMentor=0.35, TezTel=0.3, BonviGo=0.2))
    assert decision.candidate_ids == ("BonviHR", "HRMentor", "TezTel")
    assert decision.confidence == 0.4


# --- UC-02: a follow-up of a running turn ---------------------------------------------------------------------------
def _follow(decision: RouteDecision, item: TurnInput, *running: str, last_s: float = -60.0) -> RouteDecision:
    turns = [make_running_turn(project, last_part_at=NOW + timedelta(seconds=last_s)) for project in running]
    return domain.with_follow_up(decision, item, turns, CFG)


def test_a_continuation_joins_the_running_turn_instead_of_a_question() -> None:
    item = _item("yana registratsiyani ham")
    asked = domain.decide(_plan(item), None, CFG)
    assert asked.outcome is Outcome.ASK
    merged = _follow(asked, item, "BonviHR", last_s=-1.0)  # began within burst.after_text_s of its last part
    assert (merged.outcome, merged.project_id, merged.reason) == (Outcome.MERGE, "BonviHR", "continuation")


def test_a_follow_up_for_another_project_is_never_merged() -> None:
    item = _item("HRMentor'da test yoz")
    decision = domain.decide(_plan(item), None, CFG)
    assert _follow(decision, item, "BonviHR", last_s=-1.0) == decision


def test_a_confident_route_to_the_running_project_steers_and_a_context_guess_does_not() -> None:
    item = _item("BonviHR'da registratsiyani ham")
    named = domain.decide(_plan(item), None, CFG)
    assert _follow(named, item, "BonviHR").outcome is Outcome.MERGE
    guess = RouteDecision(Outcome.ROUTE, Method.CONTEXT, Intent.INSTRUCT, "BonviHR", domain.CONTEXT_CONFIDENCE)
    assert _follow(guess, _item("yana bitta"), "BonviHR") == guess  # below burst.merge_min_confidence


def test_a_report_never_steers_a_running_turn_and_two_running_turns_merge_nothing() -> None:
    item = _item("BonviHR qanday?")
    report = replace(domain.decide(_plan(item), None, CFG), intent=Intent.REPORT)
    assert _follow(report, item, "BonviHR").outcome is Outcome.ROUTE
    asked = domain.decide(_plan(_item("yana")), None, CFG)
    assert _follow(asked, _item("yana"), "BonviHR", "HRMentor", last_s=-1.0).outcome is Outcome.ASK


def test_an_interrupt_or_the_self_test_never_reaches_routing() -> None:
    domain.require_routable(_item("BonviHR'da login tuzat"))
    for stop in ("to'xta", "hammasini to'xtat", "kut"):
        with pytest.raises(ValueError, match="pre-parse"):
            domain.require_routable(make_turn_input(make_part(1, text=stop), reason=CloseReason.INTERRUPT))
    selftest = make_turn_input(make_part(1, text="selftest", synthetic=True), reason=CloseReason.SYNTHETIC)
    with pytest.raises(ValueError, match="pre-parse"):
        domain.require_routable(selftest)


def test_an_interrupt_is_never_merged() -> None:
    stop = make_turn_input(make_part(1, text="to'xta"), reason=CloseReason.INTERRUPT)
    decision = RouteDecision(Outcome.ROUTE, Method.CONTEXT, Intent.INSTRUCT, "BonviHR", 0.9)
    assert _follow(decision, stop, "BonviHR", last_s=-1.0).outcome is Outcome.ROUTE


# --- what the owner sees --------------------------------------------------------------------------------------------
def test_the_first_reply_names_the_target() -> None:
    item = _item("BonviHR'da login sahifasini tuzat")
    routed = domain.decide(_plan(item), None, CFG)
    assert domain.first_reply(routed, item, redact.scrubbed) == S.ROUTED(project="BonviHR")
    merged = replace(routed, outcome=Outcome.MERGE)
    assert domain.first_reply(merged, item, redact.scrubbed) == S.ROUTED_MERGED(project="BonviHR")
    report = RouteDecision(Outcome.NO_PROJECT, Method.LLM, Intent.REPORT_ALL, None, 0.9)
    assert domain.first_reply(report, item, redact.scrubbed) is None


def test_a_voice_note_is_echoed_in_one_line_with_the_project() -> None:
    voice = make_voice_part(1, at_s=0, transcript="BonviHR'da login\nsahifasini tuzat")
    item = make_turn_input(voice)
    decision = domain.decide(_plan(item), None, CFG)
    reply = domain.first_reply(decision, item, redact.scrubbed)
    assert reply == S.VOICE_ECHO(text="BonviHR'da login sahifasini tuzat", project="BonviHR")


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (("BonviHR", "HRMentor"), "«login sahifasini tuzat» — BonviHR'gami yoki HRMentor'gami?"),
        (("A1", "B2", "C3"), "«login sahifasini tuzat» — A1'gami, B2'gami yoki C3'gami?"),
        (("BonviHR",), S.ROUTING_WHICH_PROJECT_ONE(material="«login sahifasini tuzat»", first="BonviHR")),
        ((), S.ROUTING_WHICH_PROJECT_ANY(material="«login sahifasini tuzat»")),
    ],
)
def test_the_one_question_quotes_the_material_and_names_the_candidates(
    candidates: tuple[str, ...], expected: str
) -> None:
    item = _item("login sahifasini tuzat")
    decision = RouteDecision(
        Outcome.ASK, Method.LLM, Intent.INSTRUCT, None, 0.5, tuple(Candidate(c, 0) for c in candidates)
    )
    assert domain.first_reply(decision, item, redact.scrubbed) == expected


def test_media_is_named_and_a_quoted_key_is_masked() -> None:
    photo = make_turn_input(make_part(1, kind=UpdateKind.PHOTO))
    assert domain.material(photo, redact.scrubbed) == S.ROUTING_MATERIAL_PHOTO()
    token = FAKE_KEYS["github_token"]
    quoted = domain.material(_item(f"token {token} ni qo'y"), redact.scrubbed)
    assert token not in quoted
    long = domain.material(_item("juda " * 40), redact.scrubbed)
    assert len(long) <= domain.EXCERPT_MAX + 3


# --- the owner's answer ---------------------------------------------------------------------------------------------
TWO = ("BonviHR", "HRMentor")


@pytest.mark.parametrize(
    ("text", "project"),
    [
        ("2", "HRMentor"),
        ("2-si", "HRMentor"),
        ("ikkinchisi", "HRMentor"),
        ("birinchisiga", "BonviHR"),
        ("oxirgisi", "HRMentor"),
        ("ikkinchisi bo'lsin", "HRMentor"),
        ("BonviHR'ga", "BonviHR"),
        ("ha, HRMentor uchun", "HRMentor"),
        ("hrmentr", "HRMentor"),  # a typo of a candidate
        ("yo'q, TezTel'ga", "TezTel"),  # another project, and nothing else said
    ],
)
def test_an_answer_by_number_ordinal_or_name_picks_the_project(text: str, project: str) -> None:
    choice = domain.choose(text, TWO, REGISTRY, accept_yes=True)
    assert (choice.kind, choice.project_id, choice.pure) == (ChoiceKind.PICKED, project, True)


def test_an_answer_that_says_more_picks_and_is_routed_on_its_own_too() -> None:
    choice = domain.choose("BonviHR'da registratsiyani ham tuzat", TWO, REGISTRY, accept_yes=True)
    assert (choice.kind, choice.project_id, choice.pure) == (ChoiceKind.PICKED, "BonviHR", False)


@pytest.mark.parametrize("text", ["TezTel'da yangi sahifa qil", "salom", "", "rahmat", "1 va 2"])
def test_a_message_that_answers_nothing_is_no_choice(text: str) -> None:
    assert domain.choose(text, TWO, REGISTRY, accept_yes=True).kind is ChoiceKind.NONE


def test_out_of_range_cancel_and_yes() -> None:
    assert domain.choose("3", TWO, REGISTRY, accept_yes=True).kind is ChoiceKind.OUT_OF_RANGE
    assert domain.choose("hech biri", TWO, REGISTRY, accept_yes=True).kind is ChoiceKind.CANCELLED
    yes = domain.choose("ha", ("BonviHR",), REGISTRY, accept_yes=True)
    assert (yes.kind, yes.project_id) == (ChoiceKind.PICKED, "BonviHR")
    assert domain.choose("ha", ("BonviHR",), REGISTRY, accept_yes=False).kind is ChoiceKind.NONE  # a decision waits
    assert domain.choose("ha", TWO, REGISTRY, accept_yes=True).kind is ChoiceKind.NONE


def test_a_worktree_candidate_is_picked_by_its_name_even_written_apart() -> None:
    pair = ("HRMentor", "HRMentor-wt-agent-api")
    assert domain.choose("HRMentor wt agent api", pair, REGISTRY, accept_yes=True).project_id == pair[1]
    assert domain.choose("HRMentor", pair, REGISTRY, accept_yes=True).project_id == pair[0]
    assert domain.choose("2", pair, REGISTRY, accept_yes=True).project_id == pair[1]


def test_an_answer_binds_to_the_replied_question_else_the_newest_live_one() -> None:
    def asked(number: int, minutes_ago: int, message_id: int | None) -> OpenClarification:
        at = NOW - timedelta(minutes=minutes_ago)
        return OpenClarification(number, number, ClarificationKind.WHICH_PROJECT, TWO, message_id, at)

    items = [asked(1, 10, 501), asked(2, 2, 502), asked(3, 40, 503)]
    assert domain.live_clarification(items, replies_to=frozenset(), now=NOW) == items[1]
    assert domain.live_clarification(items, replies_to={501}, now=NOW) == items[0]
    assert domain.live_clarification(items, replies_to={503}, now=NOW) == items[1]  # expired: not bound
    assert domain.expired(items, now=NOW) == (items[2],)
    assert domain.live_clarification([items[2]], replies_to=frozenset(), now=NOW) is None


def test_clarification_notices() -> None:
    item = OpenClarification(1, 1, ClarificationKind.WHICH_PROJECT, TWO, None, NOW)
    assert domain.clarification_expired_text(item) == S.ROUTING_CLARIFY_EXPIRED(candidates="BonviHR / HRMentor")
    assert domain.out_of_range_text(TWO) == S.ROUTING_CHOICE_OUT_OF_RANGE(count=2, options="1) BonviHR, 2) HRMentor")


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "bu HRMentor uchun edi",
        "HRMentor'ga edi",
        "bu HRMentor'niki edi",
        "xato loyiha, HRMentor",
        "HRMentor'ga yuborish kerak edi",
        "bu HRMentor uchun",
        "buni HRMentor'ga yubor",
        "это было для HRMentor",
        "wrong project, HRMentor",
    ],
)
def test_a_correction_names_the_project_the_work_was_meant_for(text: str) -> None:
    assert domain.wrong_target_of(text, REGISTRY) == "HRMentor"


@pytest.mark.parametrize(
    "text",
    [
        "BonviHR'da buni oldin qilish kerak edi, endi tuzat",
        "HRMentor'ga yubor",
        "bu HRMentor yoki BonviHR uchun edi",
        "HRMentor'da login tuzat",
        "bu HRMentor wt uchun edi",
        "bu uchun edi",
    ],
)
def test_an_instruction_or_an_unclear_correction_is_not_one(text: str) -> None:
    assert domain.wrong_target_of(text, REGISTRY) is None


def test_a_correction_moves_the_latest_work_within_its_window() -> None:
    def route(number: int, minutes_ago: float, project: str, intent: Intent = Intent.INSTRUCT) -> PastRoute:
        at = NOW - timedelta(minutes=minutes_ago)
        return PastRoute(number, number * 10, at, Method.LLM, intent, project, None, asked=False)

    past = [
        route(1, 30, "BonviHR"),
        route(2, 5, "TezTel"),
        route(3, 1, "HRMentor", Intent.WRONG_TARGET),
        route(4, 2, "BonviGo", Intent.ANSWER),
        route(5, 200, "OvozYoz"),
    ]
    assert domain.route_to_correct(past, now=NOW, reply_project=None, burst_id=99) == past[1]
    assert domain.route_to_correct(past, now=NOW, reply_project="BonviHR", burst_id=99) == past[0]
    assert domain.route_to_correct(past, now=NOW, reply_project="OvozYoz", burst_id=99) is None  # too old
    assert domain.route_to_correct(past[1:2], now=NOW, reply_project=None, burst_id=20) is None  # itself


# --- rows and settings ----------------------------------------------------------------------------------------------
def test_rows_round_trip_and_unknown_values_fall_back() -> None:
    assert domain.candidates_json([Candidate("BonviHR", 0.812)]) == [{"project_id": "BonviHR", "confidence_milli": 812}]
    assert domain.ids_from_json(domain.ids_json(TWO)) == TWO
    assert domain.ids_from_json({"x": 1}) == ()
    assert (domain.confidence_milli(1.4), domain.confidence_milli(-1)) == (1000, 0)
    assert domain.member(Intent, "dance", Intent.INSTRUCT) is Intent.INSTRUCT
    assert domain.route_cutoff(NOW) == NOW - timedelta(days=90)
    assert domain.clarification_cutoff(NOW) == NOW - timedelta(days=30)


def test_route_settings_come_from_the_snapshot_defaults() -> None:
    assert (CFG.min_confidence, CFG.min_margin, CFG.context_window) == (0.75, 0.2, timedelta(minutes=15))
    assert (CFG.merge_min_confidence, CFG.model, CFG.timeout_s) == (0.85, "haiku", 2.5)


def test_an_unknown_part_kind_reads_as_a_message() -> None:
    sticker: BurstPart = make_part(1, kind=UpdateKind.OTHER)
    assert domain.material(make_turn_input(sticker), redact.scrubbed) == S.ROUTING_MATERIAL_MESSAGE()
