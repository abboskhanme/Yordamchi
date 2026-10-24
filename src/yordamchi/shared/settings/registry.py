"""The settings mechanism: how a behaviour value is declared (SPEC §4.3; CONVENTIONS §5; NFR-OPS-03).

    BURST_AFTER_MEDIA_S = Setting("burst.after_media_s", float, 10.0, check=Range(2, 60), group=Group.BURST,
                                  bots=BOTH, doc="Quiet window after a bare photo, file or link (UC-02).")

A new setting is one declaration in `defaults.py`, never a change here. Values are JSON only
(`bool int float str list[str] dict[str, str]`; an enum is `str` + `OneOf`).

- `bots`: `DEV`, `PERSONAL` or `BOTH`. A `BOTH` key is shared: the personal bot falls back to the dev bot's value
  (SPEC §2.3). A key whose default differs per process (`PerBot`, `FROM_BOOTSTRAP`) is never shared that way: each
  bot keeps its own value, because its default already says the bots differ.
- `check`: `Range`, `OneOf`, `SubsetOf`, `MaxLen`, `Regex`, `HostList`, `TimeOfDay`, `Duration`, `DictOf`.
- `weakens(old, new) -> bool`: set on every safeguard; a weakening change asks the owner once (UC-09).

This module is pure (C6 lists it with the domain modules).
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from typing import Final, Protocol, get_args, get_origin

type SettingValue = bool | int | float | str | list[str] | dict[str, str]
type Weakens = Callable[[SettingValue, SettingValue], bool]


KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+")
KEY_MAX_LEN: Final = 96  # setting_versions.key is String(96)


class BotScope(StrEnum):
    DEV = "dev"
    PERSONAL = "personal"
    BOTH = "both"


DEV: Final = BotScope.DEV
PERSONAL: Final = BotScope.PERSONAL
BOTH: Final = BotScope.BOTH


class Group(StrEnum):
    """The groups of SPEC §4.3 (UC-09 lists them); `ACCESS` holds the bootstrap-backed ids."""

    ACCESS = "access"
    PERSONA = "persona"
    PROMPTS = "prompts"
    VOICE = "voice"
    SPEECH = "speech"
    LANGUAGE = "language"
    MODELS = "models"
    ROUTING = "routing"  # "ROUTING / PROJECTS" in SPEC §4.3
    DECISIONS = "decisions"
    BURST = "burst"
    TIMEOUTS = "timeouts"
    SCHEDULES = "schedules"
    CAPS = "caps"
    LIMITS = "limits"
    SERVERS = "servers"


class FromBootstrap:
    """Default taken from the process's env file (owner and chat ids, SPEC §4.3)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "FROM_BOOTSTRAP"


FROM_BOOTSTRAP: Final = FromBootstrap()


@dataclass(frozen=True, slots=True)
class PerBot[T: SettingValue]:
    """A default that differs per bot, e.g. `voice.enabled` (dev False, personal True)."""

    dev: T
    personal: T


# --- checks --------------------------------------------------------------------------------------------------------
class Check(Protocol):
    def problem(self, value: SettingValue) -> str | None:
        """`None` if the value passes, else a snake_case reason (for code and logs)."""
        ...

    def options(self) -> str:
        """What is allowed, shown to the owner after a refusal (`S.SETTING_INVALID`)."""
        ...


def _number(value: SettingValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _strings(value: SettingValue) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value)
    return None


@dataclass(frozen=True, slots=True)
class Range:
    lo: float
    hi: float

    def problem(self, value: SettingValue) -> str | None:
        number = _number(value)
        if number is None:
            return "not_a_number"
        return None if self.lo <= number <= self.hi else "out_of_range"

    def options(self) -> str:
        return f"{_fmt(self.lo)}–{_fmt(self.hi)}"


class OneOf:
    __slots__ = ("values",)

    def __init__(self, *values: str) -> None:
        self.values: tuple[str, ...] = values

    def problem(self, value: SettingValue) -> str | None:
        return None if isinstance(value, str) and value in self.values else "not_an_option"

    def options(self) -> str:
        return ", ".join(self.values)


class SubsetOf:
    __slots__ = ("values",)

    def __init__(self, values: Iterable[str]) -> None:
        self.values: tuple[str, ...] = tuple(values)

    def problem(self, value: SettingValue) -> str | None:
        if not isinstance(value, list):
            return "not_a_list"
        if len(set(value)) != len(value):
            return "duplicate_item"
        return None if set(value) <= set(self.values) else "not_an_option"

    def options(self) -> str:
        return ", ".join(self.values)


@dataclass(frozen=True, slots=True)
class MaxLen:
    """Characters for a string; items for a list or a dict."""

    limit: int

    def problem(self, value: SettingValue) -> str | None:
        if isinstance(value, (str, list, dict)):
            return None if len(value) <= self.limit else "too_long"
        return "not_text"

    def options(self) -> str:
        return f"≤ {self.limit}"


class Regex:
    """Full match on a string, on each list item, or on each dict key."""

    __slots__ = ("description", "pattern")

    def __init__(self, pattern: str, *, description: str) -> None:
        self.pattern = re.compile(pattern)
        self.description = description

    def problem(self, value: SettingValue) -> str | None:
        items = _strings(value)
        if items is None:
            return "not_text"
        return None if all(self.pattern.fullmatch(item) for item in items) else "bad_format"

    def options(self) -> str:
        return self.description


_HOST: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?")


@dataclass(frozen=True, slots=True)
class HostList:
    """Host names or `~/.ssh/config` aliases: no scheme, user, port or path."""

    def problem(self, value: SettingValue) -> str | None:
        if not isinstance(value, list):
            return "not_a_list"
        if len(set(value)) != len(value):
            return "duplicate_item"
        return None if all(_HOST.fullmatch(host) for host in value) else "bad_host"

    def options(self) -> str:
        return "host nomlari (masalan: server, bonvi-srv)"


_HHMM: Final = r"(?:[01]\d|2[0-3]):[0-5]\d"


@dataclass(frozen=True, slots=True)
class TimeOfDay:
    """`HH:MM` (24 h), or `""` (off) when `allow_empty`."""

    allow_empty: bool = False

    def problem(self, value: SettingValue) -> str | None:
        if not isinstance(value, str):
            return "not_text"
        if value == "" and self.allow_empty:
            return None
        return None if re.fullmatch(_HHMM, value) else "bad_time"

    def options(self) -> str:
        return "HH:MM" + (" yoki bo'sh (o'chiq)" if self.allow_empty else "")


@dataclass(frozen=True, slots=True)
class Duration:
    """A daily span `HH:MM-HH:MM` (may cross midnight, e.g. quiet hours `23:00-07:00`), or `""` when `allow_empty`.

    SPEC §4.3 names this check without defining it; its only user is `schedule.quiet_hours` (ASSUMPTIONS).
    """

    allow_empty: bool = True

    def problem(self, value: SettingValue) -> str | None:
        if not isinstance(value, str):
            return "not_text"
        if value == "" and self.allow_empty:
            return None
        match = re.fullmatch(rf"({_HHMM})-({_HHMM})", value)
        if match is None:
            return "bad_span"
        return None if match.group(1) != match.group(2) else "empty_span"

    def options(self) -> str:
        return "HH:MM-HH:MM" + (" yoki bo'sh (o'chiq)" if self.allow_empty else "")


class DictOf:
    """A `dict[str, str]` whose keys come from `keys` and whose values from `values`; a key may be left out.

    SPEC §4.3 lists no check for dict values; `assistant.warm_clients` (TASKS T12) needs one, so `"9"` warm clients is
    refused at the change instead of being clamped by the reader.
    """

    __slots__ = ("keys", "values")

    def __init__(self, keys: Iterable[str], values: Iterable[str]) -> None:
        self.keys: tuple[str, ...] = tuple(keys)
        self.values: tuple[str, ...] = tuple(values)

    def problem(self, value: SettingValue) -> str | None:
        if not isinstance(value, dict):
            return "not_a_dict"
        if not set(value) <= set(self.keys):
            return "unknown_key"
        return None if all(item in self.values for item in value.values()) else "not_an_option"

    def options(self) -> str:
        return f"{', '.join(self.keys)} → {' | '.join(self.values)}"


# --- weakens: (old, new) -> True when the change removes or loosens a safeguard ------------------------------------
def always(old: SettingValue, new: SettingValue) -> bool:
    """Any change weakens (owner and group ids)."""
    return old != new


def increases(old: SettingValue, new: SettingValue) -> bool:
    """Raising a cap or a limit weakens it."""
    before, after = _number(old), _number(new)
    return before is not None and after is not None and after > before


def decreases(old: SettingValue, new: SettingValue) -> bool:
    before, after = _number(old), _number(new)
    return before is not None and after is not None and after < before


def disables(old: SettingValue, new: SettingValue) -> bool:
    return old is True and new is False


def removes_items(old: SettingValue, new: SettingValue) -> bool:
    """Dropping an entry from a guarded list (an ask class, a jump-only host)."""
    return isinstance(old, list) and isinstance(new, list) and bool(set(old) - set(new))


def adds_items(old: SettingValue, new: SettingValue) -> bool:
    """Adding an entry to an allow-list (a read-only remote command, an HTTP write host). SPEC: "always on add"."""
    return isinstance(old, list) and isinstance(new, list) and bool(set(new) - set(old))


class to_values:  # noqa: N801  # reads as the SPEC writes it: weakens=to_values({"fable", "best"})
    """Changing *to* one of these values weakens (a model that bills usage credits, UC-13 #3)."""

    __slots__ = ("values",)

    def __init__(self, values: Iterable[str]) -> None:
        self.values = frozenset(values)

    def __call__(self, old: SettingValue, new: SettingValue) -> bool:
        return isinstance(new, str) and new in self.values and old != new

    def __repr__(self) -> str:
        return f"to_values({sorted(self.values)})"


# --- the declaration -----------------------------------------------------------------------------------------------
_UNIT_SUFFIXES: Final = (
    ("_s", "s"),
    ("_min", "min"),
    ("_h", "h"),
    ("_days", "days"),
    ("_usd", "USD"),
    ("_pct", "%"),
    ("_gb", "GB"),
)


@dataclass(frozen=True, slots=True)
class Setting[T: SettingValue]:
    key: str
    type: type[T]
    default: T | PerBot[T] | FromBootstrap
    _: KW_ONLY
    group: Group
    bots: BotScope
    doc: str
    check: Check | None = None
    weakens: Weakens | None = None
    unit: str = ""

    def __post_init__(self) -> None:
        if len(self.key) > KEY_MAX_LEN or not KEY_PATTERN.fullmatch(self.key):
            msg = f"setting key {self.key!r} must be <group>.<name>, lowercase, ≤ {KEY_MAX_LEN} chars"
            raise ValueError(msg)
        if value_kind(self.type) is None:
            msg = f"setting {self.key!r} has a non-JSON type {self.type!r}"
            raise TypeError(msg)
        if not self.doc:
            msg = f"setting {self.key!r} needs a doc line"
            raise ValueError(msg)

    @property
    def per_bot(self) -> bool:
        """True when each bot keeps its own value (its default comes from the process)."""
        return isinstance(self.default, (PerBot, FromBootstrap))

    @property
    def shared(self) -> bool:
        """True when the personal bot falls back to the dev bot's value (SPEC §2.3)."""
        return self.bots is BotScope.BOTH and not self.per_bot

    @property
    def display_unit(self) -> str:
        if self.unit:
            return self.unit
        return next((unit for suffix, unit in _UNIT_SUFFIXES if self.key.endswith(suffix)), "")


def value_kind(declared: object) -> str | None:
    """`bool int float str list dict` for the six JSON setting types, else `None`."""
    for kind in (bool, int, float, str):
        if declared is kind:
            return kind.__name__
    origin, args = get_origin(declared), get_args(declared)
    if origin is list and args == (str,):
        return "list"
    if origin is dict and args == (str, str):
        return "dict"
    return None


# A Setting of any value type; covariance does not hold for a frozen generic dataclass in mypy, so list them.
type AnySetting = (
    Setting[bool] | Setting[int] | Setting[float] | Setting[str] | Setting[list[str]] | Setting[dict[str, str]]
)


class Registry:
    """Every declared setting, by key. Built once from `defaults.py`."""

    __slots__ = ("_by_key",)

    def __init__(self, settings: Iterable[AnySetting]) -> None:
        by_key: dict[str, AnySetting] = {}
        for setting in settings:
            if setting.key in by_key:
                msg = f"setting {setting.key!r} is declared twice"
                raise ValueError(msg)
            by_key[setting.key] = setting
        self._by_key = by_key

    @classmethod
    def from_namespace(cls, namespace: Mapping[str, object]) -> "Registry":
        """Collect every `Setting` bound in a module namespace (`Registry.from_namespace(globals())`)."""
        return cls(value for value in namespace.values() if isinstance(value, Setting))

    def __getitem__(self, key: str) -> AnySetting:
        return self._by_key[key]

    def get(self, key: str) -> AnySetting | None:
        return self._by_key.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def __iter__(self) -> Iterator[AnySetting]:
        return iter(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)
