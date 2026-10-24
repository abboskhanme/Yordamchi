"""The JSON value type shared by settings, ports and persistence, and its one canonical encoding.

Settings values, `*_json` columns and tool inputs are JSON. Encoding is compact, key-sorted and keeps non-ASCII text
as-is (Uzbek), so equal values always produce equal strings.
"""

import json
from collections.abc import Callable

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def dumps(value: JsonValue) -> str:
    """Canonical JSON text for storage: compact, sorted keys, UTF-8 kept, NaN refused."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(text: str) -> JsonValue:
    """Parse JSON text produced by `dumps` (or any JSON). Raises `ValueError` on invalid input."""
    value: JsonValue = json.loads(text)
    return value


def map_strings(value: JsonValue, fn: Callable[[str], str]) -> JsonValue:
    """`value` with `fn` applied to every string, dict keys included, at any depth.

    Used with `core.redact.scrubbed`: every audit `data_json`, inbox and outbox payload passes it (SPEC §4.5).
    """
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [map_strings(item, fn) for item in value]
    if isinstance(value, dict):
        return {fn(key): map_strings(item, fn) for key, item in value.items()}
    return value
