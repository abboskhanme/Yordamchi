"""Settings rules, pure (SPEC §2.3, §4.3): type and check validation, scope, weakening, and resolution.

Resolution, per key in this bot's scope:
- dev: its own latest row, else the default;
- personal: its own latest row, else the dev bot's latest row for a shared `BOTH` key (read through the read-only
  bridge), else the default.
A stored value that no longer passes its declaration (a check was tightened in a newer release) is skipped, and the
next source applies; `Resolution.skipped` lists those keys so the service can log them.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from yordamchi.core.errors import SettingDevOnly, SettingInvalid, SettingPersonalOnly
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.shared.settings.registry import (
    AnySetting,
    BotScope,
    FromBootstrap,
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
        candidates: list[tuple[Source, StoredValue | None]] = [(Source.OWN, own.get(setting.key))]
        if bot is BotName.PERSONAL and setting.shared:
            candidates.append((Source.DEV, dev.get(setting.key)))
        for source, stored in candidates:
            if stored is None:
                continue
            if problem(setting, stored.value) is not None:
                skipped.append(f"{source.value}:{setting.key}")
                continue
            coerced = coerce(setting, stored.value)
            if coerced is not None:
                values[setting.key] = coerced
                sources[setting.key] = source
                break
        else:
            values[setting.key] = default_for(setting, bot, bootstrap)
            sources[setting.key] = Source.BOOTSTRAP if isinstance(setting.default, FromBootstrap) else Source.DEFAULT
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
