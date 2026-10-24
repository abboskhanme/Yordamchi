"""Personal assistant rules, pure (SPEC §3.2.13; UC-30, UC-31 content-is-data, Q4; T72).

What the owner sends becomes one user message (`question_of`, `prompt_of`): his own words as they are, and anything
he forwarded inside `<forwarded>` blocks, which the system prompt treats as data. The model answers in the contract of
`prompts/personal/qa_system.md`:

    <the answer>
    ---OVOZ---
    <the voice script>
    ---TOLIQ---          (only when the answer was shortened)
    <the full answer>

`parse_answer` turns that into what is sent: the Telegram text (markdown the outbox would show raw is removed, and
the limits `text.max_chars` / `text.max_lines` are enforced whatever the model did; a cut answer keeps its full
version for "to'liqroq"), the voice script (at most `voice.max_words` words, cut at a sentence end) and the full
version. A marker line never reaches the owner, in the final text or in a streamed draft (`draft_text`).

Also here: when a conversation starts anew (`rotation_due`), the five-line summary it is seeded with
(`exchange_line`, `rolled_summary`), and which Claude failures the fallback model answers (`falls_back`, Q4).
Every input is an argument, `now` and setting values included; nothing here does I/O (C6, A1).
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core.errors import ErrorCode
from yordamchi.shared.burst.domain import TurnInput

MARK_VOICE: Final = "---OVOZ---"
MARK_FULL: Final = "---TOLIQ---"
FORWARDED_OPEN: Final = "<forwarded>"
FORWARDED_CLOSE: Final = "</forwarded>"
_FORWARD_MARKERS: Final = ((FORWARDED_CLOSE, "</forwarded-quoted>"), (FORWARDED_OPEN, "<forwarded-quoted>"))
# A marker line as models write it: dashes around OVOZ or TOLIQ (TO'LIQ), maybe wrapped in markdown.
_MARKER: Final = re.compile(
    r"^[ \t>*_`#]*-{2,}[ \t]*(?P<name>OVOZ|TO['’‘ʻ]?LIQ)[ \t]*-{2,}[ \t*_`]*[ \t]*$", re.IGNORECASE | re.MULTILINE
)
# The start of a marker line still being streamed: never shown in a draft.
_MARKER_START: Final = re.compile(r"(?:^|\n)[ \t>*_`#]*-{2,}[^\n]*$")
_FENCE: Final = re.compile(r"^[ \t]*```")
_HEADING: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+")
_BULLET: Final = re.compile(r"^([ \t]*)[*+-][ \t]+")
_BOLD: Final = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC: Final = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_LINK: Final = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_SENTENCE_END: Final = re.compile(r"[.!?…](?=\s|$)")
_BLANK_RUNS: Final = re.compile(r"\n{3,}")
ELLIPSIS: Final = "…"
EXCHANGE_QUESTION_MAX: Final = 100
EXCHANGE_ANSWER_MAX: Final = 140
SUMMARY_LINES: Final = 5  # SPEC §3.2.13 step 10: "a 5-line summary of the old session"


# --- the question ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QaQuestion:
    """One closed burst for the assistant: the owner's own words, and what he forwarded (material, never a command)."""

    chat_id: int
    own_text: str = field(repr=False)
    forwarded: tuple[str, ...] = field(default=(), repr=False)
    burst_id: int | None = None
    reply_to: int | None = None
    # `personal/media` (T73) briefs: each a delimited, defused block of a video's findings or a page's text, put into
    # the prompt as it is (data; the assistant says what it is, the key points and its opinion).
    material: tuple[str, ...] = field(default=(), repr=False)

    @property
    def empty(self) -> bool:
        parts = (self.own_text, *self.forwarded, *self.material)
        return not any(part.strip() for part in parts)


def question_of(item: TurnInput) -> QaQuestion:
    """The burst's own words (text, captions, voice transcripts) and its forwarded parts, in arrival order."""
    forwarded = tuple(part.text for part in item.parts if part.forwarded and part.text)
    return QaQuestion(
        chat_id=item.chat_id,
        own_text=item.own_text,
        forwarded=forwarded,
        burst_id=item.burst_id,
        reply_to=item.own_reply_to,
    )


def _defused(text: str) -> str:
    for marker, quoted in _FORWARD_MARKERS:
        text = text.replace(marker, quoted)
    return text


def prompt_of(question: QaQuestion) -> str:
    """The user message: his own words first, then each forwarded part in its own `<forwarded>` block (a forwarded
    text cannot close its block early or open a new one: its markers are defused), then any media material."""
    parts = [_defused(question.own_text.strip())] if question.own_text.strip() else []
    parts += [f"{FORWARDED_OPEN}\n{_defused(text.strip())}\n{FORWARDED_CLOSE}" for text in question.forwarded]
    parts += [block.strip() for block in question.material if block.strip()]  # already delimited by personal/media
    return "\n\n".join(parts)


# --- the answer ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Limits:
    """This turn's `text.max_chars`, `text.max_lines` and `voice.max_words`."""

    max_chars: int
    max_lines: int
    voice_words: int


@dataclass(frozen=True, slots=True)
class ParsedAnswer:
    """What is sent: `text` (within the limits), `voice` (the spoken summary) and `full`, the complete answer when
    `text` is a shortened form of it (the model's own `---TOLIQ---`, or the text before Yordamchi cut it)."""

    text: str = field(repr=False)
    voice: str = field(repr=False)
    full: str | None = field(default=None, repr=False)
    cut: bool = False  # the model ignored the limits and Yordamchi shortened the text

    @property
    def compressed(self) -> bool:
        return self.full is not None


def sections(raw: str) -> tuple[str, str, str]:
    """`(answer, voice, full)`: the text before the first marker, after `---OVOZ---`, after `---TOLIQ---` (either may
    be missing; the first of each counts)."""
    found: dict[str, str] = {}
    answer_end: int | None = None
    matches = list(_MARKER.finditer(raw))
    for index, match in enumerate(matches):
        name = "voice" if match.group("name").upper() == "OVOZ" else "full"
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        answer_end = match.start() if answer_end is None else answer_end
        found.setdefault(name, raw[match.end() : end].strip())
    answer = raw[:answer_end].strip() if answer_end is not None else raw.strip()
    return answer, found.get("voice", ""), found.get("full", "")


def plain_text(text: str) -> str:
    """Markdown the outbox would show raw (headings, bold, italics, `*` bullets, `[a](url)` links), outside code
    fences, as plain Telegram text."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        plain = _HEADING.sub("", line)
        plain = _BULLET.sub(r"\1• ", plain)
        plain = _MD_LINK.sub(r"\1 (\2)", plain)
        plain = _BOLD.sub(r"\2", plain)
        plain = _ITALIC.sub(r"\1", plain)
        lines.append(plain.rstrip())
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def _within(text: str, max_chars: int, max_lines: int) -> bool:
    return len(text) <= max_chars and len(text.splitlines()) <= max_lines


def cut_sentence(text: str, max_chars: int) -> str:
    """At most `max_chars`: up to the last sentence end that fits, else the last word boundary plus "…"."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 1]
    ends = [match.end() for match in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= max_chars // 3:
        return head[: ends[-1]].rstrip()
    space = head.rfind(" ")
    return (head[:space] if space > max_chars // 3 else head).rstrip(" ,;:—-") + ELLIPSIS


def fit(text: str, *, max_chars: int, max_lines: int) -> str:
    """`text` within the limits: whole lines while they fit, and a first line too long for them cut at a sentence."""
    if _within(text, max_chars, max_lines):
        return text
    kept: list[str] = []
    for line in text.splitlines():
        if len(kept) + 1 > max_lines or len("\n".join([*kept, line])) > max_chars:
            break
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        return cut_sentence(text.splitlines()[0] if text.strip() else "", max_chars)
    return "\n".join(kept).rstrip()


def voice_script(text: str, max_words: int) -> str:
    """The spoken summary on one line, at most `max_words` words, cut at the last sentence end that fits (else at
    the word limit, closed with a full stop). `voice.domain.speakable` (T71) cleans it further before TTS."""
    cleaned = " ".join(line.strip().lstrip("•").strip() for line in text.splitlines() if line.strip())
    cleaned = " ".join(cleaned.split())
    words = cleaned.split(" ") if cleaned else []
    if len(words) <= max_words:
        return cleaned
    head = " ".join(words[:max_words])
    ends = [match.end() for match in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= len(head) // 3:
        return head[: ends[-1]]
    return head.rstrip(" ,;:—-") + "."


def parse_answer(raw: str, limits: Limits, *, reserve_chars: int = 0, reserve_lines: int = 0) -> ParsedAnswer:
    """The model's reply as it is sent. `reserve_*` keeps room for the lines Yordamchi adds (the fallback header, the
    freshness line, the "to'liqroq" hint)."""
    answer, voice, full = (plain_text(part) for part in sections(raw))
    if not answer and full:
        answer = full  # only the full version came: shorten it ourselves
    max_chars = max(1, limits.max_chars - reserve_chars)
    max_lines = max(1, limits.max_lines - reserve_lines)
    cut = not _within(answer, max_chars, max_lines)
    text = fit(answer, max_chars=max_chars, max_lines=max_lines)
    complete = full or (answer if cut else "")
    spoken = voice_script(voice or text, limits.voice_words)
    return ParsedAnswer(text=text, voice=spoken, full=complete if complete and complete != text else None, cut=cut)


def draft_text(partial: str, max_chars: int) -> str:
    """What a streamed draft may show: the answer so far, never a marker or what follows it, never a half marker."""
    answer = partial
    match = _MARKER.search(answer)
    if match is not None:
        answer = answer[: match.start()]
    tail = _MARKER_START.search(answer)
    if tail is not None:
        answer = answer[: tail.start()]
    return plain_text(answer)[:max_chars].rstrip()


# --- conversations ---------------------------------------------------------------------------------------------------
class RotateReason(StrEnum):
    IDLE = "idle"  # `qa.session_idle_rotate_h` since the last answer
    WEB_SEARCHES = "web_searches"  # `qa.web_search_rotate_at` searches, below the CLI's per-session cap
    OWNER = "owner"  # "yangi suhbat": a fresh start, without a summary


def rotation_due(
    last_active_at: datetime, web_searches: int, *, now: datetime, idle_rotate_h: int, rotate_at: int
) -> RotateReason | None:
    if now - last_active_at >= timedelta(hours=idle_rotate_h):
        return RotateReason.IDLE
    if web_searches >= rotate_at:
        return RotateReason.WEB_SEARCHES
    return None


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + ELLIPSIS


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = _SENTENCE_END.search(flat)
    return flat[: match.end()] if match is not None else flat


def exchange_line(question: str, answer: str) -> str:
    """One line of the conversation summary: the question, then the answer's first sentence."""
    return f"{_shorten(question, EXCHANGE_QUESTION_MAX)} → {_shorten(_first_sentence(answer), EXCHANGE_ANSWER_MAX)}"


def rolled_summary(previous: str | None, line: str, *, keep: int = SUMMARY_LINES) -> str:
    """The last `keep` exchange lines, oldest first (stored redacted, seeded into the next conversation)."""
    lines = [*(previous or "").splitlines(), line]
    return "\n".join(item for item in lines[-keep:] if item.strip())


# --- Claude unavailable (Q4) -----------------------------------------------------------------------------------------
# The failures that mean "Claude cannot answer now": all accounts limited, the login refused, the CLI missing, the
# CLI broken or silent. A refused tool list (NFR-SEC-08) is not one: it is a security stop the owner must see.
FALLBACK_CODES: Final = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.QUOTA_EXHAUSTED,
        ErrorCode.AUTH_FAILED,
        ErrorCode.NOT_INSTALLED,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.CALL_TIMED_OUT,
    }
)


def falls_back(code: ErrorCode) -> bool:
    return code in FALLBACK_CODES


class VoiceStatus(StrEnum):
    """`answers.voice_status`. `pending` until the voice step (T71) reports; `off` when voice is disabled."""

    PENDING = "pending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"
    OFF = "off"


def lines_of(texts: Iterable[str]) -> int:
    return sum(len(text.splitlines()) for text in texts)


def reserve_for(texts: Sequence[str]) -> tuple[int, int]:
    """Room (chars, lines) the added lines take, each with its line break."""
    return sum(len(text) + 1 for text in texts), lines_of(texts)
