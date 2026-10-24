"""gate.service as the dev bot's settings asker (UC-09 F; SPEC §3.2.15 step 3): a settings change that weakens a
safeguard is ONE `settings_weaken` decision, bound like any other; "ha" applies it once, "yo'q" keeps the value, an
expired one is asked again, and a bare "ha" with a gate decision also open approves nothing."""

from datetime import timedelta

from tests.factories import Fakes, gate_asker, make_decision, make_owner_origin
from yordamchi.core import strings as S
from yordamchi.core.errors import ProviderUnavailable
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service, store
from yordamchi.dev.gate.domain import PendingItem
from yordamchi.dev.gate.models import Decision
from yordamchi.dev.gate.service import SETTINGS_KIND, AnswerKind, DecisionReply
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.domain import AuditKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.service import OutcomeKind

SCAN_OFF = "gate.secret_scan_enabled: yoqilgan → o'chirilgan"


async def turn_scan_off(deps: DevDeps, *, update_id: int = 900) -> settings.SettingsOutcome:
    origin = make_owner_origin(update_id=update_id)
    return await settings.change_setting(deps, origin, "gate.secret_scan_enabled", False, ask=gate_asker(deps))


async def answer(deps: DevDeps, text: str, *, update_id: int = 950) -> service.DecisionAnswer:
    return await service.answer_decision(deps, make_owner_origin(update_id=update_id), DecisionReply(text))


async def decisions(deps: DevDeps) -> list[Decision]:
    async with deps.db.read() as s:
        return sorted(await store.list_recent_decisions(s, limit=50), key=lambda row: row.id)


async def scan_enabled(deps: DevDeps) -> bool:
    return (await settings.snapshot(deps))[defaults.GATE_SECRET_SCAN_ENABLED]


async def test_a_weakening_change_is_one_decision_with_one_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    outcome = await turn_scan_off(dev_deps)
    [row] = await decisions(dev_deps)
    assert (outcome.kind, outcome.decision_id) == (OutcomeKind.ASKED, row.id)
    assert (row.kind, row.status, row.project_id, row.target) == (
        SETTINGS_KIND,
        "pending",
        None,
        "gate.secret_scan_enabled",
    )
    assert row.input_redacted == SCAN_OFF
    [question] = fakes.messenger.sent()
    assert question.payload["text"] == S.SETTING_WEAKEN_QUESTION(decision_id=row.id, change=SCAN_OFF)
    assert isinstance(row.payload_json, dict)
    assert row.payload_json["message_id"] == 101  # the owner may reply to it
    async with dev_deps.db.read() as s:
        [asked] = await audit_store.list_audit_events(s, kind=AuditKind.DECISION_ASKED.value)
    assert isinstance(asked.data_json, dict)
    assert (asked.actor, asked.data_json["keys"]) == ("owner", ["gate.secret_scan_enabled"])
    assert await service.pending_items(dev_deps) == (PendingItem("decision", row.id, None),)


async def test_asking_the_same_change_again_reuses_its_question(dev_deps: DevDeps, fakes: Fakes) -> None:
    first = await turn_scan_off(dev_deps, update_id=900)
    again = await turn_scan_off(dev_deps, update_id=901)
    assert first.decision_id == again.decision_id
    assert len(await decisions(dev_deps)) == 1
    assert len(fakes.messenger.sent()) == 1


async def test_ha_applies_the_change_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    await turn_scan_off(dev_deps)
    assert await scan_enabled(dev_deps)
    approved = await answer(dev_deps, "ha", update_id=950)
    assert (approved.kind, approved.delivery, approved.turn_id) == (AnswerKind.APPROVED, "", None)
    assert approved.reply == S.SETTING_CHANGED(change=SCAN_OFF)
    assert fakes.messenger.sent()[-1].payload["text"] == approved.reply
    assert not await scan_enabled(dev_deps)
    [row] = await decisions(dev_deps)
    assert (row.status, row.decided_update_id) == ("consumed", 950)

    again = await answer(dev_deps, f"#{row.id} ha", update_id=951)
    assert again.kind is AnswerKind.CLOSED
    assert again.reply == S.DECISION_CLOSED(project=S.SETTINGS_SUBJECT(), decision_id=row.id)
    async with dev_deps.db.read() as s:
        changes = await audit_store.list_audit_events(s, kind=AuditKind.SETTING_CHANGED.value)
    assert len(changes) == 1


async def test_no_keeps_the_value(dev_deps: DevDeps) -> None:
    await turn_scan_off(dev_deps)
    declined = await answer(dev_deps, "yo'q")
    [row] = await decisions(dev_deps)
    assert (declined.kind, row.status) == (AnswerKind.DECLINED, "denied")
    assert declined.reply == S.SETTING_CHANGE_DECLINED(decision_id=row.id, change=SCAN_OFF)
    assert await scan_enabled(dev_deps)


async def test_another_instruction_declines_without_an_agent_to_tell(dev_deps: DevDeps) -> None:
    await turn_scan_off(dev_deps)
    declined = await answer(dev_deps, "yo'q, faqat BonviHR uchun o'chir")
    assert (declined.kind, declined.delivery) == (AnswerKind.DECLINED, "")
    assert await scan_enabled(dev_deps)


async def test_ha_after_expiry_asks_again_and_applies_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    await turn_scan_off(dev_deps)
    await fakes.clock.advance(timedelta(minutes=121).total_seconds())  # gate.approval_ttl_min = 120
    reasked = await answer(dev_deps, "ha")
    old, fresh = await decisions(dev_deps)
    assert (reasked.kind, reasked.decision_id) == (AnswerKind.REASKED, fresh.id)
    assert (old.status, fresh.status, fresh.kind) == ("expired", "pending", SETTINGS_KIND)
    assert reasked.reply == S.DECISION_EXPIRED(project=S.SETTINGS_SUBJECT(), decision_id=old.id)
    assert fakes.messenger.sent()[-1].payload["text"] == S.SETTING_WEAKEN_QUESTION(
        decision_id=fresh.id, change=SCAN_OFF
    )
    assert await scan_enabled(dev_deps)
    assert (await answer(dev_deps, "ha", update_id=951)).kind is AnswerKind.APPROVED
    assert not await scan_enabled(dev_deps)


async def test_a_bare_ha_with_a_gate_decision_also_open_approves_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    async with dev_deps.db.uow() as s:
        gate_row = await make_decision(s, at=fakes.clock.now())
    await turn_scan_off(dev_deps)
    ambiguous = await answer(dev_deps, "ha")
    settings_row = (await decisions(dev_deps))[-1]
    assert ambiguous.kind is AnswerKind.AMBIGUOUS
    items = ", ".join(
        [
            S.DECISION_ITEM(decision_id=gate_row.id, project="BonviHR"),
            S.DECISION_ITEM(decision_id=settings_row.id, project=S.SETTINGS_SUBJECT()),
        ]
    )
    assert ambiguous.reply == S.WHICH_DECISION(decisions=items)
    assert await scan_enabled(dev_deps)
    assert (await answer(dev_deps, f"#{settings_row.id} ha", update_id=951)).kind is AnswerKind.APPROVED
    assert not await scan_enabled(dev_deps)


async def test_a_decision_whose_change_cannot_be_read_applies_nothing(dev_deps: DevDeps) -> None:
    await turn_scan_off(dev_deps)
    [row] = await decisions(dev_deps)
    broken: JsonValue = {"change": {"reason": "change", "items": []}}
    async with dev_deps.db.uow() as s:
        await store.set_decision_payload(s, row.id, broken)
    closed = await answer(dev_deps, "ha")
    assert closed.kind is AnswerKind.CLOSED
    assert await scan_enabled(dev_deps)
    [row] = await decisions(dev_deps)
    assert row.status == "cancelled"  # a later bare "ha" no longer binds to it
    assert (await answer(dev_deps, "ha", update_id=951)).kind is AnswerKind.NOT_AN_APPROVAL


async def test_an_unsent_settings_question_is_sent_later(dev_deps: DevDeps, fakes: Fakes) -> None:

    fakes.messenger.fail_next("send_text", ProviderUnavailable(provider="telegram"))
    await turn_scan_off(dev_deps)
    assert fakes.messenger.sent() == []
    assert await service.post_unasked_decisions(dev_deps) == 1
    [row] = await decisions(dev_deps)
    [question] = fakes.messenger.sent()
    assert question.payload["text"] == S.SETTING_WEAKEN_QUESTION(decision_id=row.id, change=SCAN_OFF)
