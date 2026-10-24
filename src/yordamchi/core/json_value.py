"""The JSON value type shared by settings, ports and persistence, and its one canonical encoding.

Settings values, `*_json` columns and tool inputs are JSON. Encoding is compact, key-sorted and keeps non-ASCII text
as-is (Uzbek), so equal values always produce equal strings.
"""

import json

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
