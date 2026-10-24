"""routing.service on a real SQLite file with `FakeAssistant`: each route recorded with its burst, the LLM ask and its
failure, the one clarification and the owner's answer, expiry, the UC-02 merge, "bu X uchun edi", idempotency and
retention (SPEC §3.2.6, §4.2; UC-10, UC-02)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.factories import (
    Fakes,
    add_registered_projects,
    add_setting_row,
    make_closed_burst,
    make_part,
    make_running_turn,
    make_voice_part,
    routing_reply,
)
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, ProviderUnavailable
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.routing import domain, service, store
from yordamchi.dev.routing.domain import (
    ChoiceKind,
    ClarificationStatus,
    CorrectionKind,
    Intent,
    Method,
    Outcome,
    RouteFacts,
)
from yordamchi.dev.routing.models import Clarification, RouteDecision
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.burst.domain import BurstPart, BurstStatus, CloseReason, TurnInput
from yordamchi.shared.cost.models import UsageRecord
from yordamchi.shared.settings import defaults

PROJECTS = ("BonviHR", "HRMentor", "HRMentor-wt-agent-api", "TezTel", "WunderkindLC", "WunderkindLMS")


@pytest.fixture(autouse=True)
async def _registry(dev_deps: DevDeps) -> None:
    async with dev_deps.db.uow() as s:
        await add_registered_projects(s, *PROJECTS)


async def _burst(deps: DevDeps, fakes: Fakes, *texts: str, reply_to: int | None = None) -> TurnInput:
    """A closed burst of these texts, received now."""
    start = fakes.clock.now()
    parts: list[BurstPart] = [
        make_part(9000 + index, at_s=float(index), text=text, start=start, reply_to_message_id=reply_to)
        for index, text in enumerate(texts)
    ]
    async with deps.db.uow() as s:
        return await make_closed_burst(s, *parts)


async def _decisions(deps: DevDeps) -> list[RouteDecision]:
    async with deps.db.read() as s:
        return list((await s.scalars(select(RouteDecision).order_by(RouteDecision.id))).all())


async def _clarifications(deps: DevDeps) -> list[Clarification]:
    async with deps.db.read() as s:
        return list((await s.scalars(select(Clarification).order_by(Clarification.id))).all())


async def _burst_status(deps: DevDeps, burst_id: int) -> tuple[str, int | None]:
    async with deps.db.read() as s:
        row = next(row for row in await burst_store.list_bursts(s, deps.bot.value) if row.id == burst_id)
    return row.status, row.route_id


async def _ask_two(deps: DevDeps, fakes: Fakes) -> service.Routed:
    """A content-only message the LLM cannot place: the one question names BonviHR and HRMentor."""
    fakes.assistant.reply_with("routing", routing_reply("BonviHR", 0.55, candidates={"HRMentor": 0.5}))
    routed = await service.route_burst(deps, await _burst(deps, fakes, "login sahifasini tuzat"))
    assert routed is not None
    assert routed.decision.candidate_ids == ("BonviHR", "HRMentor")
    return routed


# --- routing --------------------------------------------------------------------------------------------------------
async def test_a_named_project_is_routed_recorded_and_named_in_the_first_reply(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "BonviHR'da login sahifasini tuzat")
    routed = await service.route_burst(dev_deps, item)

    assert routed is not None
    assert (routed.decision.outcome, routed.decision.project_id) == (Outcome.ROUTE, "BonviHR")
    assert routed.reply == S.ROUTED(project="BonviHR")
    [row] = await _decisions(dev_deps)
    assert (row.burst_id, row.method, row.intent, row.project_id) == (
        item.burst_id,
        "explicit",
        "instruct_project",
        "BonviHR",
    )
    assert (row.asked, row.confidence_milli, row.corrected_to) == (False, 1000, None)
    assert row.candidates_json == [{"project_id": "BonviHR", "confidence_milli": 1000}]
    assert await _burst_status(dev_deps, item.burst_id) == (BurstStatus.ROUTED.value, routed.route_id)


async def test_the_routing_ask_uses_the_fast_tier_the_schema_and_the_fixed_prompt(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    fakes.assistant.reply_with("routing", routing_reply("HRMentor", 0.92))
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "mentor tavsiyalari chiqmayapti"))

    assert routed is not None
    assert (routed.decision.method, routed.decision.project_id) == (Method.LLM, "HRMentor")
    [request] = fakes.assistant.requests
    assert (request.kind, request.model, request.effort) == ("routing", "haiku", domain.ROUTING_EFFORT)
    assert request.output_schema == domain.OUTPUT_SCHEMA
    assert request.instructions == ""  # routing has no owner-editable block (no persona)
    assert "{{" not in request.system_prompt
    assert "HRMentor-wt-agent-api" in request.system_prompt
    assert "mentor tavsiyalari chiqmayapti" in request.prompt
    [row] = await _decisions(dev_deps)
    assert (row.method, row.model, row.confidence_milli) == ("llm", "haiku", 920)
    async with dev_deps.db.read() as s:
        [usage] = (await s.scalars(select(UsageRecord))).all()
    assert (usage.source, usage.provider, usage.is_paid_api, usage.project_id) == (
        "routing",
        "anthropic_plan",
        False,
        "HRMentor",
    )


async def test_the_warm_client_gets_exactly_the_profile_every_ask_uses(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.warm_routing(dev_deps)
    fakes.assistant.reply_with("routing", routing_reply("HRMentor", 0.92))
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "mentor tavsiyalari"))
    assert fakes.assistant.warmed == [fakes.assistant.requests[0].profile]


async def test_a_model_change_in_settings_applies_to_the_next_ask(dev_deps: DevDeps, fakes: Fakes) -> None:
    await add_setting_row(dev_deps.db, defaults.MODELS_ROUTING.key, "sonnet", at=fakes.clock.now())
    fakes.assistant.reply_with("routing", routing_reply("HRMentor", 0.92))
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "mentor tavsiyalari"))
    assert fakes.assistant.requests[0].model == "sonnet"


async def test_an_alias_from_settings_routes_at_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    await add_setting_row(dev_deps.db, defaults.PROJECTS_ALIASES.key, {"hr": "BonviHR"}, at=fakes.clock.now())
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "hr'da xato bor"))
    assert routed is not None
    assert (routed.decision.method, routed.decision.project_id) == (Method.EXPLICIT, "BonviHR")


async def test_an_unsure_route_asks_one_question_and_records_it(dev_deps: DevDeps, fakes: Fakes) -> None:
    routed = await _ask_two(dev_deps, fakes)

    assert routed.reply == "«login sahifasini tuzat» — BonviHR'gami yoki HRMentor'gami?"
    [row] = await _decisions(dev_deps)
    assert (row.asked, row.project_id, row.method) == (True, None, "llm")
    [asked] = await _clarifications(dev_deps)
    assert (asked.id, asked.burst_id, asked.kind, asked.status) == (
        routed.clarification_id,
        row.burst_id,
        "which_project",
        "open",
    )
    assert asked.candidates_json == ["BonviHR", "HRMentor"]
    assert await service.set_clarification_message(dev_deps, asked.id, 777)


@pytest.mark.parametrize(
    "failure", [CallTimedOut(operation="assistant_routing"), ProviderUnavailable(provider="claude")]
)
async def test_without_the_llm_a_follow_up_goes_to_the_one_project_just_spoken_about(
    dev_deps: DevDeps, fakes: Fakes, failure: Exception
) -> None:
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "HRMentor'da test yoz"))
    await fakes.clock.advance(180)
    fakes.assistant.reply_with("routing", failure)
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "testlarini ham yoz"))

    assert routed is not None
    assert (routed.decision.method, routed.decision.project_id) == (Method.CONTEXT, "HRMentor")
    assert routed.reply == S.ROUTED(project="HRMentor")
    assert (await _decisions(dev_deps))[-1].model is None


async def test_without_the_llm_and_without_context_the_bot_asks(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("routing", CallTimedOut(operation="assistant_routing"))
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "testlarini ham yoz"))
    assert routed is not None
    assert routed.decision.outcome is Outcome.ASK
    assert routed.reply == S.ROUTING_WHICH_PROJECT_ANY(material="«testlarini ham yoz»")


async def test_context_older_than_the_window_is_not_used(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "HRMentor'da test yoz"))
    await fakes.clock.advance(16 * 60)  # routing.context_window_min = 15
    fakes.assistant.reply_with("routing", CallTimedOut(operation="assistant_routing"))
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "testlarini ham yoz"))
    assert routed is not None
    assert routed.decision.outcome is Outcome.ASK


async def test_a_look_alike_of_a_worktree_asks_and_names_both(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("routing", routing_reply("HRMentor", 0.99))
    routed = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "HRMentor wt'da testlarni yoz"))
    assert routed is not None
    assert routed.decision.candidate_ids == ("HRMentor", "HRMentor-wt-agent-api")
    assert routed.reply == "«HRMentor wt'da testlarni yoz» — HRMentor'gami yoki HRMentor-wt-agent-api'gami?"


async def test_a_burst_already_routed_is_not_routed_again(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "BonviHR'da login sahifasini tuzat")
    first = await service.route_burst(dev_deps, item)
    assert first is not None
    assert await service.route_burst(dev_deps, item) is None
    assert [row.id for row in await _decisions(dev_deps)] == [first.route_id]


async def test_an_interrupt_is_refused_and_nothing_is_recorded(dev_deps: DevDeps, fakes: Fakes) -> None:
    async with dev_deps.db.uow() as s:
        stop = await make_closed_burst(s, make_part(1, text="to'xta"), reason=CloseReason.INTERRUPT)
    with pytest.raises(ValueError, match="pre-parse"):
        await service.route_burst(dev_deps, stop)
    assert await _decisions(dev_deps) == []
    assert fakes.assistant.requests == []


async def test_decide_and_record_split_lets_the_caller_join_one_transaction(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "TezTel'da sahifa qo'sh")
    planned = await service.decide_route(dev_deps, item)
    assert await _decisions(dev_deps) == []  # deciding writes nothing
    async with dev_deps.db.uow() as s:
        routed = await service.record_route(s, bot=dev_deps.bot, planned=planned, at=fakes.clock.now())
    assert routed is not None
    assert (await _burst_status(dev_deps, item.burst_id))[0] == BurstStatus.ROUTED.value


async def test_a_follow_up_of_the_running_turn_is_merged_not_asked(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "yana registratsiyani ham")
    running = make_running_turn("BonviHR", last_part_at=item.parts[0].at - timedelta(seconds=1))
    routed = await service.route_burst(dev_deps, item, RouteFacts(running=(running,)))

    assert routed is not None
    assert (routed.decision.outcome, routed.decision.project_id) == (Outcome.MERGE, "BonviHR")
    assert routed.reply == S.ROUTED_MERGED(project="BonviHR")
    assert await _clarifications(dev_deps) == []
    assert (await _burst_status(dev_deps, item.burst_id))[0] == BurstStatus.MERGED.value


async def test_an_unrelated_follow_up_is_routed_on_its_own(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "TezTel'da sahifa qo'sh")
    running = make_running_turn("BonviHR", last_part_at=item.parts[0].at - timedelta(seconds=1))
    routed = await service.route_burst(dev_deps, item, RouteFacts(running=(running,)))
    assert routed is not None
    assert (routed.decision.outcome, routed.decision.project_id) == (Outcome.ROUTE, "TezTel")


async def test_a_voice_note_is_echoed_with_its_project(dev_deps: DevDeps, fakes: Fakes) -> None:
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_voice_part(1, at_s=0, transcript="TezTel'da sahifa qo'sh"))
    routed = await service.route_burst(dev_deps, item)
    assert routed is not None
    assert routed.reply == S.VOICE_ECHO(text="TezTel'da sahifa qo'sh", project="TezTel")


# --- clarifications -------------------------------------------------------------------------------------------------
async def test_the_answer_2_resolves_the_question_and_names_the_project(dev_deps: DevDeps, fakes: Fakes) -> None:
    asked = await _ask_two(dev_deps, fakes)
    answer = await _burst(dev_deps, fakes, "2")
    resolution = await service.answer_clarification(dev_deps, answer)

    assert resolution is not None
    assert (resolution.choice, resolution.project_id, resolution.consumed) == (ChoiceKind.PICKED, "HRMentor", True)
    assert resolution.reply == S.ROUTED(project="HRMentor")
    first, second = await _decisions(dev_deps)
    assert (resolution.burst_id, resolution.route_id, resolution.intent) == (
        first.burst_id,
        asked.route_id,
        Intent.INSTRUCT,
    )
    assert (first.project_id, first.asked) == ("HRMentor", True)
    assert (second.burst_id, second.method, second.intent, second.project_id) == (
        answer.burst_id,
        "binding",
        "answer_clarification",
        "HRMentor",
    )
    [row] = await _clarifications(dev_deps)
    assert (row.status, row.resolved_at) == (ClarificationStatus.RESOLVED.value, fakes.clock.now())
    assert (await _burst_status(dev_deps, answer.burst_id))[0] == BurstStatus.ROUTED.value


@pytest.mark.parametrize(("text", "project"), [("ikkinchisi", "HRMentor"), ("BonviHR'ga", "BonviHR")])
async def test_an_ordinal_or_a_name_answers_too(dev_deps: DevDeps, fakes: Fakes, text: str, project: str) -> None:
    await _ask_two(dev_deps, fakes)
    resolution = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, text))
    assert resolution is not None
    assert resolution.project_id == project


async def test_a_voice_answer_resolves_the_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _ask_two(dev_deps, fakes)
    async with dev_deps.db.uow() as s:
        voice = await make_closed_burst(s, make_voice_part(2, at_s=5, transcript="birinchisi"))
    resolution = await service.answer_clarification(dev_deps, voice)
    assert resolution is not None
    assert resolution.project_id == "BonviHR"


async def test_an_answer_that_says_more_resolves_and_leaves_its_burst_for_routing(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await _ask_two(dev_deps, fakes)
    answer = await _burst(dev_deps, fakes, "BonviHR'da registratsiyani ham tuzat")
    resolution = await service.answer_clarification(dev_deps, answer)
    assert resolution is not None
    assert (resolution.project_id, resolution.consumed) == ("BonviHR", False)
    assert (await _burst_status(dev_deps, answer.burst_id))[0] == BurstStatus.CLOSED.value
    routed = await service.route_burst(dev_deps, answer)
    assert routed is not None
    assert routed.decision.project_id == "BonviHR"


async def test_a_message_that_answers_nothing_changes_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _ask_two(dev_deps, fakes)
    assert await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "TezTel'da yangi sahifa")) is None
    assert [row.status for row in await _clarifications(dev_deps)] == ["open"]
    assert len(await _decisions(dev_deps)) == 1


async def test_a_number_past_the_list_keeps_the_question_open(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _ask_two(dev_deps, fakes)
    resolution = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "3"))
    assert resolution is not None
    assert resolution.choice is ChoiceKind.OUT_OF_RANGE
    assert resolution.reply == domain.out_of_range_text(("BonviHR", "HRMentor"))
    assert [row.status for row in await _clarifications(dev_deps)] == ["open"]
    later = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "1"))
    assert later is not None
    assert later.project_id == "BonviHR"


async def test_none_of_them_cancels_the_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _ask_two(dev_deps, fakes)
    resolution = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "hech biri"))
    assert resolution is not None
    assert (resolution.choice, resolution.reply) == (ChoiceKind.CANCELLED, S.ROUTING_CLARIFY_CANCELLED())
    assert [row.status for row in await _clarifications(dev_deps)] == ["resolved"]
    assert (await _decisions(dev_deps))[0].project_id is None


async def test_a_bare_yes_answers_a_one_candidate_question_only_when_nothing_else_waits(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    fakes.assistant.reply_with("routing", routing_reply("BonviHR", 0.6))
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "login sahifasini tuzat"))
    pending = RouteFacts(others_pending=True)
    assert await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "ha"), pending) is None
    resolution = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "ha"))
    assert resolution is not None
    assert resolution.project_id == "BonviHR"


async def test_a_reply_binds_the_answer_to_that_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    first = await _ask_two(dev_deps, fakes)
    assert first.clarification_id is not None
    await service.set_clarification_message(dev_deps, first.clarification_id, 501)
    fakes.assistant.reply_with("routing", routing_reply("TezTel", 0.5, candidates={"BonviHR": 0.45}))
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "sahifani yangila"))

    resolution = await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "2", reply_to=501))
    assert resolution is not None
    assert (resolution.clarification_id, resolution.project_id) == (first.clarification_id, "HRMentor")


async def test_an_expired_question_answers_nothing_and_says_its_message_went_nowhere(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await _ask_two(dev_deps, fakes)
    await fakes.clock.advance(30 * 60)
    assert await service.answer_clarification(dev_deps, await _burst(dev_deps, fakes, "2")) is None
    [expired] = await service.expire_clarifications(dev_deps)
    assert expired.reply == S.ROUTING_CLARIFY_EXPIRED(candidates="BonviHR / HRMentor")
    assert [row.status for row in await _clarifications(dev_deps)] == ["expired"]
    assert await service.expire_clarifications(dev_deps) == ()


async def test_a_fresh_question_does_not_expire(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _ask_two(dev_deps, fakes)
    await fakes.clock.advance(29 * 60)
    assert await service.expire_clarifications(dev_deps) == ()


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
async def test_bu_x_uchun_edi_moves_the_latest_work_to_x(dev_deps: DevDeps, fakes: Fakes) -> None:
    wrong = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "BonviHR'da login sahifasini tuzat"))
    assert wrong is not None
    await fakes.clock.advance(120)
    fix = await _burst(dev_deps, fakes, "bu HRMentor uchun edi")
    correction = await service.correct_route(dev_deps, fix)

    assert correction is not None
    assert (correction.kind, correction.from_project, correction.to_project) == (
        CorrectionKind.MOVED,
        "BonviHR",
        "HRMentor",
    )
    assert (correction.route_id, correction.burst_id) == (wrong.route_id, (await _decisions(dev_deps))[0].burst_id)
    assert correction.reply == S.ROUTING_WRONG_TARGET_MOVED(to_project="HRMentor", from_project="BonviHR")
    first, second = await _decisions(dev_deps)
    assert (first.project_id, first.corrected_to) == ("BonviHR", "HRMentor")
    assert (second.burst_id, second.intent, second.project_id) == (fix.burst_id, "wrong_target", "HRMentor")
    assert (await _burst_status(dev_deps, fix.burst_id))[0] == BurstStatus.ROUTED.value

    fakes.assistant.reply_with("routing", CallTimedOut(operation="assistant_routing"))
    follow = await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "testlarini ham yoz"))
    assert follow is not None
    assert follow.decision.project_id == "HRMentor"  # the context now follows the correction


async def test_a_correction_to_where_the_work_already_went_or_with_nothing_to_move(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    nothing = await service.correct_route(dev_deps, await _burst(dev_deps, fakes, "bu HRMentor uchun edi"))
    assert nothing is not None
    assert (nothing.kind, nothing.reply) == (
        CorrectionKind.NOTHING,
        S.ROUTING_WRONG_TARGET_NONE(project="HRMentor", hours=2),
    )
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "HRMentor'da test yoz"))
    same = await service.correct_route(dev_deps, await _burst(dev_deps, fakes, "bu HRMentor uchun edi"))
    assert same is not None
    assert (same.kind, same.reply) == (CorrectionKind.SAME, S.ROUTING_WRONG_TARGET_SAME(project="HRMentor"))


async def test_an_instruction_is_not_a_correction(dev_deps: DevDeps, fakes: Fakes) -> None:
    item = await _burst(dev_deps, fakes, "HRMentor'da login tuzat")
    assert await service.correct_route(dev_deps, item) is None
    assert await _decisions(dev_deps) == []


async def test_a_correction_that_replies_moves_that_projects_work(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "BonviHR'da login tuzat"))
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "TezTel'da sahifa qo'sh"))
    facts = RouteFacts(reply_projects={42: "BonviHR"})
    correction = await service.correct_route(
        dev_deps, await _burst(dev_deps, fakes, "bu HRMentor uchun edi", reply_to=42), facts
    )
    assert correction is not None
    assert correction.from_project == "BonviHR"


# --- retention ------------------------------------------------------------------------------------------------------
async def test_retention_drops_old_rows_and_never_an_open_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.route_burst(dev_deps, await _burst(dev_deps, fakes, "BonviHR'da login tuzat"))
    await _ask_two(dev_deps, fakes)
    async with dev_deps.db.uow() as s:
        await store.add_clarification(
            s, burst_id=None, kind="which_project", candidates=[], status="resolved", asked_at=fakes.clock.now()
        )
    await fakes.clock.advance(31 * 24 * 3600)
    assert await service.apply_retention(dev_deps) == 1  # the resolved question; the open one stays
    assert [row.status for row in await _clarifications(dev_deps)] == ["open"]
    await fakes.clock.advance(60 * 24 * 3600)
    assert await service.apply_retention(dev_deps) == 2  # both route decisions are past 90 days
    assert await _decisions(dev_deps) == []
    assert [row.status for row in await _clarifications(dev_deps)] == ["open"]
