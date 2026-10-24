"""`BotText`: the only type the Messenger port accepts (CONVENTIONS §10).

A `BotText` is made in exactly two ways (A5): by a catalog entry in `core/strings.py` (through `render` here), or by
`core.redact.scrubbed()` for model output and transcripts. mypy then rejects a plain literal sent to the owner.
"""

import string
from collections.abc import Iterable, Mapping
from typing import NewType

BotText = NewType("BotText", str)

type TextParam = str | int

_FORMATTER = string.Formatter()


def placeholders(template: str) -> frozenset[str]:
    """The `{name}` placeholders of a catalog template. Positional, indexed or attribute fields are refused."""
    names: set[str] = set()
    for _literal, field, _spec, _conversion in _FORMATTER.parse(template):
        if field is None:
            continue
        if not field.isidentifier():
            msg = f"catalog placeholder {field!r} must be a plain name"
            raise ValueError(msg)
        names.add(field)
    return frozenset(names)


def render(template: str, params: Mapping[str, TextParam], expected: frozenset[str] | None = None) -> BotText:
    """Fill a catalog template. A missing or an unused parameter is a bug and raises `ValueError`."""
    wanted = placeholders(template) if expected is None else expected
    missing = wanted - params.keys()
    unused = params.keys() - wanted
    if missing or unused:
        msg = f"catalog params mismatch: missing={sorted(missing)} unused={sorted(unused)}"
        raise ValueError(msg)
    return BotText(template.format_map(params))


def join(parts: Iterable[BotText], separator: str = "\n") -> BotText:
    """Join texts that are already `BotText`; the separator is layout, not content."""
    return BotText(separator.join(parts))
