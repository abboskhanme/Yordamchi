"""The gate's self-update question (UC-19, Q5; protected core): ONE `selfupdate_core` decision per update, bound and
answered like any other, nothing delivered to an agent, an approval consumed exactly once, a late "ha" re-asked."""

from datetime import timedelta

from tests.factories import Fakes, make_owner_origin
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service, store
from yordamchi.dev.gate.service import SELFUPDATE_KIND, AnswerKind, CoreAnswer, DecisionReply
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.domain import AuditKind

KEY = "selfupdate:u000001:" + "b" * 40


async def ask(deps: DevDeps, key: str = KEY) -> int:
    return await service.ask_selfupdate_core(
        deps,
        project_id="Yordamchi",
        key=key,
        summary="Gate endi `gh pr merge` ni ham so'raydi. token=sk-proj-abcdefghijklmnopqrstuvwx",
        files=["src/yordamchi/dev/gate/domain.py"],
    )


def questions(fakes: Fakes) -> list[str]:
    return [str(r.payload["text"]) for r in fakes.messenger.sent() if "himoya qatlamiga" in str(r.payload["text"])]


async def test_the_same_update_is_asked_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    first = await ask(dev_deps)
    second = await ask(dev_deps)
    assert first == second
    (text,) = questions(fakes)
    assert text.startswith(f"Yordamchi: bu yangilanish himoya qatlamiga tegadi (#{first})")
    assert "src/yordamchi/dev/gate/domain.py" in text
    assert "sk-proj-abcdefghijklmnopqrstuvwx" not in text  # scrubbed before it is stored
    async with dev_deps.db.read() as s:
        row = await store.get_decision(s, first)
        events = await audit_store.list_audit_events(s, kind=AuditKind.DECISION_ASKED.value)
    assert row is not None
    assert (row.kind, row.turn_id, row.project_id) == (SELFUPDATE_KIND, None, "Yordamchi")
    assert len(events) == 1
    assert await ask(dev_deps, key="selfupdate:u000002:" + "c" * 40) != first  # another update asks again


async def test_ha_is_taken_exactly_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    decision = await ask(dev_deps)
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.PENDING, decision)
    answer = await service.answer_decision(dev_deps, make_owner_origin(update_id=5), DecisionReply("ha"))
    assert (answer.kind, answer.delivery) == (AnswerKind.APPROVED, "")
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.APPROVED, decision)
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.GONE, decision)


async def test_yoq_declines_without_a_delivery(dev_deps: DevDeps, fakes: Fakes) -> None:
    decision = await ask(dev_deps)
    answer = await service.answer_decision(dev_deps, make_owner_origin(update_id=6), DecisionReply("yo'q"))
    assert (answer.kind, answer.delivery) == (AnswerKind.DECLINED, "")
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.DECLINED, decision)


async def test_a_late_ha_asks_again_and_the_answer_follows_the_new_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    decision = await ask(dev_deps)
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=121))
    assert (await service.take_selfupdate_core(dev_deps, decision))[0] is CoreAnswer.PENDING
    reasked = await service.answer_decision(dev_deps, make_owner_origin(update_id=7), DecisionReply("ha"))
    assert reasked.kind is AnswerKind.REASKED
    newest = reasked.decision_id
    assert newest is not None
    assert newest != decision
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.PENDING, newest)
    await service.answer_decision(dev_deps, make_owner_origin(update_id=8), DecisionReply("ha"))
    assert await service.take_selfupdate_core(dev_deps, decision) == (CoreAnswer.APPROVED, newest)
