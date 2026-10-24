"""Chat, pure: the dev group's plain answers (owner, 2026-10-24; SPEC §3.2.6 `chat`).

"guruhda ham … men bilan sodda yordamchi bot kabi gaplashishi kerak, e'tibori faqat projectlar bo'lmasligi kerak".
Routing marks a message about none of his projects `chat` (small talk, a general or tech question, advice,
translation, arithmetic: `routing.domain.Intent.CHAT`), and `dev/chat` answers it with the warm `chat` Assistant: no
tools, no project data, no turn, no report.

- The ask is one JSON object (`chat_prompt`): the owner's words in order, a voice transcript and what he forwarded
  flagged as such, and his local time. Every string is scrubbed first (NFR-SEC-03).
- The frozen system prompt holds the fixed parts only; persona and reply language travel with every ask in the
  instructions block (`context_slots`, SPEC §5.4, spike S4).
- The reply is the model's text, scrubbed and cut to one Telegram message (`answer_text`). No usable reply (a timeout,
  Claude unavailable, an empty answer) → the service sends the one catalog line `S.CHAT_FAILED`, never a report.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from yordamchi.core import json_value, timefmt
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText
from yordamchi.dev.projects.domain import shorten
from yordamchi.dev.turns.domain import LANGUAGE_NAMES
from yordamchi.shared.burst.domain import TRANSCRIBED_KINDS, TurnInput
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot

type Scrub = Callable[[str], BotText]  # `core.redact.scrubbed`: the owner's words and the model's answer, masked

SYSTEM_PROMPT: Final = "dev/chat_system"
CONTEXT_PROMPT: Final = "dev/chat_context"
SOURCE: Final = "chat"  # `usage_records.source`
ANSWER_MAX: Final = 3500  # characters: one Telegram message (4,096) with room to spare, as the personal bot's default
PART_MAX: Final = 4000  # characters of one part sent to the model
FENCE: Final = "```"
# The report prompt's small-talk marker (`digest.domain.CHAT_MARK`), should a warm model echo it here.
_MARKER: Final = re.compile(r"\A\s*\[chat\]\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ChatSettings:
    """The CHAT settings of one answer, read once from the snapshot."""

    model: str
    effort: str
    timeout_s: float
    persona: str
    language: str  # `lang.reply`: "uz", "ru" or "en"
    timezone: str


def chat_settings(snap: Snapshot) -> ChatSettings:
    return ChatSettings(
        model=snap[defaults.MODELS_CHAT],
        effort=snap[defaults.MODELS_CHAT_EFFORT],
        timeout_s=snap[defaults.TIMEOUTS_CHAT_S],
        persona=snap[defaults.PERSONA_DEV_CHAT],
        language=snap[defaults.LANG_REPLY],
        timezone=snap[defaults.UI_TIMEZONE],
    )


def context_slots(cfg: ChatSettings) -> dict[str, str]:
    """The slots of `prompts/dev/chat_context.md`: the owner-editable block sent with every ask (SPEC §5.4)."""
    return {"persona": cfg.persona, "language": LANGUAGE_NAMES.get(cfg.language, cfg.language)}


def chat_prompt(item: TurnInput, *, now: datetime, timezone: str, scrub: Scrub) -> str:
    """The ask as one canonical JSON object (the system prompt explains its fields). Parts with no words (a photo)
    are left out: routing sends only bursts of words here (`routing.domain.CHAT_KINDS`)."""
    message: list[JsonValue] = []
    for part in item.parts:
        if not part.text:
            continue
        entry: JsonObject = {"text": shorten(part.text, PART_MAX)}
        if part.kind in TRANSCRIBED_KINDS:
            entry["voice"] = True
        if part.forwarded:
            entry["forwarded"] = True
        message.append(entry)
    payload: JsonObject = {
        "message": message,
        "now": timefmt.local(now, timezone).isoformat(timespec="minutes"),
    }
    return json_value.dumps(json_value.map_strings(payload, scrub))


def _close_fence(text: str) -> str:
    """A cut that fell inside a code block gets its closing fence back, so Telegram still shows it as code."""
    return f"{text}\n{FENCE}" if text.count(FENCE) % 2 else text


def answer_text(reply: str, scrub: Scrub) -> BotText | None:
    """What the owner reads of the model's answer: scrubbed and within one message. None when it is empty (the
    service then sends `S.CHAT_FAILED`, as for a failed or timed-out ask)."""
    text = _MARKER.sub("", reply.strip()).strip()
    if not text:
        return None
    return scrub(_close_fence(shorten(text, ANSWER_MAX)))
