"""Settings rules, pure (SPEC §2.3, §3.2.15, §4.3; UC-09): validation, scope, weakening, resolution, the change plan,
the chat interpretation and the owner's listing.

Resolution, per key in this bot's scope:
- dev: its own latest row, else the default;
- personal: its own latest row, else the dev bot's latest row for a shared `BOTH` key (read through the read-only
  bridge), else the default.
A stored value that no longer passes its declaration (a check was tightened in a newer release) is skipped, and the
next source applies; `Resolution.skipped` lists those keys so the service can log them.

A change (`plan_changes`) checks every proposal before any applies: an unknown key, a key of the other bot or an
invalid value refuses the whole request. What is left either applies at once, or, if any part weakens a safeguard,
becomes one `PendingChange` the owner is asked about once. The model's answer (`parse_interpretation`) only ever names
registry keys; anything else reads as `UNKNOWN`.
"""

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

from yordamchi.core import json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import SettingDevOnly, SettingInvalid, SettingPersonalOnly, SettingUnknown
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText, join
from yordamchi.shared.settings.registry import (
    KEY_MAX_LEN,
    AnySetting,
    BotScope,
    FromBootstrap,
    Group,
    PerBot,
    Registry,
    Setting,
    SettingValue,
    value_kind,
)


class Source(StrEnum):
    OWN = "own"  # this bot's latest row
    DEV = "dev"  # the dev bot's row of a shared key (personal bot only)
    DEFAULT = "default"
    BOOTSTRAP = "bootstrap"  # FROM_BOOTSTRAP: the env file of this process


@dataclass(frozen=True, slots=True)
class StoredValue:
    """The latest `setting_versions` row of one key, as the resolver needs it."""

    key: str
    value: JsonValue
    row_id: int


def in_scope(setting: AnySetting, bot: BotName) -> bool:
    return setting.bots is BotScope.BOTH or setting.bots.value == bot.value


def require_writable_by(setting: AnySetting, bot: BotName) -> None:
    """The one-writer-per-DB rule: a bot changes only keys in its own scope (SPEC §2.3)."""
    if in_scope(setting, bot):
        return
    if setting.bots is BotScope.PERSONAL:
        raise SettingPersonalOnly(key=setting.key)
    raise SettingDevOnly(key=setting.key)


def _as_bool(raw: JsonValue) -> SettingValue | None:
    return raw if isinstance(raw, bool) else None


def _as_int(raw: JsonValue) -> SettingValue | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _as_float(raw: JsonValue) -> SettingValue | None:
    # JSON has one number type: an int is a valid float setting value.
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _as_str(raw: JsonValue) -> SettingValue | None:
    return raw if isinstance(raw, str) else None


def _as_list(raw: JsonValue) -> SettingValue | None:
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return cast(list[str], list(raw))
    return None


def _as_dict(raw: JsonValue) -> SettingValue | None:
    if isinstance(raw, dict) and all(isinstance(item, str) for item in raw.values()):
        return cast(dict[str, str], dict(raw))
    return None


_COERCE: Final = {
    "bool": _as_bool,
    "int": _as_int,
    "float": _as_float,
    "str": _as_str,
    "list": _as_list,
    "dict": _as_dict,
}


def coerce(setting: AnySetting, raw: JsonValue) -> SettingValue | None:
    """`raw` as the declared JSON type, or `None` if it is not of that type."""
    convert = _COERCE.get(value_kind(setting.type) or "")
    return convert(raw) if convert is not None else None


def problem(setting: AnySetting, raw: JsonValue) -> str | None:
    """`None` if `raw` is a valid value for `setting`, else a snake_case reason."""
    value = coerce(setting, raw)
    if value is None:
        return "wrong_type"
    if setting.check is None:
        return None
    return setting.check.problem(value)


def validate(setting: AnySetting, raw: JsonValue) -> SettingValue:
    """The coerced value, or `SettingInvalid` naming what is allowed."""
    reason = problem(setting, raw)
    value = coerce(setting, raw)
    if reason is not None or value is None:
        options = setting.check.options() if setting.check is not None else value_kind(setting.type) or "?"
        raise SettingInvalid(key=setting.key, reason=reason or "wrong_type", options=options)
    return value


def default_for(setting: AnySetting, bot: BotName, bootstrap: Mapping[str, SettingValue]) -> SettingValue:
    default: object = setting.default
    if isinstance(default, PerBot):
        return cast(SettingValue, default.dev if bot is BotName.DEV else default.personal)
    if isinstance(default, FromBootstrap):
        if setting.key not in bootstrap:
            msg = f"bootstrap value for {setting.key!r} is missing"
            raise LookupError(msg)
        return bootstrap[setting.key]
    return cast(SettingValue, default)


def weakens(setting: AnySetting, old: SettingValue, new: SettingValue) -> bool:
    """True when changing `old` → `new` loosens a safeguard and must ask the owner once (UC-09)."""
    return old != new and setting.weakens is not None and setting.weakens(old, new)


@dataclass(frozen=True, slots=True)
class Resolution:
    values: Mapping[str, SettingValue]
    sources: Mapping[str, Source]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Resolved:
    """One key's effective value; `skipped` names the stored rows that failed today's declaration."""

    value: SettingValue
    source: Source
    skipped: tuple[str, ...] = ()


def resolve_one(
    setting: AnySetting,
    bot: BotName,
    *,
    own: StoredValue | None,
    dev: StoredValue | None,
    bootstrap: Mapping[str, SettingValue],
) -> Resolved:
    """The value of one setting in `bot`'s scope, by the module docstring's order. `dev` counts for the personal
    bot's shared keys only."""
    candidates: list[tuple[Source, StoredValue | None]] = [(Source.OWN, own)]
    if bot is BotName.PERSONAL and setting.shared:
        candidates.append((Source.DEV, dev))
    skipped: list[str] = []
    for source, stored in candidates:
        if stored is None:
            continue
        if problem(setting, stored.value) is not None:
            skipped.append(f"{source.value}:{setting.key}")
            continue
        coerced = coerce(setting, stored.value)
        if coerced is not None:
            return Resolved(coerced, source, tuple(skipped))
    fallback = Source.BOOTSTRAP if isinstance(setting.default, FromBootstrap) else Source.DEFAULT
    return Resolved(default_for(setting, bot, bootstrap), fallback, tuple(skipped))


def resolve(
    registry: Registry,
    bot: BotName,
    *,
    own: Mapping[str, StoredValue],
    dev: Mapping[str, StoredValue],
    bootstrap: Mapping[str, SettingValue],
) -> Resolution:
    """Effective value and source of every setting in `bot`'s scope. `dev` is ignored for the dev bot."""
    values: dict[str, SettingValue] = {}
    sources: dict[str, Source] = {}
    skipped: list[str] = []
    for setting in registry:
        if not in_scope(setting, bot):
            continue
        one = resolve_one(setting, bot, own=own.get(setting.key), dev=dev.get(setting.key), bootstrap=bootstrap)
        values[setting.key] = one.value
        sources[setting.key] = one.source
        skipped.extend(one.skipped)
    return Resolution(values=values, sources=sources, skipped=tuple(skipped))


def shared_keys(registry: Registry) -> tuple[str, ...]:
    """Keys the personal bot reads from the dev DB when it has no row of its own."""
    return tuple(setting.key for setting in registry if setting.shared)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The settings of one bot at one moment: read once per turn or job, never cached (CONVENTIONS §5).

    `snap[SETTING]` is typed by the declaration. `version` increases whenever any row this bot reads is added:
    for the dev bot it is the max `setting_versions.id`; for the personal bot it is its own max id plus the dev
    DB's max id, both monotonic, so the sum is too. Sessions compare it to detect a changed prompt (SPEC §5.4).
    """

    bot: BotName
    version: int
    values: Mapping[str, SettingValue]
    sources: Mapping[str, Source]

    def __getitem__[T: SettingValue](self, setting: Setting[T]) -> T:
        try:
            return cast(T, self.values[setting.key])
        except KeyError:
            msg = f"setting {setting.key!r} is not in the {self.bot} bot's scope"
            raise KeyError(msg) from None

    def source(self, setting: AnySetting) -> Source:
        return self.sources[setting.key]

    def keys(self) -> Iterable[str]:
        return self.values.keys()


def json_of(value: SettingValue) -> JsonValue:
    """A setting value as the JSON it is stored as (a list or dict is copied, so the snapshot stays untouched)."""
    if isinstance(value, list):
        items: list[JsonValue] = [item for item in value]  # noqa: C416  # widens list[str] to list[JsonValue]
        return items
    if isinstance(value, dict):
        entries: dict[str, JsonValue] = {key: item for key, item in value.items()}  # noqa: C416
        return entries
    return value


def value_before(
    setting: AnySetting,
    bot: BotName,
    *,
    history: Sequence[StoredValue],
    dev: StoredValue | None,
    bootstrap: Mapping[str, SettingValue],
) -> SettingValue:
    """What a revert restores (UC-09): the value that applied before this bot's newest row of `setting`.

    `history` is this bot's rows of the key, newest first. Before the first row it is the dev row (a personal bot's
    shared key) or the default.
    """
    previous = history[1] if len(history) > 1 else None
    return resolve_one(setting, bot, own=previous, dev=dev, bootstrap=bootstrap).value


# --- how a value reads to the owner ---------------------------------------------------------------------------------
CHANGE_VALUE_CHARS: Final = 80  # of each value in a restated change or a clarifying question
SUMMARY_VALUE_CHARS: Final = 60  # of each changed value in the grouped summary of "sozlamalaring qanday?"


def _number_text(number: int | float) -> str:  # an int prints as itself, a whole float without ".0"
    if isinstance(number, float):
        return str(int(number)) if number.is_integer() else repr(number)
    return str(number)


def _cut(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def show_value(
    setting: AnySetting,
    value: SettingValue,
    *,
    limit: int | None = None,
    scrub: Callable[[str], str] = redact.scrubbed,
) -> str:
    """One line, in the owner's words: `yoqilgan`, `10 s`, `bo'sh`, `a, b`, `hr → BonviHR`.

    Scrubbed before it is cut: a key cut in half no longer matches its pattern and would leak its first part.
    """
    return _cut(scrub(_plain_value(setting, value)), limit)


def _plain_value(setting: AnySetting, value: SettingValue) -> str:
    text: str
    if isinstance(value, bool):
        text = S.SETTING_ON() if value else S.SETTING_OFF()
    elif isinstance(value, (int, float)):
        unit = setting.display_unit
        text = _number_text(value) + (f" {unit}" if unit else "")
    elif isinstance(value, str):
        text = " ".join(value.split()) or S.SETTING_EMPTY()
    elif isinstance(value, list):
        text = ", ".join(value) or S.SETTING_EMPTY()
    else:
        text = ", ".join(f"{key} → {item}" for key, item in value.items()) or S.SETTING_EMPTY()
    return text


# --- a change (SPEC §3.2.15 steps 2–4; UC-09) -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Proposal:
    """One `(key, value)` the owner asked for: interpreted from chat, or built by a feature (aliases, terms)."""

    key: str
    value: JsonValue


@dataclass(frozen=True, slots=True)
class PlannedChange:
    setting: AnySetting
    old: SettingValue
    new: SettingValue
    weakens: bool


def plan_changes(
    registry: Registry, bot: BotName, current: Mapping[str, SettingValue], proposals: Iterable[Proposal]
) -> tuple[PlannedChange, ...]:
    """Every proposal is checked before any applies, so one refusal changes nothing (UC-09 F).

    Raises `SettingUnknown`, `SettingPersonalOnly` / `SettingDevOnly` (SPEC §2.3: "buni shaxsiy botga ayting") or
    `SettingInvalid` naming what is allowed. A key proposed twice keeps its last value; a value equal to the current
    one drops out. `current` is this bot's snapshot.
    """
    wanted: dict[str, tuple[AnySetting, SettingValue]] = {}
    for proposal in proposals:
        setting = registry.get(proposal.key)
        if setting is None:
            raise SettingUnknown(key=proposal.key[:KEY_MAX_LEN])
        require_writable_by(setting, bot)
        wanted[setting.key] = (setting, validate(setting, proposal.value))
    planned: list[PlannedChange] = []
    for key, (setting, new) in wanted.items():
        old = current[key]
        if new != old:
            planned.append(PlannedChange(setting=setting, old=old, new=new, weakens=weakens(setting, old, new)))
    return tuple(planned)


def change_line(setting: AnySetting, old: SettingValue, new: SettingValue) -> str:
    return S.SETTING_CHANGE_LINE(
        key=setting.key,
        old=show_value(setting, old, limit=CHANGE_VALUE_CHARS),
        new=show_value(setting, new, limit=CHANGE_VALUE_CHARS),
    )


def restate(changes: Iterable[PlannedChange]) -> str:
    """The one line the owner reads before a change applies or is asked about: `key: old → new; …`."""
    return "; ".join(change_line(change.setting, change.old, change.new) for change in changes)


type ChangeReason = Literal["change", "revert"]
_REASONS: Final[Mapping[str, ChangeReason]] = {"change": "change", "revert": "revert"}


@dataclass(frozen=True, slots=True)
class ChangeItem:
    key: str
    value: JsonValue
    old: JsonValue


@dataclass(frozen=True, slots=True)
class PendingChange:
    """A change set that weakens a safeguard, held until the owner's "ha" (UC-09 F; SPEC §3.2.15 step 3).

    The asker stores `to_json()` with its one question (a `settings_weaken` decision on the dev bot) and the approval
    rebuilds it with `from_json`. `action_sha256` names the exact change, so asking it again reuses the question.
    Its values are those of safeguards: ids, class names, hosts, numbers, model names.
    """

    reason: ChangeReason
    items: tuple[ChangeItem, ...]
    restated: str
    origin_update_id: int

    def to_json(self) -> JsonObject:
        return {
            "reason": self.reason,
            "items": [{"key": item.key, "value": item.value, "old": item.old} for item in self.items],
            "restated": self.restated,
            "origin_update_id": self.origin_update_id,
        }

    @classmethod
    def from_json(cls, raw: JsonValue) -> "PendingChange | None":
        """None for anything `to_json` did not write."""
        if not isinstance(raw, dict):
            return None
        reason, items, restated = raw.get("reason"), raw.get("items"), raw.get("restated")
        update_id = raw.get("origin_update_id")
        if not (isinstance(reason, str) and reason in _REASONS and isinstance(items, list) and items):
            return None
        if not isinstance(restated, str) or not isinstance(update_id, int) or isinstance(update_id, bool):
            return None
        parsed: list[ChangeItem] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(key := item.get("key"), str) or "value" not in item:
                return None
            parsed.append(ChangeItem(key=key, value=item["value"], old=item.get("old")))
        return cls(reason=_REASONS[reason], items=tuple(parsed), restated=restated, origin_update_id=update_id)

    @property
    def action_sha256(self) -> str:
        material: JsonValue = {"reason": self.reason, "items": [[item.key, item.value] for item in self.items]}
        return hashlib.sha256(json_value.dumps(material).encode()).hexdigest()

    def proposals(self) -> tuple[Proposal, ...]:
        return tuple(Proposal(key=item.key, value=item.value) for item in self.items)


def pending_change(
    planned: Sequence[PlannedChange], *, reason: ChangeReason, restated: str, origin_update_id: int
) -> PendingChange:
    items = tuple(
        ChangeItem(key=change.setting.key, value=json_of(change.new), old=json_of(change.old)) for change in planned
    )
    return PendingChange(reason=reason, items=items, restated=restated, origin_update_id=origin_update_id)


# --- the chat interpretation (SPEC §3.2.15 step 1; the `settings` Assistant) ---------------------------------------
class Intent(StrEnum):
    CHANGE = "change"
    REVERT = "revert"
    CLARIFY = "clarify"  # several settings fit: ask one question naming them
    SHOW = "show"
    NEEDS_CODE = "needs_code"  # not a setting: offer a self-update (UC-19)
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Interpretation:
    intent: Intent
    proposals: tuple[Proposal, ...] = ()  # CHANGE: what to apply; CLARIFY: the candidates
    keys: tuple[str, ...] = ()  # REVERT: the named keys, () = the last change


MAX_PROPOSALS: Final = 10
_JSON_VALUE_TYPES: Final[list[JsonValue]] = ["boolean", "integer", "number", "string", "array", "object"]
INTERPRETATION_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": [intent.value for intent in Intent]},
        "changes": {
            "type": "array",
            "maxItems": MAX_PROPOSALS,
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": _JSON_VALUE_TYPES}},
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
        "keys": {"type": "array", "maxItems": MAX_PROPOSALS, "items": {"type": "string"}},
    },
    "required": ["outcome", "changes", "keys"],
    "additionalProperties": False,
}


def normalize(setting: AnySetting, raw: JsonValue) -> JsonValue:
    """A model's value in the declared JSON type where that is unambiguous: `"15"` → 15, `"false"` → False."""
    if not isinstance(raw, str):
        return raw
    kind, text = value_kind(setting.type), raw.strip()
    if kind in {"int", "float"}:
        try:
            number = float(text)
        except ValueError:
            return raw
        return int(number) if kind == "int" and number.is_integer() else number
    if kind == "bool" and text.casefold() in {"true", "false"}:
        return text.casefold() == "true"
    return raw


def _proposals(registry: Registry, raw: JsonValue, *, strict: bool) -> tuple[Proposal, ...] | None:
    """The `changes` of an answer. A malformed item or an unknown key → None when `strict`, else it is dropped."""
    if not isinstance(raw, list):
        return None if strict else ()
    found: list[Proposal] = []
    for item in raw[:MAX_PROPOSALS]:
        key = item.get("key") if isinstance(item, dict) else None
        setting = registry.get(key) if isinstance(key, str) else None
        if setting is None or not isinstance(item, dict) or "value" not in item:
            if strict:
                return None
            continue
        found.append(Proposal(key=setting.key, value=normalize(setting, item["value"])))
    return tuple(found)


def _revert_keys(registry: Registry, raw: JsonValue) -> tuple[str, ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(key, str) and key in registry for key in raw):
        return None
    return tuple(dict.fromkeys(str(key) for key in raw))[:MAX_PROPOSALS]


def parse_interpretation(registry: Registry, raw: JsonValue) -> Interpretation:
    """The structured answer as an `Interpretation`. Anything that names a key the registry lacks, or is not of
    `INTERPRETATION_SCHEMA`, is `UNKNOWN`: a guess never reaches a write."""
    unknown = Interpretation(Intent.UNKNOWN)
    if not isinstance(raw, dict):
        return unknown
    try:
        intent = Intent(str(raw.get("outcome")))
    except ValueError:
        return unknown
    if intent is Intent.CHANGE:
        proposals = _proposals(registry, raw.get("changes"), strict=True)
        return Interpretation(intent, proposals=proposals) if proposals else unknown
    if intent is Intent.CLARIFY:
        candidates = _proposals(registry, raw.get("changes"), strict=False)
        return Interpretation(intent, proposals=candidates) if candidates else unknown
    if intent is Intent.REVERT:
        keys = _revert_keys(registry, raw.get("keys"))
        return unknown if keys is None else Interpretation(intent, keys=keys)
    return Interpretation(intent)


def clarify_options(
    registry: Registry, current: Mapping[str, SettingValue], candidates: Iterable[Proposal]
) -> tuple[str, ...]:
    """The candidates of a clarifying question as numbered change lines. One outside this bot's scope, invalid, or
    equal to the current value drops out."""
    lines: list[str] = []
    for candidate in candidates:
        setting = registry.get(candidate.key)
        if setting is None or setting.key not in current or problem(setting, candidate.value) is not None:
            continue
        new = coerce(setting, candidate.value)
        if new is not None and new != current[setting.key]:
            line = change_line(setting, current[setting.key], new)
            lines.append(S.SETTINGS_CLARIFY_OPTION(number=len(lines) + 1, change=line))
    return tuple(lines)


def _sorted(settings: Iterable[AnySetting]) -> list[AnySetting]:
    order = {group: index for index, group in enumerate(Group)}
    return sorted(settings, key=lambda setting: (order[setting.group], setting.key))


def _default_json(setting: AnySetting, bot: BotName, bootstrap: Mapping[str, SettingValue]) -> str:
    if isinstance(setting.default, FromBootstrap) and setting.key not in bootstrap:
        return "-"  # the other bot's env-file value
    return json_value.dumps(json_of(default_for(setting, bot, bootstrap)))


def context_listing(
    registry: Registry,
    bot: BotName,
    values: Mapping[str, SettingValue],
    bootstrap: Mapping[str, SettingValue],
    scrub: Callable[[str], str],
) -> str:
    """The `settings` slot of `prompts/dev/settings_context.md`: one row per declared setting, in the column order
    that prompt names. Keys outside this bot's scope are listed too, with no current value, so a request for one is
    answered with the right bot's name instead of a guess."""
    rows: list[str] = []
    for setting in _sorted(registry):
        current = json_value.dumps(json_of(values[setting.key])) if setting.key in values else "-"
        allowed = setting.check.options() if setting.check is not None else value_kind(setting.type) or "?"
        cells = (
            setting.key,
            value_kind(setting.type) or "?",
            setting.bots.value,
            current,
            _default_json(setting, bot, bootstrap),
            allowed,
            setting.doc,
        )
        rows.append("| " + " | ".join(cells) + " |")
    return scrub("\n".join(rows))


# --- "sozlamalaring qanday?" (UC-09: ≤ 3 s, every group, the stored values, secrets masked) ------------------------
@dataclass(frozen=True, slots=True)
class SettingsListing:
    """The grouped summary, sent as a message, and the full list, sent as a `.md` file (tests/golden/settings/)."""

    summary: BotText
    full: BotText
    total: int
    changed: int
    groups: tuple[Group, ...]


def listing(
    registry: Registry,
    bot: BotName,
    values: Mapping[str, SettingValue],
    default_values: Mapping[str, SettingValue],
    scrub: Callable[[str], str],
) -> SettingsListing:
    """Every setting in `values` (this bot's snapshot), by group. A value that differs from its default reads
    "o'zgartirilgan"; every value passes `scrub`, so a key pasted into a text setting is masked."""
    by_group: dict[Group, list[AnySetting]] = {}
    for setting in _sorted(registry):
        if setting.key in values:
            by_group.setdefault(setting.group, []).append(setting)
    changed = {key for key, value in values.items() if key in default_values and value != default_values[key]}
    bot_label = S.SETTINGS_BOTS[bot.value]()
    summary = [S.SETTINGS_LIST_HEAD(bot=bot_label, total=len(values), groups=len(by_group), changed=len(changed))]
    blocks = [S.SETTINGS_FILE_HEAD(bot=bot_label, total=len(values))]
    for group, settings in by_group.items():
        label = S.SETTINGS_GROUPS[group.value]()
        items = [
            S.SETTINGS_LIST_ITEM(
                key=setting.key, value=show_value(setting, values[setting.key], limit=SUMMARY_VALUE_CHARS, scrub=scrub)
            )
            for setting in settings
            if setting.key in changed
        ]
        shown = "; ".join(items) if items else S.SETTINGS_LIST_DEFAULTS()
        summary.append(S.SETTINGS_LIST_GROUP(group=label, count=len(settings), items=shown))
        lines = [_file_item(setting, values[setting.key], default_values, changed, scrub) for setting in settings]
        blocks.append(join([S.SETTINGS_FILE_GROUP(group=label), *lines]))
    summary.append(S.SETTINGS_LIST_FOOT())
    return SettingsListing(
        summary=join(summary),
        full=join(blocks, "\n\n"),
        total=len(values),
        changed=len(changed),
        groups=tuple(by_group),
    )


def _file_item(
    setting: AnySetting,
    value: SettingValue,
    default_values: Mapping[str, SettingValue],
    changed: set[str],
    scrub: Callable[[str], str],
) -> BotText:
    options = S.SETTINGS_FILE_OPTIONS(options=setting.check.options()) if setting.check is not None else ""
    shown = show_value(setting, value, scrub=scrub)
    if setting.key in changed:
        default = show_value(setting, default_values[setting.key], scrub=scrub)
        return S.SETTINGS_FILE_ITEM_CHANGED(key=setting.key, value=shown, default=default, options=options)
    return S.SETTINGS_FILE_ITEM(key=setting.key, value=shown, options=options)
