"""Prompt files: `prompts/<bot>/<feature>_<purpose>.md`, English Markdown with `{{slot}}` placeholders and no logic
(CONVENTIONS §5). Persona, tone and length limits are slots filled from settings.

    text = prompts.render("dev/routing_system", persona=snap[PERSONA_DEV_TONE])

`render` raises on a missing or an unused slot, so a prompt and its caller cannot drift apart.
"""

import re
from collections.abc import Mapping
from importlib import resources
from typing import Final

SLOT: Final = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
NAME: Final = re.compile(r"(?:dev|personal)/[a-z][a-z0-9_]*")


class PromptSlotError(ValueError):
    """A prompt and its caller disagree on the slots."""


def slots_of(template: str) -> frozenset[str]:
    return frozenset(SLOT.findall(template))


def fill(template: str, slots: Mapping[str, str]) -> str:
    wanted = slots_of(template)
    missing = wanted - slots.keys()
    unused = slots.keys() - wanted
    if missing or unused:
        msg = f"prompt slots mismatch: missing={sorted(missing)} unused={sorted(unused)}"
        raise PromptSlotError(msg)
    return SLOT.sub(lambda match: slots[match.group(1)], template)


def load(name: str) -> str:
    """The raw text of `prompts/<name>.md` (`name` like `dev/routing_system`)."""
    if not NAME.fullmatch(name):
        msg = f"prompt name {name!r} must look like dev/<feature>_<purpose>"
        raise ValueError(msg)
    group, _, stem = name.partition("/")
    return resources.files(__name__).joinpath(group, f"{stem}.md").read_text(encoding="utf-8")


def render(name: str, /, **slots: str) -> str:
    return fill(load(name), slots)
