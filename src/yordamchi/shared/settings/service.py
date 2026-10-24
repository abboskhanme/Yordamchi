"""Settings use cases (SPEC §2.3, §3.2.15, §4.3; UC-09, UC-34; CONVENTIONS §5; NFR-OPS-03, NFR-SEC-09).

- `snapshot(deps)`: every setting of this bot as of now. Every job reads it once per turn, so a change applies from
  the next message; it is a row, so it survives a restart.
- Every write needs an `OwnerOrigin`, which only `shared/access` mints from a verified owner update. Executor output,
  tool results and web content carry none, so they can never change a setting (A5, UC-09 F, NFR-SEC-09).
- `change_setting`, `apply_proposals`, `revert_setting`: every proposal is checked before any applies
  (`domain.plan_changes`). A refusal raises a `DomainError` whose Uzbek message `guarded()` sends, and nothing is
  stored. A change that weakens a safeguard goes to `ask`: ONE question (a `settings_weaken` decision through
  `dev/gate` on the dev bot, a conversational confirm on the personal bot), and `apply_pending_change` writes it in
  that question's unit of work once the owner says "ha". Anything else applies at once: one unit of work holds a
  `setting_versions` row and an audit row per key, plus the one-line restatement queued in the outbox.
- `handle_request`: the chat path. The `settings` Assistant turns the owner's words into proposals
  (`prompts/dev/settings_system.md` holds the fixed parts; the settings table travels in `instructions`, SPEC §5.4),
  then the same steps. An ambiguous request gets one clarifying question and changes nothing.
- `list_settings` / `show_settings`: "sozlamalaring qanday?" — a grouped summary, then the full list as a file.

`shared` cannot import `dev` (C1), so the dev bot passes its asker in:
`ask=functools.partial(gate.service.ask_setting_change, deps)`.
"""

import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import SettingUnknown
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence.uow import Session
from yordamchi.ports.assistant import AssistantRequest
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults, domain, store
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import (
    Intent,
    Interpretation,
    PendingChange,
    PlannedChange,
    Proposal,
    SettingsListing,
    Snapshot,
    StoredValue,
)
from yordamchi.shared.settings.models import SettingVersion
from yordamchi.shared.settings.registry import AnySetting, SettingValue

log = get_log(__name__)

type ChangeReason = Literal["change", "revert", "import"]
# The one question a weakening change gets; returns its id (a `decisions` row on the dev bot).
type AskOwner = Callable[[PendingChange], Awaitable[int]]

SYSTEM_PROMPT: Final = "dev/settings_system"
CONTEXT_PROMPT: Final = "dev/settings_context"
REPLY_KIND: Final = Kind.REPORT


class OutcomeKind(StrEnum):
    APPLIED = "applied"
    ASKED = "asked"  # one question is open; nothing changed yet
    UNCHANGED = "unchanged"  # every value was already so
    NOTHING_TO_REVERT = "nothing_to_revert"
    CLARIFY = "clarify"  # one clarifying question naming the candidates
    SHOWN = "shown"
    NEEDS_CODE = "needs_code"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class SettingsOutcome:
    """What a settings request did. `reply` is what this call queued for the owner (None when the asker posts the
    question instead); `keys` are the settings it wrote or asked about."""

    kind: OutcomeKind
    reply: BotText | None = None
    keys: tuple[str, ...] = ()
    decision_id: int | None = None


# --- reading --------------------------------------------------------------------------------------------------------
def _stored(rows: Iterable[SettingVersion]) -> dict[str, StoredValue]:
    return {row.key: StoredValue(key=row.key, value=row.value_json, row_id=row.id) for row in rows}


async def snapshot(deps: SharedDeps) -> Snapshot:
    """Every setting of this bot as of now. Read it once per turn or job; never keep it in a global."""
    async with deps.db.read() as s:
        own_rows = await store.list_latest_versions(s)
        own_version = await store.get_max_setting_version_id(s)
    dev_rows: list[SettingVersion] = []
    if deps.bot is BotName.PERSONAL and deps.peer_db is not None:
        async with deps.peer_db.read() as ro:
            dev_rows = await store.list_latest_versions(ro, domain.shared_keys(REGISTRY))
    resolution = domain.resolve(
        REGISTRY, deps.bot, own=_stored(own_rows), dev=_stored(dev_rows), bootstrap=deps.bootstrap
    )
    for skipped in resolution.skipped:
        log.warning("setting_row_skipped", key=skipped)
    dev_version = max((row.id for row in dev_rows), default=0)
    return Snapshot(
        bot=deps.bot,
        version=own_version + dev_version,
        values=resolution.values,
        sources=resolution.sources,
    )


def as_json(value: SettingValue) -> JsonValue:
    return domain.json_of(value)


# --- writing --------------------------------------------------------------------------------------------------------
def _owner_origin(origin: object, bot: BotName) -> OwnerOrigin:
    """The proof that a write comes from the owner of this bot. Anything else is a wiring bug: nothing is written."""
    if not isinstance(origin, OwnerOrigin):
        msg = "a settings change needs the OwnerOrigin of a verified owner update"
        raise TypeError(msg)
    if origin.bot is not bot:
        msg = f"an owner origin of the {origin.bot} bot cannot change {bot} settings"
        raise ValueError(msg)
    return origin


async def _write(
    s: Session,
    *,
    bot: BotName,
    origin_update_id: int,
    setting: AnySetting,
    value: SettingValue,
    reason: ChangeReason,
    at: datetime,
    decision_id: int | None = None,
) -> int:
    """The next version of `setting` and its audit row, in the caller's unit of work. Returns the version."""
    version = await store.get_latest_version_number(s, setting.key) + 1
    row = await store.add_setting_version(
        s,
        key=setting.key,
        value=domain.json_of(value),
        version=version,
        reason=reason,
        actor="owner",
        origin_update_id=origin_update_id,
        created_at=at,
    )
    await audit.record_event(
        s,
        bot=bot,
        at=at,
        actor=AuditActor.OWNER,
        kind=AuditKind.SETTING_CHANGED,
        ref_type="setting_version",
        ref_id=str(row.id),
        data={"key": setting.key, "version": version, "reason": reason, "decision_id": decision_id},
    )
    return version


async def _tell(
    s: Session, deps: SharedDeps, origin: OwnerOrigin, text: BotText, *, at: datetime, max_parts: int, part: str = ""
) -> None:
    """Queue the reply to the owner's message in the caller's unit of work (one reply per update and `part`)."""
    await outbox.enqueue_text(
        s,
        bot=deps.bot,
        chat_id=origin.chat_id,
        text=text,
        kind=REPLY_KIND,
        ref=f"settings-{origin.update_id}{part}",
        at=at,
        max_parts=max_parts,
    )


async def _reply(
    deps: SharedDeps, origin: OwnerOrigin, snap: Snapshot, kind: OutcomeKind, text: BotText
) -> SettingsOutcome:
    async with deps.db.uow() as s:
        await _tell(s, deps, origin, text, at=deps.clock.now(), max_parts=snap[defaults.OUTBOX_MAX_PARTS])
    return SettingsOutcome(kind, text)


async def record_version(
    deps: SharedDeps, origin: OwnerOrigin, setting: AnySetting, raw: JsonValue, *, reason: ChangeReason
) -> int:
    """Validate `raw` and append it as the next version of `setting`, with its audit row; returns that version.

    For a feature that edits one value itself (aliases, terms) and tells the owner on its own. It never asks, so
    the caller must not pass a safeguard (`domain.weakens`): those go through `change_setting`. A key outside this
    bot's scope raises `SettingPersonalOnly` / `SettingDevOnly`; a bad value `SettingInvalid`, and nothing is stored.
    """
    owner = _owner_origin(origin, deps.bot)
    domain.require_writable_by(setting, deps.bot)
    value = domain.validate(setting, raw)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        version = await _write(
            s, bot=deps.bot, origin_update_id=owner.update_id, setting=setting, value=value, reason=reason, at=now
        )
    log.info("setting_version_recorded", key=setting.key, version=version, reason=reason)
    return version


async def change_setting(
    deps: SharedDeps, origin: OwnerOrigin, key: str, value: JsonValue, *, ask: AskOwner
) -> SettingsOutcome:
    """Set one key (UC-09). See `apply_proposals`."""
    return await apply_proposals(deps, origin, (Proposal(key=key, value=value),), ask=ask)


async def apply_proposals(
    deps: SharedDeps,
    origin: OwnerOrigin,
    proposals: Sequence[Proposal],
    *,
    ask: AskOwner,
    reason: domain.ChangeReason = "change",
) -> SettingsOutcome:
    """Change one or more keys as one request: all of it applies, or all of it waits for one "ha", or none of it.

    Raises `SettingUnknown`, `SettingPersonalOnly` ("buni shaxsiy botga ayting"), `SettingDevOnly` or
    `SettingInvalid` (out of range, unknown model: the message names the options) before anything is stored.
    """
    owner = _owner_origin(origin, deps.bot)
    snap = await snapshot(deps)
    planned = domain.plan_changes(REGISTRY, deps.bot, snap.values, proposals)
    return await _carry_out(deps, owner, snap, planned, ask=ask, reason=reason)


async def _carry_out(
    deps: SharedDeps,
    origin: OwnerOrigin,
    snap: Snapshot,
    planned: Sequence[PlannedChange],
    *,
    ask: AskOwner,
    reason: domain.ChangeReason,
) -> SettingsOutcome:
    keys = tuple(change.setting.key for change in planned)
    if not planned:
        return await _reply(deps, origin, snap, OutcomeKind.UNCHANGED, S.SETTINGS_UNCHANGED())
    restated = redact.scrubbed(domain.restate(planned))
    if any(change.weakens for change in planned):
        pending = domain.pending_change(planned, reason=reason, restated=restated, origin_update_id=origin.update_id)
        decision_id = await ask(pending)  # the asker posts the one question; nothing is written here
        log.info("setting_change_asked", keys=list(keys), reason=reason, decision_id=decision_id)
        return SettingsOutcome(OutcomeKind.ASKED, keys=keys, decision_id=decision_id)
    reply = S.SETTING_REVERTED(change=restated) if reason == "revert" else S.SETTING_CHANGED(change=restated)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        for change in planned:
            await _write(
                s,
                bot=deps.bot,
                origin_update_id=origin.update_id,
                setting=change.setting,
                value=change.new,
                reason=reason,
                at=now,
            )
        await _tell(s, deps, origin, reply, at=now, max_parts=snap[defaults.OUTBOX_MAX_PARTS])
    log.info("settings_changed", keys=list(keys), reason=reason)
    return SettingsOutcome(OutcomeKind.APPLIED, reply, keys)


async def apply_pending_change(
    s: Session, *, bot: BotName, origin: OwnerOrigin, change: PendingChange, at: datetime, decision_id: int | None
) -> tuple[str, ...]:
    """The owner said "ha" to `change`: write it in the caller's unit of work (the question's), so the answer and
    the change commit together. `origin` is the update that said "ha". Every value is checked again against today's
    registry; a refusal raises and the caller's unit of work rolls back. Returns the keys written."""
    owner = _owner_origin(origin, bot)
    written: list[str] = []
    for proposal in change.proposals():
        setting = REGISTRY.get(proposal.key)
        if setting is None:
            raise SettingUnknown(key=proposal.key)
        domain.require_writable_by(setting, bot)
        value = domain.validate(setting, proposal.value)
        await _write(
            s,
            bot=bot,
            origin_update_id=owner.update_id,
            setting=setting,
            value=value,
            reason=change.reason,
            at=at,
            decision_id=decision_id,
        )
        written.append(setting.key)
    log.info("settings_change_confirmed", keys=written, reason=change.reason, decision_id=decision_id)
    return tuple(written)


# --- revert ---------------------------------------------------------------------------------------------------------
async def revert_setting(
    deps: SharedDeps, origin: OwnerOrigin, key: str | None = None, *, ask: AskOwner
) -> SettingsOutcome:
    """ "oldingi holatiga qaytar [key]": a new row holding the value from before this bot's newest change of `key`,
    or of the newest change of any key. It passes the same checks, so a revert that loosens a safeguard asks too."""
    owner = _owner_origin(origin, deps.bot)
    return await _revert(deps, owner, () if key is None else (key,), ask=ask)


def _setting_of(key: str, bot: BotName) -> AnySetting:
    setting = REGISTRY.get(key)
    if setting is None:
        raise SettingUnknown(key=key)
    domain.require_writable_by(setting, bot)
    return setting


async def _revert(deps: SharedDeps, origin: OwnerOrigin, keys: Sequence[str], *, ask: AskOwner) -> SettingsOutcome:
    snap = await snapshot(deps)
    async with deps.db.read() as s:
        if keys:
            names = tuple(keys)
        else:
            newest = await store.find_newest_setting_version(s)
            names = () if newest is None else (newest.key,)
        settings = [_setting_of(name, deps.bot) for name in names]
        histories = {
            setting.key: _stored_list(await store.list_setting_history(s, setting.key)) for setting in settings
        }
    dev_rows: dict[str, StoredValue] = {}
    shared = [setting.key for setting in settings if setting.shared]
    if deps.bot is BotName.PERSONAL and deps.peer_db is not None and shared:
        async with deps.peer_db.read() as ro:
            dev_rows = _stored(await store.list_latest_versions(ro, shared))
    proposals = [
        Proposal(
            key=setting.key,
            value=domain.json_of(
                domain.value_before(
                    setting,
                    deps.bot,
                    history=histories[setting.key],
                    dev=dev_rows.get(setting.key),
                    bootstrap=deps.bootstrap,
                )
            ),
        )
        for setting in settings
        if histories[setting.key]
    ]
    if not proposals:
        text = S.SETTING_NOTHING_TO_REVERT(key=keys[0]) if len(keys) == 1 else S.SETTINGS_NOTHING_TO_REVERT()
        return await _reply(deps, origin, snap, OutcomeKind.NOTHING_TO_REVERT, text)
    planned = domain.plan_changes(REGISTRY, deps.bot, snap.values, proposals)
    return await _carry_out(deps, origin, snap, planned, ask=ask, reason="revert")


def _stored_list(rows: Iterable[SettingVersion]) -> list[StoredValue]:
    return [StoredValue(key=row.key, value=row.value_json, row_id=row.id) for row in rows]


# --- "sozlamalaring qanday?" ----------------------------------------------------------------------------------------
def _listing(deps: SharedDeps, snap: Snapshot) -> SettingsListing:
    default_values = {key: domain.default_for(REGISTRY[key], deps.bot, deps.bootstrap) for key in snap.values}
    return domain.listing(REGISTRY, deps.bot, snap.values, default_values, redact.scrubbed)


async def list_settings(deps: SharedDeps) -> SettingsListing:
    """Every group, the stored values, secrets masked (UC-09: ≤ 3 s; it is one local read)."""
    started = time.monotonic()
    result = _listing(deps, await snapshot(deps))
    duration_ms = round((time.monotonic() - started) * 1000)
    log.info("settings_listed", total=result.total, changed=result.changed, duration_ms=duration_ms)
    return result


async def show_settings(deps: SharedDeps, origin: OwnerOrigin) -> SettingsListing:
    """The listing, queued as the reply: the grouped summary as a message, then the full list. The full list is
    queued with `max_parts=1`, so the outbox sends it as one `.md` file captioned with its first line (it is far
    longer than one message)."""
    owner = _owner_origin(origin, deps.bot)
    snap = await snapshot(deps)
    result = _listing(deps, snap)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await _tell(s, deps, owner, result.summary, at=now, max_parts=snap[defaults.OUTBOX_MAX_PARTS])
        await _tell(s, deps, owner, result.full, at=now, max_parts=1, part="-full")
    log.info("settings_shown", total=result.total, changed=result.changed)
    return result


# --- the chat path (SPEC §3.2.15) -----------------------------------------------------------------------------------
async def interpret_request(deps: SharedDeps, text: str) -> Interpretation:
    """The `settings` Assistant's reading of the owner's words, checked against the registry (it reads only; nothing
    changes here). An `ExternalError` from the Assistant propagates: `guarded()` tells the owner."""
    snap = await snapshot(deps)
    table = domain.context_listing(REGISTRY, deps.bot, snap.values, deps.bootstrap, redact.scrubbed)
    request = AssistantRequest(
        kind="settings",
        system_prompt=prompts.render(SYSTEM_PROMPT),
        prompt=redact.scrubbed(text),
        model=snap[defaults.MODELS_SETTINGS],
        settings_version=snap.version,
        output_schema=domain.INTERPRETATION_SCHEMA,
        instructions=prompts.render(CONTEXT_PROMPT, bot=deps.bot.value, settings=table),
        prompt_version=snap.version,
    )
    reply = await deps.assistant.ask(request, timeout=snap[defaults.TIMEOUTS_SETTINGS_S])
    interpretation = domain.parse_interpretation(REGISTRY, reply.structured)
    log.info(
        "settings_interpreted",
        intent=interpretation.intent.value,
        proposals=len(interpretation.proposals),
        provider=reply.provider,
        duration_ms=reply.duration_ms,
    )
    return interpretation


async def handle_request(deps: SharedDeps, origin: OwnerOrigin, text: str, *, ask: AskOwner) -> SettingsOutcome:
    """One owner message about settings ("javoblaring qisqaroq bo'lsin", "oldingi holatiga qaytar"): interpret it,
    then change, revert, show, or answer with one clarifying question. The restated change is the reply."""
    owner = _owner_origin(origin, deps.bot)
    interpretation = await interpret_request(deps, text)
    if interpretation.intent is Intent.CHANGE:
        return await apply_proposals(deps, owner, interpretation.proposals, ask=ask)
    if interpretation.intent is Intent.REVERT:
        return await _revert(deps, owner, interpretation.keys, ask=ask)
    if interpretation.intent is Intent.SHOW:
        shown = await show_settings(deps, owner)
        return SettingsOutcome(OutcomeKind.SHOWN, shown.summary)
    snap = await snapshot(deps)
    if interpretation.intent is Intent.NEEDS_CODE:
        return await _reply(deps, owner, snap, OutcomeKind.NEEDS_CODE, S.SETTINGS_NEEDS_CODE())
    options = domain.clarify_options(REGISTRY, snap.values, interpretation.proposals)
    if interpretation.intent is Intent.CLARIFY and options:
        return await _reply(deps, owner, snap, OutcomeKind.CLARIFY, S.SETTINGS_CLARIFY(options="; ".join(options)))
    return await _reply(deps, owner, snap, OutcomeKind.UNCLEAR, S.SETTINGS_UNCLEAR())
