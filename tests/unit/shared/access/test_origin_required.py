"""Every public entry that takes an `origin` refuses anything but a minted `OwnerOrigin`, before it reads or writes
(CONVENTIONS §5, A5; UC-09 F, NFR-SEC-09; T33 bug: `gate.answer_decision` approved a deploy for any object whose
`.bot` was DEV).

Untrusted content (a web page, a tool result, executor output) can at most build an object shaped like an origin. The
type annotation stops that only under mypy; `access.domain.require_owner_origin` stops it at runtime. The inventory
test finds every public function in `src/` with an `origin: OwnerOrigin` parameter, so a new one without a refusal
case here fails.
"""

import ast
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest
from sqlalchemy import func, select

from tests.factories import (
    DEV_GROUP_ID,
    OWNER_ID,
    Fakes,
    gate_asker,
    make_decision,
    make_owner_origin,
    make_part,
    make_update,
)
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.service import DecisionReply
from yordamchi.dev.newproject import service as newproject
from yordamchi.dev.projects import service as projects
from yordamchi.dev.questions import service as questions
from yordamchi.dev.servers import service as servers
from yordamchi.dev.turns import service as turns
from yordamchi.shared.access import domain
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit.models import AuditEvent
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst.domain import CloseReason
from yordamchi.shared.inbox import domain as inbox_domain
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.integrations import service as integrations
from yordamchi.shared.integrations.models import Integration
from yordamchi.shared.integrations.service import AddApiKey, AddIntegration, IntegrationsState
from yordamchi.shared.settings import defaults, store
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import ChangeItem, PendingChange, Proposal
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault import store as vault_store
from yordamchi.shared.vault.domain import NewSecret, SecretRef
from yordamchi.shared.voice import service as voice

SRC: Final = Path(domain.__file__).resolve().parents[2]


@dataclass(frozen=True)
class Lookalike:
    """Every field an `OwnerOrigin` has, naming the dev bot and the owner, never minted by `shared/access`."""

    bot: BotName = BotName.DEV
    update_id: int = 1
    user_id: int = OWNER_ID
    chat_id: int = DEV_GROUP_ID


FAKE: Final = cast(OwnerOrigin, Lookalike())


def test_a_minted_origin_passes_and_comes_back() -> None:
    origin = make_owner_origin(update_id=5)
    assert require_owner_origin(origin, BotName.DEV) is origin
    assert require_owner_origin(origin) is origin


@pytest.mark.parametrize("wrong", [None, Lookalike(), {"bot": "dev", "update_id": 1}, "owner"], ids=repr)
def test_anything_else_is_a_type_error(wrong: object) -> None:
    with pytest.raises(TypeError, match="OwnerOrigin"):
        require_owner_origin(wrong, BotName.DEV)
    with pytest.raises(TypeError, match="OwnerOrigin"):
        require_owner_origin(wrong)


def test_an_origin_of_the_other_bot_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="owner origin of the personal bot"):
        require_owner_origin(make_owner_origin(BotName.PERSONAL), BotName.DEV)


# --- every entry ------------------------------------------------------------------------------------------------------
type Entry = Callable[[DevDeps, Fakes], Awaitable[object]]


async def _apply_pending_change(deps: DevDeps, fakes: Fakes) -> object:
    change = PendingChange(
        reason="change",
        items=(ChangeItem(key=defaults.GATE_SECRET_SCAN_ENABLED.key, value=False, old=True),),
        restated="gate.secret_scan_enabled: yoqilgan → o'chirilgan",
        origin_update_id=1,
    )
    async with deps.db.uow() as s:
        return await settings.apply_pending_change(
            s, bot=deps.bot, origin=FAKE, change=change, at=fakes.clock.now(), decision_id=None
        )


async def _add_secrets(deps: DevDeps, fakes: Fakes) -> object:
    secret = NewSecret(kind="google_api_key", value="AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
    async with deps.db.uow() as s:
        return await vault.add_secrets(s, fakes.keystore, origin=FAKE, secrets=[secret], at=fakes.clock.now())


async def _answer_owner(deps: DevDeps, fakes: Fakes) -> object:
    """A bare "ha" burst, the only open item a deploy decision (`_written` checks it stays pending)."""
    part = make_part(1, text="ha")
    item = burst_domain.turn_input(
        1, DEV_GROUP_ID, [part], opened_at=fakes.clock.now(), closed_at=fakes.clock.now(), reason=CloseReason.QUIET
    )
    return await questions.answer_owner(deps, turns.TurnsState(), FAKE, item)


def _description() -> burst_domain.TurnInput:
    """ "yangi loyiha: …" as routing hands it to `dev/newproject`."""
    part = make_part(1, text="yangi loyiha: kutubxona boti")
    return burst_domain.turn_input(
        1, DEV_GROUP_ID, [part], opened_at=part.at, closed_at=part.at, reason=CloseReason.QUIET
    )


INTEGRATION_URL: Final = "https://hr.bonvi.example.uz/mcp"
STORED_KEY: Final = SecretRef(id=1, name="generic_secret", kind="generic_secret", last4="abcd", bots=(BotName.DEV,))


async def _require_matching_origin(deps: DevDeps, fakes: Fakes) -> object:
    del fakes
    inbox_domain.require_matching_origin(FAKE, make_update(1), deps.bot)
    return None


async def _incoming_of(deps: DevDeps, fakes: Fakes) -> object:
    """The dev bot's Telegram translation: an aiogram message with a lookalike origin never becomes an update."""
    from aiogram.types import Message

    from tests.factories import TELEGRAM_DATE, tg_chat, tg_user
    from yordamchi.bots.dev import telegram

    del deps, fakes
    raw = {
        "message_id": 1,
        "date": TELEGRAM_DATE,
        "chat": tg_chat(DEV_GROUP_ID),
        "from": tg_user(OWNER_ID),
        "text": "ha",
    }
    return telegram.incoming_of(FAKE, Message.model_validate(raw))


async def _member_of(deps: DevDeps, fakes: Fakes) -> object:
    from aiogram.types import ChatMemberUpdated

    from tests.factories import tg_member_change
    from yordamchi.bots.dev import telegram

    del deps, fakes
    raw = tg_member_change(1, user_id=OWNER_ID, chat_id=DEV_GROUP_ID)["my_chat_member"]
    return telegram.member_of(FAKE, ChatMemberUpdated.model_validate(raw))


async def _intake_accepted(deps: DevDeps, fakes: Fakes) -> object:
    """The running bot's intake hook: a lookalike origin wakes nothing and starts no speech-key job."""
    import asyncio

    from yordamchi.bots.dev.runtime import Intake
    from yordamchi.shared.health import service as health
    from yordamchi.shared.inbox.domain import Accepted

    del fakes
    intake = Intake(deps=deps, health=health.new_state(deps, version="test"), wake=asyncio.Event())
    try:
        intake.accepted(FAKE, Accepted(update_id=1, duplicate=False))
    finally:
        assert not intake.wake.is_set()
    return None


async def _personal_incoming_of(deps: DevDeps, fakes: Fakes) -> object:
    """The personal bot's Telegram translation (T77): a lookalike origin never becomes an update."""
    from aiogram.types import Message

    from tests.factories import TELEGRAM_DATE, tg_chat, tg_user
    from yordamchi.bots.personal import telegram

    del deps, fakes
    raw = {"message_id": 1, "date": TELEGRAM_DATE, "chat": tg_chat(OWNER_ID), "from": tg_user(OWNER_ID), "text": "ha"}
    return telegram.incoming_of(FAKE, Message.model_validate(raw))


async def _personal_member_of(deps: DevDeps, fakes: Fakes) -> object:
    from aiogram.types import ChatMemberUpdated

    from tests.factories import tg_member_change
    from yordamchi.bots.personal import telegram

    del deps, fakes
    raw = tg_member_change(1, user_id=OWNER_ID, chat_id=OWNER_ID)["my_chat_member"]
    return telegram.member_of(FAKE, ChatMemberUpdated.model_validate(raw))


async def _personal_intake_accepted(deps: DevDeps, fakes: Fakes) -> object:
    """The personal bot's intake hook (T77): a lookalike origin wakes nothing and starts no speech-key job."""
    import asyncio

    from tests.factories import make_personal_deps
    from yordamchi.bots.personal.runtime import Intake
    from yordamchi.shared.health import service as health
    from yordamchi.shared.inbox.domain import Accepted

    personal = make_personal_deps(db=deps.db, paths=deps.paths, fakes=fakes)
    intake = Intake(deps=personal, health=health.new_state(personal, version="test"), wake=asyncio.Event())
    try:
        intake.accepted(FAKE, Accepted(update_id=1, duplicate=False))
    finally:
        assert not intake.wake.is_set()
    return None


async def _confirm_change(deps: DevDeps, fakes: Fakes) -> object:
    """The personal bot's "ha" to a waiting weakening change (T77): refused before the change is written."""
    del fakes
    change = PendingChange(
        reason="change",
        items=(ChangeItem(key=defaults.GATE_SECRET_SCAN_ENABLED.key, value=False, old=True),),
        restated="gate.secret_scan_enabled: yoqilgan → o'chirilgan",
        origin_update_id=1,
    )
    return await settings.confirm_change(deps, FAKE, change)


ENTRIES: Final[dict[str, Entry]] = {
    "yordamchi.shared.inbox.domain.require_matching_origin": _require_matching_origin,
    "yordamchi.shared.settings.service.record_version": lambda deps, _: settings.record_version(
        deps, FAKE, defaults.BURST_AFTER_MEDIA_S, 6, reason="change"
    ),
    "yordamchi.shared.settings.service.change_setting": lambda deps, _: settings.change_setting(
        deps, FAKE, defaults.BURST_AFTER_MEDIA_S.key, 6, ask=gate_asker(deps)
    ),
    "yordamchi.shared.settings.service.apply_proposals": lambda deps, _: settings.apply_proposals(
        deps, FAKE, (Proposal(defaults.BURST_AFTER_MEDIA_S.key, 6),), ask=gate_asker(deps)
    ),
    "yordamchi.shared.settings.service.apply_pending_change": _apply_pending_change,
    "yordamchi.shared.settings.service.revert_setting": lambda deps, _: settings.revert_setting(
        deps, FAKE, ask=gate_asker(deps)
    ),
    "yordamchi.shared.settings.service.show_settings": lambda deps, _: settings.show_settings(deps, FAKE),
    "yordamchi.shared.settings.service.handle_request": lambda deps, _: settings.handle_request(
        deps, FAKE, "rasmdan keyin 6 soniya kut", ask=gate_asker(deps)
    ),
    "yordamchi.shared.inbox.service.accept_update": lambda deps, _: inbox.accept_update(deps, FAKE, make_update(1)),
    "yordamchi.shared.voice.service.add_owner_term": lambda deps, _: voice.add_owner_term(deps, FAKE, "Imzo"),
    "yordamchi.shared.voice.service.configure_speech_keys": lambda deps, _: voice.configure_speech_keys(deps, FAKE, ()),
    "yordamchi.shared.vault.service.add_secrets": _add_secrets,
    "yordamchi.shared.vault.service.remove_secret": lambda deps, _: vault.remove_secret(deps, FAKE, "google_api_key"),
    "yordamchi.dev.projects.service.alias_project": lambda deps, _: projects.alias_project(deps, FAKE, "BonviHR", "hr"),
    "yordamchi.dev.projects.service.archive_project": lambda deps, _: projects.archive_project(
        deps, FAKE, "BonviHR", archived=True
    ),
    "yordamchi.dev.projects.service.describe_project": lambda deps, _: projects.describe_project(
        deps, FAKE, "BonviHR", "Kadrlar tizimi"
    ),
    "yordamchi.dev.gate.service.answer_decision": lambda deps, _: gate.answer_decision(deps, FAKE, DecisionReply("ha")),
    "yordamchi.dev.turns.service.answer_revert": lambda deps, _: turns.answer_revert(
        deps, FAKE, turns.RevertReply("ha")
    ),
    "yordamchi.dev.servers.service.add_server_host": lambda deps, _: servers.add_server_host(deps, FAKE, "bonvi-srv"),
    "yordamchi.dev.servers.service.remove_server_host": lambda deps, _: servers.remove_server_host(
        deps, FAKE, "bonvi-srv"
    ),
    "yordamchi.dev.questions.service.answer_owner": _answer_owner,
    "yordamchi.dev.newproject.service.start_draft": lambda deps, _: newproject.start_draft(
        deps, newproject.NewProjectState(turns=turns.TurnsState()), FAKE, _description()
    ),
    "yordamchi.dev.newproject.service.take_message": lambda deps, _: newproject.take_message(
        deps, newproject.NewProjectState(turns=turns.TurnsState()), FAKE, _description()
    ),
    "yordamchi.shared.integrations.service.add_integration": lambda deps, _: integrations.add_integration(
        deps, IntegrationsState(), FAKE, AddIntegration("Bonvi HR", INTEGRATION_URL)
    ),
    "yordamchi.shared.integrations.service.add_api_key": lambda deps, _: integrations.add_api_key(
        deps, FAKE, AddApiKey("Ob-havo", "https://api.weather.example.uz/v1", "ob-havo", STORED_KEY)
    ),
    "yordamchi.shared.integrations.service.complete_oauth": lambda deps, _: integrations.complete_oauth(
        deps, IntegrationsState(), FAKE, pasted="http://localhost:33418/callback?code=c0de1234&state=st-1"
    ),
    "yordamchi.shared.integrations.service.remove_integration": lambda deps, _: integrations.remove_integration(
        deps, IntegrationsState(), FAKE, "Bonvi HR"
    ),
    "yordamchi.shared.integrations.service.restore_integration": lambda deps, _: integrations.restore_integration(
        deps, FAKE, "Bonvi HR"
    ),
    "yordamchi.shared.integrations.service.reconnect_integration": lambda deps, _: integrations.reconnect_integration(
        deps, IntegrationsState(), FAKE, "Bonvi HR"
    ),
    "yordamchi.shared.integrations.service.rename_integration": lambda deps, _: integrations.rename_integration(
        deps, FAKE, "Bonvi HR", "Kadrlar"
    ),
    "yordamchi.shared.integrations.service.assign_integration": lambda deps, _: integrations.assign_integration(
        deps, FAKE, "Bonvi HR", [BotName.DEV, BotName.PERSONAL]
    ),
    "yordamchi.bots.dev.telegram.incoming_of": _incoming_of,
    "yordamchi.bots.dev.telegram.member_of": _member_of,
    "yordamchi.bots.dev.runtime.accepted": _intake_accepted,
    "yordamchi.bots.personal.telegram.incoming_of": _personal_incoming_of,
    "yordamchi.bots.personal.telegram.member_of": _personal_member_of,
    "yordamchi.bots.personal.runtime.accepted": _personal_intake_accepted,
    "yordamchi.shared.settings.service.confirm_change": _confirm_change,
}


def _public_origin_entries() -> set[str]:
    """`<module>.<function>` of every public function in `src/` with an `origin` parameter annotated `OwnerOrigin`."""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
                continue
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for argument in arguments:
                annotation = argument.annotation
                if argument.arg == "origin" and isinstance(annotation, ast.Name) and annotation.id == "OwnerOrigin":
                    found.add(f"{module}.{node.name}")
    return found


def test_every_public_entry_with_an_origin_has_a_refusal_case() -> None:
    assert _public_origin_entries() == set(ENTRIES)


async def _written(deps: DevDeps) -> tuple[int, int, int, int]:
    """(setting versions, approved or consumed decisions, vault secrets, inbox rows)."""
    async with deps.db.read() as s:
        versions = await store.get_max_setting_version_id(s)
        decisions = await gate_store.list_recent_decisions(s, limit=20)
        secrets = await vault_store.list_active_secrets(s)
        updates = await inbox_store.list_inbound_updates(s, deps.bot.value)
    approved = sum(1 for row in decisions if row.status in {"approved", "consumed"})
    return versions, approved, len(secrets), len(updates)


@pytest.mark.parametrize("name", sorted(ENTRIES))
async def test_a_lookalike_origin_is_refused_before_anything_changes(
    dev_deps: DevDeps, fakes: Fakes, name: str
) -> None:
    async with dev_deps.db.uow() as s:
        pending = await make_decision(s, at=fakes.clock.now(), gate_class="deploy")
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await ENTRIES[name](dev_deps, fakes)
    assert await _written(dev_deps) == (0, 0, 0, 0)
    async with dev_deps.db.read() as s:
        row = await gate_store.get_decision(s, pending.id)
    assert row is not None
    assert row.status == "pending"
    assert fakes.assistant.requests == []  # refused before any model is asked


async def test_the_gate_no_longer_approves_a_deploy_for_a_lookalike(dev_deps: DevDeps, fakes: Fakes) -> None:
    """The regression of the T33 bug: the only pending decision, a bare "ha", an object with `.bot == DEV`."""
    async with dev_deps.db.uow() as s:
        pending = await make_decision(s, at=fakes.clock.now(), gate_class="deploy")
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await gate.answer_decision(dev_deps, FAKE, DecisionReply("ha"))
    answer = await gate.answer_decision(dev_deps, make_owner_origin(update_id=2), DecisionReply("ha"))
    assert (answer.kind, answer.decision_id) == (
        gate.AnswerKind.APPROVED,
        pending.id,
    )  # the owner's own "ha" still does


INTEGRATION_ENTRIES: Final = sorted(name for name in ENTRIES if ".integrations." in name)


@pytest.mark.parametrize("name", INTEGRATION_ENTRIES)
async def test_a_lookalike_origin_changes_no_integration(dev_deps: DevDeps, fakes: Fakes, name: str) -> None:
    """T50 entries: refused before the registry, the vault or the MCP server is touched (no row, no audit, no call)."""
    async with dev_deps.db.uow() as s:
        s.add(
            Integration(
                slug="bonvi_hr",
                display_name="Bonvi HR",
                kind="mcp_http",
                url=INTEGRATION_URL,
                description="",
                auth="none",
                bots_json=["dev"],
                tool_policy_json={"dev": [], "personal": []},
                tools_cache_json=[],
                status="ok",
                created_at=fakes.clock.now(),
            )
        )
    with pytest.raises(TypeError, match="OwnerOrigin"):
        await ENTRIES[name](dev_deps, fakes)
    async with dev_deps.db.read() as s:
        rows = list((await s.scalars(select(Integration))).all())
        audits = await s.scalar(select(func.count()).select_from(AuditEvent))
    assert [(row.slug, row.display_name, row.bots_json, row.removed_at) for row in rows] == [
        ("bonvi_hr", "Bonvi HR", ["dev"], None)
    ]
    assert audits == 0
    assert fakes.mcp.list_calls == []
    assert len(INTEGRATION_ENTRIES) == 8
