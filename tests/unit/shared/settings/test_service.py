"""settings.service on real SQLite files with fakes (SPEC §2.3, §3.2.15, §4.3; UC-09, NFR-OPS-03, NFR-SEC-09).

UC-09's acceptance rows, for one setting of every group the dev bot holds: a change applies from the next snapshot
and survives a restart, "oldingi holatiga qaytar" restores the value before it, an out-of-range value is refused with
its options and changes nothing, and each safeguard-weakening change asks exactly one question and applies only after
"ha" (through the gate's decisions, as the dev bot runs it). Plus: no write without an `OwnerOrigin`, a PERSONAL key
on the dev bot is refused, the list covers every group with the stored values in ≤ 3 s with secrets masked, and the
chat path on `FakeAssistant` replies.
"""

import ast
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from tests.factories import (
    FAKE_KEYS,
    Fakes,
    RecordingAsker,
    add_setting_row,
    gate_asker,
    list_replies,
    list_setting_rows,
    make_owner_origin,
)
from tests.fakes.clock import FakeClock
from tests.owner_text import shown
from yordamchi import prompts
from yordamchi.core import strings as S
from yordamchi.core.errors import ProviderUnavailable, SettingInvalid, SettingPersonalOnly, YordamchiError
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate_service
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.models import Decision
from yordamchi.dev.gate.service import AnswerKind, DecisionReply
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.domain import AuditKind
from yordamchi.shared.audit.models import AuditEvent
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.settings import defaults, domain, service, store
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import Source
from yordamchi.shared.settings.registry import AnySetting, Group
from yordamchi.shared.settings.service import OutcomeKind, SettingsOutcome


async def test_a_fresh_dev_db_resolves_to_defaults(dev_deps: DevDeps) -> None:
    snap = await service.snapshot(dev_deps)
    assert snap.version == 0
    assert snap[defaults.TURNS_MAX_CONCURRENT] == 15
    assert snap[defaults.ACCESS_DEV_GROUP_ID] == dev_deps.bootstrap[defaults.ACCESS_DEV_GROUP_ID.key]


async def test_a_recorded_version_applies_to_the_next_snapshot(dev_deps: DevDeps) -> None:
    origin = make_owner_origin(update_id=901)
    assert await service.record_version(dev_deps, origin, defaults.BURST_AFTER_MEDIA_S, 6, reason="change") == 1
    assert await service.record_version(dev_deps, origin, defaults.BURST_AFTER_MEDIA_S, 8, reason="change") == 2
    snap = await service.snapshot(dev_deps)
    assert snap[defaults.BURST_AFTER_MEDIA_S] == 8.0
    assert snap.version == 2
    async with dev_deps.db.read() as s:
        history = await store.list_setting_history(s, defaults.BURST_AFTER_MEDIA_S.key)
    assert [(row.version, row.value_json, row.origin_update_id, row.actor) for row in history] == [
        (2, 8.0, 901, "owner"),
        (1, 6.0, 901, "owner"),
    ]
    assert history[0].created_at == FakeClock().now()


async def test_an_invalid_value_stores_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(SettingInvalid):
        await service.record_version(dev_deps, make_owner_origin(), defaults.BURST_AFTER_MEDIA_S, 999, reason="change")
    assert (await service.snapshot(dev_deps)).version == 0


async def test_the_dev_bot_refuses_a_personal_key(dev_deps: DevDeps) -> None:
    with pytest.raises(SettingPersonalOnly):
        await service.record_version(dev_deps, make_owner_origin(), defaults.PERSONA_PERSONAL, "x", reason="change")


async def test_an_origin_from_the_other_bot_is_a_bug(dev_deps: DevDeps) -> None:
    with pytest.raises(ValueError, match="origin"):
        await service.record_version(
            dev_deps, make_owner_origin(BotName.PERSONAL), defaults.BURST_AFTER_MEDIA_S, 6, reason="change"
        )


async def test_the_personal_bot_reads_shared_dev_values_through_the_read_only_bridge(
    dev_deps: DevDeps, personal_deps: PersonalDeps
) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.CAPS_WARN_PCT, 90, reason="change")
    await service.record_version(dev_deps, make_owner_origin(), defaults.VOICE_ENABLED, True, reason="change")
    snap = await service.snapshot(personal_deps)
    assert (snap[defaults.CAPS_WARN_PCT], snap.source(defaults.CAPS_WARN_PCT)) == (90, Source.DEV)
    assert snap[defaults.VOICE_ENABLED] is True  # its own PerBot default, not the dev row
    assert snap.source(defaults.VOICE_ENABLED) is Source.DEFAULT

    personal_origin = make_owner_origin(BotName.PERSONAL)
    await service.record_version(personal_deps, personal_origin, defaults.CAPS_WARN_PCT, 70, reason="change")
    again = await service.snapshot(personal_deps)
    assert (again[defaults.CAPS_WARN_PCT], again.source(defaults.CAPS_WARN_PCT)) == (70, Source.OWN)
    assert again.version > snap.version


async def test_settings_survive_a_restart(dev_deps: DevDeps) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.SERVERS_HOSTS, ["bonvi-srv"], reason="change")
    await dev_deps.db.dispose()  # the next open is a new process's engine on the same file
    assert (await service.snapshot(dev_deps))[defaults.SERVERS_HOSTS] == ["bonvi-srv"]


async def test_a_row_that_fails_todays_check_is_skipped_and_logged(
    dev_deps: DevDeps, caplog: pytest.LogCaptureFixture
) -> None:
    await add_setting_row(dev_deps.db, defaults.BURST_AFTER_MEDIA_S.key, 500, at=dev_deps.clock.now())
    with caplog.at_level(logging.WARNING):
        snap = await service.snapshot(dev_deps)
    assert snap[defaults.BURST_AFTER_MEDIA_S] == 10.0
    assert any(record.getMessage() == "setting_row_skipped" for record in caplog.records)


async def test_a_recorded_version_is_audited_without_its_value(dev_deps: DevDeps) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.SERVERS_HOSTS, ["bonvi-srv"], reason="change")
    [event] = await audit_events(dev_deps)
    assert (event.actor, event.ref_type) == ("owner", "setting_version")
    assert event.data_json == {"key": "servers.hosts", "version": 1, "reason": "change", "decision_id": None}


# --- helpers --------------------------------------------------------------------------------------------------------
async def audit_events(deps: DevDeps | PersonalDeps) -> list[AuditEvent]:
    async with deps.db.read() as s:
        return list(await audit_store.list_audit_events(s, kind=AuditKind.SETTING_CHANGED.value))


async def audit_data(deps: DevDeps) -> list[JsonObject]:
    """The `data_json` of every `setting_changed` event, oldest first."""
    found: list[JsonObject] = []
    for event in await audit_events(deps):
        assert isinstance(event.data_json, dict)
        found.append(event.data_json)
    return found


async def settings_decisions(deps: DevDeps) -> list[Decision]:
    async with deps.db.read() as s:
        rows = await gate_store.list_recent_decisions(s, limit=50)
    return sorted((row for row in rows if row.kind == gate_service.SETTINGS_KIND), key=lambda row: row.id)


def text_of(row: OutboxMessage) -> JsonValue:
    assert isinstance(row.payload_json, dict)
    return row.payload_json.get("text")


class Owner:
    """The owner talking to the dev bot: each message is a new update, so each carries its own `OwnerOrigin`."""

    def __init__(self, deps: DevDeps) -> None:
        self.deps = deps
        self.update_id = 900
        self.ask = gate_asker(deps)

    def origin(self) -> OwnerOrigin:
        self.update_id += 1
        return make_owner_origin(update_id=self.update_id)

    async def change(self, setting: AnySetting, value: JsonValue) -> SettingsOutcome:
        return await service.change_setting(self.deps, self.origin(), setting.key, value, ask=self.ask)

    async def revert(self, key: str | None = None) -> SettingsOutcome:
        return await service.revert_setting(self.deps, self.origin(), key, ask=self.ask)

    async def answer(self, text: str) -> gate_service.DecisionAnswer:
        return await gate_service.answer_decision(self.deps, self.origin(), DecisionReply(text))

    async def confirmed(self, outcome: SettingsOutcome) -> SettingsOutcome:
        """A change that weakens a safeguard waits for the owner: say "ha"."""
        if outcome.kind is OutcomeKind.ASKED:
            assert (await self.answer("ha")).kind is AnswerKind.APPROVED
        return outcome


@dataclass(frozen=True, slots=True)
class Case:
    setting: AnySetting
    new: JsonValue
    bad: JsonValue


# One setting of every group the dev bot holds (UC-09 "≥ 1 change per settings group"). The ACCESS change weakens a
# safeguard, so it goes through one question and "ha"; the DECISIONS one lowers the TTL, so its revert asks.
GROUP_CASES: tuple[Case, ...] = (
    Case(defaults.ACCESS_DEV_GROUP_ID, -1009999999999, 5),
    Case(defaults.PERSONA_DEV_TONE, "qisqa va iliq", "x" * 301),
    Case(defaults.PROMPT_DEV_EXECUTOR_EXTRA, "Har doim testlarni ishga tushir.", "x" * 4001),
    Case(defaults.VOICE_MAX_WORDS, 50, 5),
    Case(defaults.SPEECH_STT_PROVIDER, "openrouter", "whisper"),
    Case(defaults.LANG_REPLY, "ru", "de"),
    Case(defaults.MODELS_ROUTING, "sonnet", "gpt-5"),
    Case(defaults.ROUTING_MIN_CONFIDENCE, 0.8, 1.5),
    Case(defaults.GATE_APPROVAL_TTL_MIN, 60, 2),
    Case(defaults.BURST_AFTER_MEDIA_S, 15, 999),
    Case(defaults.TIMEOUTS_ROUTING_S, 3.0, 100),
    Case(defaults.SCHEDULE_QUIET_HOURS, "23:00-07:00", "25:00-07:00"),
    Case(defaults.GUARD_MIN_FREE_DISK_GB, 20, 0),
    Case(defaults.OUTBOX_MAX_PARTS, 5, 11),
    Case(defaults.SERVERS_HOSTS, ["bonvi-srv"], ["user@host"]),
)
# Every safeguard kind SPEC §4.3 names: the ids, the gate list, the secret scan, the jump-only hosts, cost and
# concurrency caps, a credit-billed model.
WEAKENING_CASES: tuple[Case, ...] = (
    Case(defaults.ACCESS_OWNER_ID, 777000, 0),
    Case(defaults.GATE_ASK_CLASSES, [name for name in defaults.GATE_CLASSES if name != "data_loss_db"], ["nuke"]),
    Case(defaults.GATE_SECRET_SCAN_ENABLED, False, "no"),
    Case(defaults.GATE_JUMP_ONLY_HOSTS, [], ["user@host"]),
    Case(defaults.CAPS_GOOGLE_MONTHLY_USD, 100, -1),
    Case(defaults.TURNS_MAX_CONCURRENT, 20, 99),
    Case(defaults.MODELS_CODING, "fable", "gpt-5"),
)


def group_id(case: Case) -> str:
    return case.setting.group.value


def key_id(case: Case) -> str:
    return case.setting.key


def test_the_cases_cover_every_group_the_dev_bot_holds() -> None:
    dev_groups = {setting.group for setting in REGISTRY if domain.in_scope(setting, BotName.DEV)}
    assert [case.setting.group for case in GROUP_CASES] == [group for group in Group if group in dev_groups]
    assert all(case.setting.weakens is not None for case in WEAKENING_CASES)


# --- UC-09 per group ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", GROUP_CASES, ids=group_id)
async def test_a_change_applies_from_the_next_snapshot(dev_deps: DevDeps, case: Case) -> None:
    owner = Owner(dev_deps)
    before = await service.snapshot(dev_deps)
    outcome = await owner.confirmed(await owner.change(case.setting, case.new))
    after = await service.snapshot(dev_deps)
    assert after.values[case.setting.key] == domain.validate(case.setting, case.new)
    assert after.version > before.version
    assert outcome.keys == (case.setting.key,)
    rows = await list_setting_rows(dev_deps.db, case.setting.key)
    assert [(row.version, row.reason, row.actor) for row in rows] == [(1, "change", "owner")]
    assert [audited["key"] for audited in await audit_data(dev_deps)] == [case.setting.key]


@pytest.mark.parametrize("case", GROUP_CASES, ids=group_id)
async def test_a_change_survives_a_restart(dev_deps: DevDeps, case: Case) -> None:
    owner = Owner(dev_deps)
    await owner.confirmed(await owner.change(case.setting, case.new))
    await dev_deps.db.dispose()  # the next read is a new process's engine on the same file
    assert (await service.snapshot(dev_deps)).values[case.setting.key] == domain.validate(case.setting, case.new)


@pytest.mark.parametrize("case", GROUP_CASES, ids=group_id)
async def test_revert_restores_the_value_before_the_last_change(dev_deps: DevDeps, case: Case) -> None:
    owner = Owner(dev_deps)
    before = (await service.snapshot(dev_deps)).values[case.setting.key]
    await owner.confirmed(await owner.change(case.setting, case.new))
    reverted = await owner.confirmed(await owner.revert())
    assert reverted.keys == (case.setting.key,)
    assert (await service.snapshot(dev_deps)).values[case.setting.key] == before
    rows = await list_setting_rows(dev_deps.db, case.setting.key)
    assert [(row.version, row.reason) for row in rows] == [(2, "revert"), (1, "change")]


@pytest.mark.parametrize("case", GROUP_CASES, ids=group_id)
async def test_an_invalid_value_is_refused_with_its_options_and_changes_nothing(dev_deps: DevDeps, case: Case) -> None:
    owner = Owner(dev_deps)
    with pytest.raises(SettingInvalid) as refused:
        await owner.change(case.setting, case.bad)
    message = S.ERRORS[refused.value.code](**refused.value.params)
    assert case.setting.key in message
    assert "Hech narsa o'zgarmadi" in message
    if case.setting.check is not None:
        assert case.setting.check.options() in message
    assert (await service.snapshot(dev_deps)).version == 0
    assert await list_replies(dev_deps.db) == []
    assert await settings_decisions(dev_deps) == []


@pytest.mark.parametrize("case", WEAKENING_CASES, ids=key_id)
async def test_a_weakening_change_asks_once_and_applies_only_after_ha(
    dev_deps: DevDeps, fakes: Fakes, case: Case
) -> None:
    owner = Owner(dev_deps)
    key = case.setting.key
    before = (await service.snapshot(dev_deps)).values[key]
    first = await owner.change(case.setting, case.new)
    again = await owner.change(case.setting, case.new)  # said twice before answering: still one question
    assert first.kind is again.kind is OutcomeKind.ASKED
    assert first.decision_id == again.decision_id
    [decision] = await settings_decisions(dev_deps)
    restated = domain.change_line(case.setting, before, domain.validate(case.setting, case.new))
    [question] = fakes.messenger.sent()
    assert question.payload["text"] == S.SETTING_WEAKEN_QUESTION(decision_id=decision.id, change=restated)
    assert (await service.snapshot(dev_deps)).values[key] == before
    assert await list_setting_rows(dev_deps.db, key) == []

    answer = await owner.answer("ha")
    assert answer.kind is AnswerKind.APPROVED
    assert (await service.snapshot(dev_deps)).values[key] == domain.validate(case.setting, case.new)
    [row] = await list_setting_rows(dev_deps.db, key)
    assert (row.reason, row.origin_update_id) == ("change", owner.update_id)
    [decision] = await settings_decisions(dev_deps)
    assert decision.status == "consumed"
    assert [audited["decision_id"] for audited in await audit_data(dev_deps)] == [decision.id]
    assert fakes.messenger.sent()[-1].payload["text"] == S.SETTING_CHANGED(change=restated)


async def test_a_request_with_one_weakening_part_waits_as_a_whole(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    proposals = (
        domain.Proposal(defaults.ROUTING_MIN_CONFIDENCE.key, 0.8),
        domain.Proposal(defaults.CAPS_GOOGLE_MONTHLY_USD.key, 100),
    )
    outcome = await service.apply_proposals(dev_deps, owner.origin(), proposals, ask=owner.ask)
    assert outcome.kind is OutcomeKind.ASKED
    assert (await service.snapshot(dev_deps)).version == 0
    await owner.confirmed(outcome)
    snap = await service.snapshot(dev_deps)
    assert (snap[defaults.ROUTING_MIN_CONFIDENCE], snap[defaults.CAPS_GOOGLE_MONTHLY_USD]) == (0.8, 100)


async def test_one_invalid_value_refuses_the_whole_request(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    proposals = (
        domain.Proposal(defaults.ROUTING_MIN_CONFIDENCE.key, 0.8),
        domain.Proposal(defaults.BURST_AFTER_MEDIA_S.key, 999),
    )
    with pytest.raises(SettingInvalid):
        await service.apply_proposals(dev_deps, owner.origin(), proposals, ask=owner.ask)
    assert (await service.snapshot(dev_deps)).version == 0


async def test_a_value_already_set_changes_nothing_and_says_so(dev_deps: DevDeps) -> None:
    outcome = await Owner(dev_deps).change(defaults.BURST_AFTER_MEDIA_S, 10)
    assert (outcome.kind, outcome.reply) == (OutcomeKind.UNCHANGED, S.SETTINGS_UNCHANGED())
    assert [text_of(row) for row in await list_replies(dev_deps.db)] == [S.SETTINGS_UNCHANGED()]
    assert (await service.snapshot(dev_deps)).version == 0


async def test_an_applied_change_is_restated_in_one_line_to_the_owners_chat(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    outcome = await owner.change(defaults.BURST_AFTER_MEDIA_S, 15)
    assert outcome.reply == S.SETTING_CHANGED(change="burst.after_media_s: 10 s → 15 s")
    [reply] = await list_replies(dev_deps.db)
    assert (reply.op, reply.kind, reply.chat_id) == ("send_text", "report", -1001234567890)
    assert (reply.dedupe_key, text_of(reply)) == (f"report:settings-{owner.update_id}:0", outcome.reply)


async def test_a_named_revert_touches_that_key_only(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    await owner.change(defaults.BURST_AFTER_MEDIA_S, 15)
    await owner.change(defaults.MODELS_ROUTING, "sonnet")
    outcome = await owner.revert(defaults.BURST_AFTER_MEDIA_S.key)
    assert outcome.reply == S.SETTING_REVERTED(change="burst.after_media_s: 15 s → 10 s")
    snap = await service.snapshot(dev_deps)
    assert (snap[defaults.BURST_AFTER_MEDIA_S], snap[defaults.MODELS_ROUTING]) == (10.0, "sonnet")


async def test_nothing_to_revert_says_so(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    assert (await owner.revert()).reply == S.SETTINGS_NOTHING_TO_REVERT()
    named = await owner.revert(defaults.VOICE_GENDER.key)
    assert (named.kind, named.reply) == (OutcomeKind.NOTHING_TO_REVERT, S.SETTING_NOTHING_TO_REVERT(key="voice.gender"))
    assert (await service.snapshot(dev_deps)).version == 0


async def test_a_personal_key_is_refused_on_the_dev_bot(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    with pytest.raises(SettingPersonalOnly) as refused:
        await owner.change(defaults.PERSONA_PERSONAL, "Juda qisqa javob ber.")
    assert S.ERRORS[refused.value.code](**refused.value.params).endswith("buni shaxsiy botga ayting.")
    with pytest.raises(SettingPersonalOnly):
        await owner.revert(defaults.PERSONA_PERSONAL.key)
    assert (await service.snapshot(dev_deps)).version == 0
    assert await list_replies(dev_deps.db) == []


# --- only the owner changes settings (A5, UC-09 F, NFR-SEC-09) -----------------------------------------------------
type Writer = Callable[..., Awaitable[object]]
WRITERS: tuple[Writer, ...] = (
    service.record_version,
    service.change_setting,
    service.apply_proposals,
    service.apply_pending_change,
    service.revert_setting,
    service.handle_request,
    service.show_settings,
)


@pytest.mark.parametrize("writer", WRITERS, ids=lambda writer: writer.__name__)
def test_every_settings_writer_requires_an_owner_origin(writer: Writer) -> None:
    origin = inspect.signature(writer).parameters["origin"]
    assert origin.annotation is OwnerOrigin
    assert origin.default is inspect.Parameter.empty


def test_no_other_public_coroutine_writes() -> None:
    public = {
        name
        for name, member in inspect.getmembers(service, inspect.iscoroutinefunction)
        if not name.startswith("_") and member.__module__ == service.__name__
    }
    readers = {"snapshot", "list_settings", "interpret_request"}
    assert public - readers == {writer.__name__ for writer in WRITERS}


def test_features_write_directly_only_settings_that_never_ask() -> None:
    """`record_version` never asks the owner, so in `src/` it takes only a setting with no `weakens` (aliases,
    terms, descriptions); a safeguard change goes through `change_setting` and its one question."""
    src = Path(service.__file__).resolve().parents[2]
    written: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "record_version":
                continue
            where = f"{path.relative_to(src)}:{node.lineno}"
            setting = node.args[2] if len(node.args) > 2 else None
            assert isinstance(setting, ast.Attribute), f"{where}: pass the setting as defaults.<NAME>"
            assert isinstance(setting.value, ast.Name), f"{where}: pass the setting as defaults.<NAME>"
            assert setting.value.id == "defaults", f"{where}: pass the setting as defaults.<NAME>"
            declared = getattr(defaults, setting.attr)
            assert declared.weakens is None, f"{where}: {declared.key} is a safeguard; use change_setting"
            written.append(declared.key)
    assert "stt.extra_terms" in written  # the walk sees the callers


@dataclass(frozen=True)
class Lookalike:
    """Executor output, a tool result or a web page can at most build something shaped like an origin."""

    bot: BotName = BotName.DEV
    update_id: int = 1
    user_id: int = 424242
    chat_id: int = -1001234567890


@pytest.mark.parametrize("origin", [None, Lookalike()], ids=["none", "lookalike"])
async def test_without_a_verified_owner_origin_nothing_changes(dev_deps: DevDeps, fakes: Fakes, origin: object) -> None:
    fake = cast(OwnerOrigin, origin)
    ask = gate_asker(dev_deps)
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await service.change_setting(dev_deps, fake, defaults.BURST_AFTER_MEDIA_S.key, 6, ask=ask)
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await service.record_version(dev_deps, fake, defaults.BURST_AFTER_MEDIA_S, 6, reason="change")
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await service.handle_request(dev_deps, fake, "rasmdan keyin 6 soniya kut", ask=ask)
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await service.revert_setting(dev_deps, fake, ask=ask)
    assert fakes.assistant.requests == []  # refused before the model is even asked
    assert (await service.snapshot(dev_deps)).version == 0


async def test_an_origin_of_the_other_bot_changes_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(ValueError, match="origin"):
        await service.change_setting(
            dev_deps, make_owner_origin(BotName.PERSONAL), "burst.after_media_s", 6, ask=gate_asker(dev_deps)
        )
    assert (await service.snapshot(dev_deps)).version == 0


# --- "sozlamalaring qanday?" ----------------------------------------------------------------------------------------
async def test_the_list_covers_every_group_with_the_stored_values_within_3_s(dev_deps: DevDeps) -> None:
    owner = Owner(dev_deps)
    for case in GROUP_CASES:
        await owner.confirmed(await owner.change(case.setting, case.new))
    started = time.monotonic()
    listing = await service.list_settings(dev_deps)
    assert time.monotonic() - started < 3.0
    snap = await service.snapshot(dev_deps)
    assert listing.groups == tuple(case.setting.group for case in GROUP_CASES)
    assert (listing.total, listing.changed) == (len(snap.values), len(GROUP_CASES))
    for case in GROUP_CASES:
        value = snap.values[case.setting.key]
        assert S.SETTINGS_GROUPS[case.setting.group.value]() in listing.summary
        summary_value = domain.show_value(case.setting, value, limit=domain.SUMMARY_VALUE_CHARS)
        assert f"{case.setting.key} = {summary_value}" in shown(listing.summary)
        assert f"• {case.setting.key} = {domain.show_value(case.setting, value)} — o'zgartirilgan" in shown(
            listing.full
        )
    assert all(f"• {key} = " in shown(listing.full) for key in snap.values)


async def test_the_list_masks_secrets(dev_deps: DevDeps) -> None:
    secret = FAKE_KEYS["anthropic_api_key"]
    await service.record_version(
        dev_deps, make_owner_origin(), defaults.PROMPT_DEV_EXECUTOR_EXTRA, f"kalit: {secret}", reason="change"
    )
    listing = await service.list_settings(dev_deps)
    assert secret not in listing.summary
    assert secret not in listing.full
    assert "prompt.dev_executor_extra = kalit: ••••" in shown(listing.full)


async def test_show_settings_sends_the_summary_then_the_full_list_as_a_file(dev_deps: DevDeps) -> None:
    listing = await service.show_settings(dev_deps, make_owner_origin(update_id=950))
    summary, full = await list_replies(dev_deps.db)
    assert (summary.op, summary.kind, summary.dedupe_key) == ("send_text", "report", "report:settings-950:0")
    assert text_of(summary) == listing.summary
    assert (full.op, full.kind, full.dedupe_key) == ("send_document", "report", "report:settings-950-full:0")
    assert isinstance(full.payload_json, dict)
    assert full.payload_json["content"] == listing.full
    first_line = listing.full.splitlines()[0]
    assert full.payload_json["caption"] == S.OUTBOX_TEXT_IN_FILE(summary=first_line)
    assert str(full.payload_json["filename"]).endswith(".md")


# --- the chat path (SPEC §3.2.15 step 1; FakeAssistant replays the structured answers) ----------------------------
def answer_of(outcome: str, *changes: tuple[str, JsonValue], keys: tuple[str, ...] = ()) -> dict[str, JsonValue]:
    """A `settings` Assistant answer of `domain.INTERPRETATION_SCHEMA`."""
    return {
        "outcome": outcome,
        "changes": [{"key": key, "value": value} for key, value in changes],
        "keys": list(keys),
    }


async def say(
    deps: DevDeps, fakes: Fakes, text: str, answer: dict[str, JsonValue], *, update_id: int = 960
) -> SettingsOutcome:
    fakes.assistant.reply_with("settings", answer)
    return await service.handle_request(deps, make_owner_origin(update_id=update_id), text, ask=gate_asker(deps))


async def test_routing_modelini_sonnet_qil(dev_deps: DevDeps, fakes: Fakes) -> None:
    outcome = await say(
        dev_deps, fakes, "routing modelini sonnet qil", answer_of("change", ("models.routing", "sonnet"))
    )
    assert (outcome.kind, outcome.reply) == (
        OutcomeKind.APPLIED,
        S.SETTING_CHANGED(change="models.routing: haiku → sonnet"),
    )
    assert (await service.snapshot(dev_deps))[defaults.MODELS_ROUTING] == "sonnet"
    [reply] = await list_replies(dev_deps.db)
    assert text_of(reply) == outcome.reply


async def test_the_settings_assistant_gets_the_fixed_prompt_the_table_and_the_schema(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await say(dev_deps, fakes, "routing modelini sonnet qil", answer_of("change", ("models.routing", "sonnet")))
    [request] = fakes.assistant.requests
    assert (request.kind, request.model, request.prompt) == ("settings", "sonnet", "routing modelini sonnet qil")
    assert request.system_prompt == prompts.render("dev/settings_system")
    assert request.output_schema == domain.INTERPRETATION_SCHEMA
    assert request.conversation is None
    assert request.instructions.startswith("Current instructions from the owner:\nThis message reached the dev bot.")
    assert (
        '| models.routing | str | dev | "haiku" | "haiku" | haiku, sonnet, opus, fable, best |' in request.instructions
    )
    assert all(setting.doc in request.instructions for setting in REGISTRY)
    assert request.settings_version == request.prompt_version == 0


async def test_ovozingni_ayolga_ozgartir(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.VOICE_GENDER, "male", reason="change")
    outcome = await say(dev_deps, fakes, "ovozingni ayolga o'zgartir", answer_of("change", ("voice.gender", "female")))
    assert outcome.reply == S.SETTING_CHANGED(change="voice.gender: male → female")
    assert (await service.snapshot(dev_deps))[defaults.VOICE_GENDER] == "female"


async def test_a_number_the_model_wrote_as_text_still_applies(dev_deps: DevDeps, fakes: Fakes) -> None:
    answer = answer_of("change", ("burst.after_media_s", "15"))
    outcome = await say(dev_deps, fakes, "rasmdan keyin 15 soniya kut", answer)
    assert outcome.kind is OutcomeKind.APPLIED
    assert (await service.snapshot(dev_deps))[defaults.BURST_AFTER_MEDIA_S] == 15.0


async def test_an_ambiguous_request_gets_one_clarifying_question_and_changes_nothing(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    answer = answer_of("clarify", ("models.report", "sonnet"), ("timeouts.report_s", 2))
    outcome = await say(dev_deps, fakes, "yaxshiroq javob ber", answer)
    options = "1) models.report: haiku → sonnet; 2) timeouts.report_s: 3.5 s → 2 s"
    assert (outcome.kind, outcome.reply) == (OutcomeKind.CLARIFY, S.SETTINGS_CLARIFY(options=options))
    assert [text_of(row) for row in await list_replies(dev_deps.db)] == [outcome.reply]
    assert (await service.snapshot(dev_deps)).version == 0


async def test_a_weakening_request_by_chat_asks_once_and_ha_applies_it(dev_deps: DevDeps, fakes: Fakes) -> None:
    outcome = await say(
        dev_deps, fakes, "kod yozish modelini fable qil", answer_of("change", ("models.coding", "fable"))
    )
    assert outcome.kind is OutcomeKind.ASKED
    assert (await service.snapshot(dev_deps))[defaults.MODELS_CODING] == "opus"
    answer = await gate_service.answer_decision(dev_deps, make_owner_origin(update_id=961), DecisionReply("ha"))
    assert answer.reply == S.SETTING_CHANGED(change="models.coding: opus → fable")
    assert (await service.snapshot(dev_deps))[defaults.MODELS_CODING] == "fable"


async def test_a_guessed_key_changes_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    answer = answer_of("change", ("burst.after_media_s", 15), ("answers.max_words", 20))
    outcome = await say(dev_deps, fakes, "javoblaring qisqaroq bo'lsin", answer)
    assert (outcome.kind, outcome.reply) == (OutcomeKind.UNCLEAR, S.SETTINGS_UNCLEAR())
    assert (await service.snapshot(dev_deps)).version == 0


async def test_a_request_that_needs_code_offers_a_self_update(dev_deps: DevDeps, fakes: Fakes) -> None:
    outcome = await say(dev_deps, fakes, "har kuni ertalab ob-havo yubor", answer_of("needs_code"))
    assert (outcome.kind, outcome.reply) == (OutcomeKind.NEEDS_CODE, S.SETTINGS_NEEDS_CODE())


async def test_asking_what_the_settings_are_shows_them(dev_deps: DevDeps, fakes: Fakes) -> None:
    outcome = await say(dev_deps, fakes, "sozlamalaring qanday?", answer_of("show"))
    assert outcome.kind is OutcomeKind.SHOWN
    assert [row.op for row in await list_replies(dev_deps.db)] == ["send_text", "send_document"]


async def test_undo_by_chat_reverts_the_last_change(dev_deps: DevDeps, fakes: Fakes) -> None:
    await Owner(dev_deps).change(defaults.BURST_AFTER_MEDIA_S, 15)
    outcome = await say(dev_deps, fakes, "oldingi holatiga qaytar", answer_of("revert"))
    assert outcome.reply == S.SETTING_REVERTED(change="burst.after_media_s: 15 s → 10 s")
    assert (await service.snapshot(dev_deps))[defaults.BURST_AFTER_MEDIA_S] == 10.0


@pytest.mark.parametrize(
    ("text", "answer", "error"),
    [
        ("rasmdan keyin 100 soniya kut", answer_of("change", ("burst.after_media_s", 100)), SettingInvalid),
        ("javoblaringni uzunroq qil", answer_of("change", ("persona.personal_verbosity", "long")), SettingPersonalOnly),
    ],
    ids=["out_of_range", "personal_key"],
)
async def test_a_refused_request_by_chat_changes_nothing(
    dev_deps: DevDeps, fakes: Fakes, text: str, answer: dict[str, JsonValue], error: type[YordamchiError]
) -> None:
    with pytest.raises(error):
        await say(dev_deps, fakes, text, answer)
    assert (await service.snapshot(dev_deps)).version == 0
    assert await list_replies(dev_deps.db) == []


async def test_an_assistant_failure_changes_nothing(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("settings", ProviderUnavailable(provider="claude"))
    with pytest.raises(ProviderUnavailable):
        await service.handle_request(
            dev_deps, make_owner_origin(), "rasmdan keyin 15 soniya kut", ask=gate_asker(dev_deps)
        )
    assert (await service.snapshot(dev_deps)).version == 0


# --- the personal bot: its own keys, its own asker (the conversational confirm, P3) --------------------------------
async def test_the_personal_bot_holds_a_weakening_change_for_its_own_asker(personal_deps: PersonalDeps) -> None:
    asker = RecordingAsker()
    origin = make_owner_origin(BotName.PERSONAL, update_id=5)
    applied = await service.change_setting(personal_deps, origin, "persona.personal_verbosity", "medium", ask=asker)
    assert (applied.kind, asker.asked) == (OutcomeKind.APPLIED, [])

    asked = await service.change_setting(
        personal_deps, make_owner_origin(BotName.PERSONAL, update_id=6), "caps.google_monthly_usd", 100, ask=asker
    )
    assert (asked.kind, asked.decision_id) == (OutcomeKind.ASKED, 1)
    [pending] = asker.asked
    assert pending.restated == "caps.google_monthly_usd: 40 USD → 100 USD"
    assert (await service.snapshot(personal_deps))[defaults.CAPS_GOOGLE_MONTHLY_USD] == 40

    approval = make_owner_origin(BotName.PERSONAL, update_id=7)
    async with personal_deps.db.uow() as s:
        written = await service.apply_pending_change(
            s, bot=BotName.PERSONAL, origin=approval, change=pending, at=personal_deps.clock.now(), decision_id=None
        )
    assert written == ("caps.google_monthly_usd",)
    assert (await service.snapshot(personal_deps))[defaults.CAPS_GOOGLE_MONTHLY_USD] == 100


async def test_a_held_change_is_checked_again_when_approved(personal_deps: PersonalDeps) -> None:
    asker = RecordingAsker()
    await service.change_setting(
        personal_deps, make_owner_origin(BotName.PERSONAL, update_id=6), "caps.google_monthly_usd", 100, ask=asker
    )
    [pending] = asker.asked
    tampered = replace(pending, items=(replace(pending.items[0], value=10**9),))
    with pytest.raises(SettingInvalid):
        async with personal_deps.db.uow() as s:
            await service.apply_pending_change(
                s,
                bot=BotName.PERSONAL,
                origin=make_owner_origin(BotName.PERSONAL, update_id=7),
                change=tampered,
                at=personal_deps.clock.now(),
                decision_id=None,
            )
    assert (await service.snapshot(personal_deps)).version == 0


async def test_a_personal_revert_of_a_shared_key_returns_to_the_dev_value(
    dev_deps: DevDeps, personal_deps: PersonalDeps
) -> None:
    await service.record_version(dev_deps, make_owner_origin(), defaults.CAPS_WARN_PCT, 90, reason="change")
    asker = RecordingAsker()
    lowered = await service.change_setting(
        personal_deps, make_owner_origin(BotName.PERSONAL, update_id=5), "caps.warn_pct", 70, ask=asker
    )
    assert lowered.reply == S.SETTING_CHANGED(change="caps.warn_pct: 90 % → 70 %")
    reverted = await service.revert_setting(personal_deps, make_owner_origin(BotName.PERSONAL, update_id=6), ask=asker)
    assert reverted.kind is OutcomeKind.ASKED  # raising a cap back loosens it: one question first
    [pending] = asker.asked
    assert (pending.reason, pending.items[0].value) == ("revert", 90)
