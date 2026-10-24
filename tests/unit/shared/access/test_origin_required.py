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

from tests.factories import (
    DEV_GROUP_ID,
    OWNER_ID,
    Fakes,
    gate_asker,
    make_decision,
    make_owner_origin,
    make_update,
)
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.service import DecisionReply
from yordamchi.dev.projects import service as projects
from yordamchi.dev.turns import service as turns
from yordamchi.shared.access import domain
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.inbox import domain as inbox_domain
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.settings import defaults, store
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import ChangeItem, PendingChange, Proposal
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault import store as vault_store
from yordamchi.shared.vault.domain import NewSecret
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


async def _require_matching_origin(deps: DevDeps, fakes: Fakes) -> object:
    del fakes
    inbox_domain.require_matching_origin(FAKE, make_update(1), deps.bot)
    return None


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
