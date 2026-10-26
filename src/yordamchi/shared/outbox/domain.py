"""Outbound rules, pure (SPEC §2.7, §3.2.1 steps 3/8/9, §4.6; NFR-REL-06, NFR-CAP-02/03, UC-11, UC-12, NFR-SEC-03).

Every message either bot sends is a row in `outbox_messages`, written before any call (the `dedupe_key` makes the
write idempotent). This module decides everything about those rows; the service only reads, writes and calls:

1. Text (CONVENTIONS §10, the only home): Telegram length in UTF-16 units, the 4,096 / 1,024 split at a paragraph or
   a code fence, the Markdown subset as Telegram HTML (and the same text with its markers stripped, for the plain
   resend and for captions), and the `.md` file once a text needs more than `outbox.max_parts` messages.
2. Payloads: each op has a fixed key list. Nothing else is stored or sent, so no keyboard or markup can ever reach
   Telegram (D8, UC-12).
3. Order: per chat, messages (sends and deletes) go strictly by (priority, id); a row that is not due, or still in
   flight, holds back the messages after it. Edits, pins, reactions and typing are out of band.
4. Rate limits (NFR-CAP-02): one message per second per chat, 20 per minute per group, 30 per second overall; edits
   count. A `retry_after` parks the whole chat.
5. Coalescing (UC-11, SPEC §4.6): one pending row per coalesce key (older ones are superseded), and at most one edit
   per `outbox.progress_edit_s` of the same message.
6. Typing (SPEC §3.2.1 step 3): deduped per chat for 4 s, repeated every 4 s for at most 60 s, ended by the reply.
7. Failures: a 429 parks, a refusal gives up, a transient error retries with backoff. A send whose delivery is
   unknown (the Telegram adapter's contract: it never resends a non-idempotent call) is resent under the same
   `dedupe_key` unless the key's kind is one where a duplicate costs more than a loss.
8. Forum topics (D4 revised 2026-10-26): a message goes to the `Thread` its caller names (an answer goes where the
   owner wrote), else to the open topic of the project it is about, else to the chat itself (General). Typing is
   deduped per (chat, topic). A send into a topic Telegram says is closed or gone is repaired, not given up
   (`shared/topics.repair_thread`).
"""

import html
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core.errors import ExternalError, RateLimited
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.ports.messenger import ChatAction


class Op(StrEnum):
    SEND_TEXT = "send_text"
    EDIT_TEXT = "edit_text"
    DELETE = "delete"
    REACT = "react"
    MARK_READ = "mark_read"  # a reaction set and removed at once: the owner sees ✓✓, no reaction stays
    CHAT_ACTION = "chat_action"
    PIN = "pin"
    SEND_PHOTO = "send_photo"
    SEND_DOCUMENT = "send_document"
    SEND_VOICE = "send_voice"
    SEND_VIDEO = "send_video"


class Kind(StrEnum):
    ACK = "ack"
    ECHO = "echo"
    PROGRESS = "progress"
    RESULT = "result"
    QUESTION = "question"
    DECISION = "decision"
    REPORT = "report"
    NOTICE = "notice"
    HEARTBEAT = "heartbeat"
    FILE = "file"
    VOICE = "voice"
    SYSTEM = "system"


class Status(StrEnum):
    PENDING = "pending"  # durable, waiting for its turn (restart: sent, SPEC §2.7)
    SENDING = "sending"  # claimed; the call may be in flight (restart: resent, duplicate allowed)
    SENT = "sent"
    FAILED = "failed"
    SUPERSEDED = "superseded"  # a newer row with the same coalesce key replaced it
    CANCELLED = "cancelled"  # nothing left to send (typing ended, an empty conditional reply)


TERMINAL: Final = frozenset({Status.SENT, Status.FAILED, Status.SUPERSEDED, Status.CANCELLED})
QUEUED: Final = frozenset({Status.PENDING, Status.SENDING})

FILE_OPS: Final = frozenset({Op.SEND_PHOTO, Op.SEND_DOCUMENT, Op.SEND_VOICE, Op.SEND_VIDEO})
# Ops that post a new message. The Telegram adapter never resends these on an unknown outcome (they are not
# idempotent): the outbox decides, below. Every other op is idempotent and retried inside the adapter's timeout.
MESSAGE_OPS: Final = frozenset({Op.SEND_TEXT, *FILE_OPS})
# Strict per-chat order: what the owner reads in sequence, and the delete that must precede a masked reply.
ORDERED_OPS: Final = frozenset({*MESSAGE_OPS, Op.DELETE})
# What Telegram's flood limits count (SPEC §3.2.1 step 9: "edits counted").
COUNTED_OPS: Final = frozenset({*MESSAGE_OPS, Op.EDIT_TEXT})

# Priority 0 = acks, deletes, reactions, questions and decisions (SPEC §3.2.1–3.2.3), and the answers to what the owner
# asked ("X qanday?", "holat?", "xarajat?", settings): he is waiting for those (D10, NFR-SPD-01), while results and
# echoes of work take the slots left (T37 load test: at priority 1 a report waited up to 33 s behind them for the
# group's 20/min; with echoes at 0 too, every instruction's echo spent the slots kept for answers). A sender that must
# keep an answer behind an echo passes the echo's priority. Shutdown flushes 0–1 (§2.6).
HIGHEST_PRIORITY: Final = 0
LOWEST_PRIORITY: Final = 3
DEFAULT_PRIORITY: Final[Mapping[Kind, int]] = {
    Kind.ACK: 0,
    Kind.QUESTION: 0,
    Kind.DECISION: 0,
    Kind.REPORT: 0,
    Kind.ECHO: 1,
    Kind.RESULT: 1,
    Kind.SYSTEM: 1,
    Kind.NOTICE: 2,
    Kind.PROGRESS: 2,
    Kind.FILE: 2,
    Kind.VOICE: 2,
    Kind.HEARTBEAT: 3,
}

# Telegram limits ("1-4096 characters after entities parsing"; captions 0-1024), counted in UTF-16 code units like
# Telegram's own offsets, so an emoji counts as two.
TEXT_LIMIT: Final = 4096
CAPTION_LIMIT: Final = 1024
SUMMARY_LIMIT: Final = 200  # the one-line caption over a `.md` file
MIN_SPLIT_LIMIT: Final = 64
DEDUPE_KEY_MAX: Final = 128  # outbox_messages.dedupe_key is String(128)
FENCE: Final = "```"
ELLIPSIS: Final = "…"

TYPING_EVERY: Final = timedelta(seconds=4)
TYPING_FOR: Final = timedelta(seconds=60)
# Kinds whose message does not end the typing hint: the ack of intake comes before the work it announces, and a
# turn's progress line is no answer (with `ack.typing_for_work` on, the turn's steps keep the hint up).
KEEPS_TYPING: Final = frozenset({Kind.ACK, Kind.HEARTBEAT, Kind.PROGRESS})

# Failure handling. RateLimited never uses up an attempt.
BACKOFF_BASE: Final = timedelta(seconds=1)
BACKOFF_MAX: Final = timedelta(seconds=60)
# ≈ 25 min of continuous failure while the Mac is awake (1, 2, 4 … 60 s): a 10-min network drop loses nothing
# (NFR-REL-03). Acks are short-lived: a delete that keeps failing turns into "kalitni almashtiring" within seconds.
MESSAGE_MAX_ATTEMPTS: Final = 30
EDIT_MAX_ATTEMPTS: Final = 5
ACK_MAX_ATTEMPTS: Final = 3
PARK_WITHOUT_RETRY_AFTER: Final = timedelta(seconds=5)
NOT_READY_DELAY: Final = timedelta(seconds=1)  # an edit whose message, or a reply whose delete, is still on its way
# A send whose delivery is unknown is resent (dup ≤ 2 %, NFR-REL-06) unless a duplicate costs more than a loss:
# a second "→ BonviHR" or "Onlayn" is noise, and a voice note only repeats the text sent before it (NFR-SPD-01).
AT_MOST_ONCE_KINDS: Final = frozenset({Kind.ECHO, Kind.HEARTBEAT, Kind.VOICE})

SENT_KEEP: Final = timedelta(days=30)  # SPEC §4.2 "sent 30 d"; every finished row goes by then
LINK_KEEP: Final = timedelta(days=90)


class Code(StrEnum):
    """`last_error_code` values the outbox sets itself; a failed call stores its `ErrorCode` instead."""

    DELIVERY_UNKNOWN = "delivery_unknown"
    NOT_DELETED = "not_deleted"  # Telegram refused the delete (older than 48 h, or no right)
    TARGET_MISSING = "target_missing"  # the message to edit or pin was never sent
    PAYLOAD_INVALID = "payload_invalid"
    FILE_MISSING = "file_missing"
    TYPING_ENDED = "typing_ended"
    NOTHING_TO_SEND = "nothing_to_send"
    INTERNAL = "internal"  # the sender crashed on this row; it is not tried again


# --- the sender's mechanism values -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SenderConfig:
    """Bounds of the sender: mechanism, like the Telegram adapter's retry config. SPEC §4.3 has no keys for them yet
    (proposed: `timeouts.telegram_ack_s`, `_send_s`, `_file_s`); tests pass smaller ones."""

    ack_timeout_s: float = 2.5  # intake acks: UC-04 wants a key message gone ≤ 5 s, and the reply waits for it
    text_timeout_s: float = 10.0
    file_timeout_s: float = 60.0  # uploads up to 50 MB
    hard_grace_s: float = 2.0  # the sender's own bound on top of the adapter's, should an adapter overrun
    idle_poll_s: float = 0.25  # NFR SPD: ack ≤ 1 s from receipt
    batch: int = 200


DEFAULT_CONFIG: Final = SenderConfig()


def call_timeout(op: Op, kind: Kind, config: SenderConfig) -> float:
    if op in FILE_OPS:
        return config.file_timeout_s
    if kind is Kind.ACK:
        return config.ack_timeout_s
    return config.text_timeout_s


# --- dedupe and coalesce keys ------------------------------------------------------------------------------------
def dedupe_key(kind: Kind, ref: str, part: str | int) -> str:
    """`<kind>:<turn_or_ref>:<part>` (SPEC §4.6): the same message enqueued twice is one row."""
    key = f"{kind.value}:{ref}:{part}"
    if not ref or len(key) > DEDUPE_KEY_MAX:
        msg = f"dedupe key must have a ref and at most {DEDUPE_KEY_MAX} characters"
        raise ValueError(msg)
    return key


def op_key(kind: Kind, ref: str, op: Op) -> str:
    """The key of the one `op` row a ref may have: its delete, its reaction, its typing hint, its pin."""
    return dedupe_key(kind, ref, op.value)


def kind_of_key(key: str) -> Kind | None:
    try:
        return Kind(key.partition(":")[0])
    except ValueError:
        return None


def progress_key(turn_id: int) -> str:
    """One live progress message per turn (SPEC §3.2.1 step 8)."""
    return f"progress:{turn_id}"


def typing_key(chat_id: int, thread_id: int | None = None) -> str:
    """One typing hint per chat, and per forum topic in it: `typing:<chat>` for the chat itself (General)."""
    return f"typing:{chat_id}" if thread_id is None else f"typing:{chat_id}:{thread_id}"


# --- text: length, escaping, code blocks -------------------------------------------------------------------------
def tg_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _prefix_within(text: str, limit: int) -> str:
    """The longest prefix of `text` whose Telegram length is ≤ `limit` (never splits a code point)."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def cut(text: str, limit: int) -> str:
    """`text`, or its start plus "…" so that the whole fits `limit`."""
    if tg_len(text) <= limit:
        return text
    return _prefix_within(text, limit - tg_len(ELLIPSIS)).rstrip() + ELLIPSIS


def escape_html(text: str) -> str:
    """Telegram HTML needs only `&`, `<` and `>` escaped; quotes stay as typed (Uzbek uses `'` everywhere)."""
    return html.escape(text, quote=False)


_LANGUAGE: Final = re.compile(r"[A-Za-z0-9_+#.-]{1,32}")


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith(FENCE)


def _fence_language(line: str) -> str:
    words = line.strip()[len(FENCE) :].split()
    return words[0] if words and _LANGUAGE.fullmatch(words[0]) else ""


@dataclass(frozen=True, slots=True)
class _Segment:
    code: bool
    lines: tuple[str, ...]
    language: str = ""


def _segments(text: str) -> list[_Segment]:
    """Plain runs and ``` code blocks, line by line. An unclosed fence runs to the end of the text."""
    segments: list[_Segment] = []
    plain: list[str] = []
    code: list[str] | None = None
    language = ""
    for line in text.split("\n"):
        if code is None:
            if _is_fence(line):
                if plain:
                    segments.append(_Segment(code=False, lines=tuple(plain)))
                    plain = []
                code, language = [], _fence_language(line)
            else:
                plain.append(line)
        elif _is_fence(line):
            segments.append(_Segment(code=True, lines=tuple(code), language=language))
            code = None
        else:
            code.append(line)
    if code is not None:
        segments.append(_Segment(code=True, lines=tuple(code), language=language))
    if plain:
        segments.append(_Segment(code=False, lines=tuple(plain)))
    return segments


# --- text: the Markdown subset -----------------------------------------------------------------------------------
# Catalog templates and model output write the same small subset, and `render` turns it into Telegram HTML:
#
#   **bold** → <b>    `code` → <code>    [text](https://…) → <a href>    a "#", "##" or "###" line → a <b> line
#   a line that starts with "- " or "* " → "• …"    ``` fences → <pre>
#
# An inline construct never crosses a line, and a marker without its partner stays as typed, so whatever comes in,
# what goes out is balanced HTML. `_` is never markup, nor is a single `*` inside a line. `**` opens only after a
# space or an opening bracket or quote, and closes only before a space or punctuation, so snake_case, `a*b`,
# `**kwargs`, `2**10` and `src/**/*.py` go as typed. A backtick inside a word is an apostrophe (o`zgardi), never code.
# A link is https with a dotted host. Telegram lets bold hold a link but neither hold code (Bot API "Formatting
# options"): code inside them is plain.
_BOLD_AFTER: Final = "([\"'«“‘"  # `**` opens after a space or one of these
_BOLD_BEFORE: Final = ".,:;!?)]\"'»”’·…—–-"  # and closes before a space, one of these, or the end
_INLINE: Final = re.compile(
    r"(?<![\w`])`(?=[^`\n]*[^\s`])(?P<code>[^`\n]++)`(?![\w`])"
    r"|\[(?=[^\[\]\n]*[^\s\[\]])(?P<label>[^\[\]\n]++)\]"
    r"\((?P<url>https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d{1,5})?(?:[/?#][^\s()<>\"]*+)?)\)"
    rf"|(?<![^\s{re.escape(_BOLD_AFTER)}])\*\*(?P<bold>[^\s*/](?:.*?[^\s*])??)\*\*(?![^\s{re.escape(_BOLD_BEFORE)}])"
)
_TITLE: Final = re.compile(r"[ \t]{0,3}#{1,3}[ \t]+(?P<title>\S.*?)(?:[ \t]+#+)?[ \t]*")
_ITEM: Final = re.compile(r"(?P<indent>[ \t]*)[-*][ \t]+(?P<item>.*)")
BULLET: Final = "• "


class _Mark(StrEnum):
    TEXT = "text"
    CODE = "code"
    BOLD = "bold"
    LINK = "link"


@dataclass(frozen=True, slots=True)
class _Span:
    """One piece of a line: plain text, or a construct with its content."""

    mark: _Mark
    text: str = ""  # TEXT and CODE: the characters; LINK: the URL
    inner: tuple["_Span", ...] = ()  # BOLD and LINK: what they hold


def _spans(text: str) -> tuple[_Span, ...]:
    """`text` (one line, or a construct's content) cut into its constructs, leftmost first."""
    spans: list[_Span] = []
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            spans.append(_Span(_Mark.TEXT, text[position : match.start()]))
        if (code := match.group("code")) is not None:
            spans.append(_Span(_Mark.CODE, code))
        elif (label := match.group("label")) is not None:
            spans.append(_Span(_Mark.LINK, match.group("url"), _spans(label)))
        else:
            spans.append(_Span(_Mark.BOLD, inner=_spans(match.group("bold"))))
        position = match.end()
    if position < len(text):
        spans.append(_Span(_Mark.TEXT, text[position:]))
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class _Line:
    spans: tuple[_Span, ...]
    title: bool = False  # a `#` line: bold as a whole
    indent: str | None = None  # a list item: the indent before its bullet

    @property
    def marked(self) -> bool:
        return self.title or any(span.mark is not _Mark.TEXT for span in self.spans)


def _line(text: str) -> _Line:
    if (title := _TITLE.fullmatch(text)) is not None:
        return _Line(_spans(title.group("title")), title=True)
    if (item := _ITEM.fullmatch(text)) is not None:
        return _Line(_spans(item.group("item")), indent=item.group("indent"))
    return _Line(_spans(text))


def _html(spans: Iterable[_Span], *, bold: bool = False, link: bool = False) -> str:
    """Escaped text and tags; inside a bold span or a link, code is plain and bold adds no second tag."""
    parts: list[str] = []
    for span in spans:
        if span.mark is _Mark.TEXT or (span.mark is _Mark.CODE and (bold or link)):
            parts.append(escape_html(span.text))
        elif span.mark is _Mark.CODE:
            parts.append(f"<code>{escape_html(span.text)}</code>")
        elif span.mark is _Mark.BOLD:
            inner = _html(span.inner, bold=True, link=link)
            parts.append(inner if bold else f"<b>{inner}</b>")
        else:
            parts.append(f'<a href="{escape_html(span.text)}">{_html(span.inner, bold=bold, link=True)}</a>')
    return "".join(parts)


def _plain(spans: Iterable[_Span]) -> str:
    """What the owner reads, markers stripped; a link keeps its address after its text."""
    parts: list[str] = []
    for span in spans:
        if span.mark is _Mark.BOLD:
            parts.append(_plain(span.inner))
        elif span.mark is _Mark.LINK:
            parts.append(f"{_plain(span.inner)} ({span.text})")
        else:
            parts.append(span.text)
    return "".join(parts)


def _line_html(line: _Line) -> str:
    if line.title:
        return f"<b>{_html(line.spans, bold=True)}</b>"
    body = _html(line.spans)
    return body if line.indent is None else f"{line.indent}{BULLET}{body}"


def _line_plain(line: _Line) -> str:
    body = _plain(line.spans)
    return body if line.indent is None else f"{line.indent}{BULLET}{body}"


def _pre(segment: _Segment) -> str:
    body = escape_html("\n".join(segment.lines))
    if segment.language:
        return f'<pre><code class="language-{segment.language}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    html: bool  # True: send with parse_mode="HTML"


def render(text: str) -> Rendered:
    """The Markdown subset as Telegram HTML: everything escaped, each construct its tag, each fence a `<pre>`.

    A text with nothing to mark (a bullet is no tag) goes without a parse mode, as typed but for its bullets: there
    is nothing for Telegram to refuse, and a catalog line without markup reaches the owner byte for byte."""
    html_lines: list[str] = []
    plain_lines: list[str] = []
    marked = False
    for segment in _segments(text):
        if segment.code:
            html_lines.append(_pre(segment))
            marked = True
            continue
        for raw in segment.lines:
            line = _line(raw)
            marked = marked or line.marked
            html_lines.append(_line_html(line))
            plain_lines.append(_line_plain(line))
    if not marked:
        return Rendered(text="\n".join(plain_lines), html=False)
    return Rendered(text="\n".join(html_lines), html=True)


def plain_text(text: str) -> str:
    """`text` as the owner would read it, with no markup: the markers stripped, a link as "text (address)", a code
    block as its lines. The resend after Telegram refused the HTML, and every caption (they go without a parse mode).
    Never longer than `text`, so whatever fits as Markdown fits as plain text."""
    lines: list[str] = []
    for segment in _segments(text):
        if segment.code:
            lines.extend(segment.lines)
        else:
            lines.extend(_line_plain(_line(raw)) for raw in segment.lines)
    return "\n".join(lines)


# --- text: the split ---------------------------------------------------------------------------------------------
def _pack(pieces: Iterable[str], separator: str, limit: int) -> list[str]:
    """Greedily join pieces (each ≤ `limit`) with `separator` into chunks ≤ `limit`."""
    chunks: list[str] = []
    current: str | None = None
    for piece in pieces:
        if current is None:
            current = piece
        elif tg_len(current) + tg_len(separator) + tg_len(piece) <= limit:
            current = current + separator + piece
        else:
            chunks.append(current)
            current = piece
    if current is not None:
        chunks.append(current)
    return chunks


def _hard_cut(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    while text:
        head = _prefix_within(text, limit) or text[0]
        chunks.append(head)
        text = text[len(head) :]
    return chunks


def _words(line: str, limit: int) -> list[str]:
    """`line` cut at its spaces, but not inside a construct that fits one part: cut in two, it would show its markers
    as typed (each part is rendered on its own)."""
    kept = [match.span() for match in _INLINE.finditer(line) if tg_len(match.group()) <= limit]
    starts = [start for start, _ in kept]
    words: list[str] = []
    start = 0
    index = line.find(" ")
    while index >= 0:
        inside = bisect_right(starts, index) - 1
        if inside < 0 or index >= kept[inside][1]:
            words.append(line[start:index])
            start = index + 1
        index = line.find(" ", index + 1)
    words.append(line[start:])
    return words


def _split_plain(text: str, limit: int, *, words: bool = True) -> list[str]:
    """Lines first, then words (unless `words` is False, for code), then characters."""
    if tg_len(text) <= limit:
        return [text]
    if "\n" in text:
        pieces = [piece for line in text.split("\n") for piece in _split_plain(line, limit, words=words)]
        return _pack(pieces, "\n", limit)
    if words and " " in text.strip():
        pieces = [piece for word in _words(text, limit) for piece in _split_plain(word, limit, words=False)]
        return _pack(pieces, " ", limit)
    return _hard_cut(text, limit)


def _fenced(language: str, body: str) -> str:
    return f"{FENCE}{language}\n{body}\n{FENCE}"


def _blocks(text: str, limit: int) -> list[str]:
    """Paragraphs and whole code blocks, each cut down to ≤ `limit` (a long code block reopens its fence)."""
    blocks: list[str] = []
    for segment in _segments(text):
        if segment.code:
            body = "\n".join(segment.lines)
            whole = _fenced(segment.language, body)
            if tg_len(whole) <= limit:
                blocks.append(whole)
                continue
            room = limit - tg_len(_fenced(segment.language, ""))
            blocks.extend(_fenced(segment.language, chunk) for chunk in _split_plain(body, room, words=False))
            continue
        paragraph: list[str] = []
        for line in (*segment.lines, ""):
            if line.strip():
                paragraph.append(line)
            elif paragraph:
                blocks.extend(_split_plain("\n".join(paragraph), limit))
                paragraph = []
    return blocks


def split_text(text: str, limit: int = TEXT_LIMIT) -> tuple[str, ...]:
    """Parts of at most `limit`, cut at paragraphs and code blocks, then lines, words, characters (NFR-CAP-03).

    A text that fits is returned unchanged. A code block cut in two closes its fence at the end of one part and
    reopens it, with its language, at the start of the next, so every part renders on its own. The other constructs
    never cross a line, and a long line is not cut inside one that fits a part, so no part holds half a construct.

    The split runs on the Markdown, before `render`; each part is rendered when it is sent. Telegram counts a text
    "after entities parsing" (markers and tags gone, `&lt;` one character), and every construct only drops
    characters (a bullet replaces "-" one for one), so a part within `limit` is within it as Telegram counts too."""
    if limit < MIN_SPLIT_LIMIT:
        msg = f"split limit must be at least {MIN_SPLIT_LIMIT}"
        raise ValueError(msg)
    if not text.strip():
        return ()
    if tg_len(text) <= limit:
        return (text,)
    return tuple(_pack(_blocks(text, limit), "\n\n", limit))


def fit_one(text: str, limit: int = TEXT_LIMIT) -> str:
    """One message, whatever the length (an edit cannot be split): the first part, plus "…" if anything was cut."""
    if tg_len(text) <= limit:
        return text
    more = "\n" + ELLIPSIS
    return split_text(text, limit - tg_len(more))[0] + more


def summary_line(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """The first line that says something (fence markers skipped), cut to `limit`."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith(FENCE):
            return cut(stripped, limit)
    return ELLIPSIS


_UNSAFE_FILENAME: Final = re.compile(r"[^A-Za-z0-9_.-]+")


def file_name(stem: str) -> str:
    """A safe `.md` file name from a message ref, e.g. `result-17.md`."""
    safe = _UNSAFE_FILENAME.sub("-", stem).strip("-.")[:60]
    return f"{safe or 'matn'}.md"


@dataclass(frozen=True, slots=True)
class LongText:
    """A text too long for `max_parts` messages: sent as one `.md` document whose caption is its first line."""

    summary: str = field(repr=False)
    filename: str
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TextPlan:
    parts: tuple[str, ...] = field(default=(), repr=False)
    file: LongText | None = None


def plan_text(text: str, *, max_parts: int, file_stem: str, limit: int = TEXT_LIMIT) -> TextPlan:
    """Split `text`; more than `max_parts` parts → the `.md` fallback (SPEC §3.2.1 step 9, NFR-CAP-03)."""
    parts = split_text(text, limit)
    if not parts:
        msg = "an outgoing text must not be empty"
        raise ValueError(msg)
    if len(parts) <= max_parts:
        return TextPlan(parts=parts)
    return TextPlan(file=LongText(summary=summary_line(text), filename=file_name(file_stem), content=text))


def plan_caption(caption: str, *, max_parts: int, file_stem: str) -> tuple[str, TextPlan | None]:
    """A caption that fits 1,024, plus the rest of it as follow-up text (split again at 4,096), if any."""
    parts = split_text(caption, CAPTION_LIMIT)
    if len(parts) <= 1:
        return caption, None
    rest = "\n\n".join(parts[1:])
    return parts[0], plan_text(rest, max_parts=max_parts, file_stem=file_stem)


# --- payloads ----------------------------------------------------------------------------------------------------
class PayloadInvalid(ValueError):
    """A payload that does not fit its op: a wiring bug at enqueue, a refused row at send. Never sent."""


@dataclass(frozen=True, slots=True)
class Payload:
    """What `payload_json` holds. Which fields an op may use is fixed by `_KEYS`; content never shows in a repr."""

    text: str | None = field(default=None, repr=False)
    after: str | None = None  # send_text: the dedupe key of a row that must finish first (the delete of a key message)
    text_if_after_failed: str | None = field(default=None, repr=False)  # …and the text to send if it failed
    message_id: int | None = None  # edit, pin, delete, react: a message whose id is known
    target: str | None = None  # edit, pin: the dedupe key of our own message, resolved once it is sent
    every_s: float | None = None  # edit: at most one edit of the target per this many seconds
    emoji: str | None = None
    action: str | None = None
    until: datetime | None = None  # typing: the end of the hint
    path: str | None = None  # a file to send, made by another feature
    content: str | None = field(default=None, repr=False)  # or the text of a `.md` file the sender writes
    filename: str | None = None
    mime: str | None = None
    caption: str | None = field(default=None, repr=False)
    duration_s: int | None = None
    question_id: int | None = None  # for the message's `message_links` row
    decision_id: int | None = None


_FILE_KEYS: Final = frozenset(
    {"path", "content", "filename", "mime", "caption", "duration_s", "question_id", "decision_id"}
)
_KEYS: Final[Mapping[Op, frozenset[str]]] = {
    Op.SEND_TEXT: frozenset({"text", "after", "text_if_after_failed", "question_id", "decision_id"}),
    Op.EDIT_TEXT: frozenset({"text", "message_id", "target", "every_s"}),
    Op.DELETE: frozenset({"message_id"}),
    Op.REACT: frozenset({"message_id", "emoji"}),
    Op.MARK_READ: frozenset({"message_id", "emoji"}),
    Op.CHAT_ACTION: frozenset({"action", "until"}),
    Op.PIN: frozenset({"message_id", "target"}),
    **dict.fromkeys(FILE_OPS, _FILE_KEYS),
}
_TEXT_FIELDS: Final = frozenset({"text", "text_if_after_failed", "content", "caption"})
_ACTIONS: Final = frozenset(action.value for action in ChatAction)


def _set_fields(payload: Payload) -> set[str]:
    return {item.name for item in fields(Payload) if getattr(payload, item.name) is not None}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise PayloadInvalid(reason)


def check_payload(op: Op, payload: Payload) -> None:
    """Refuse a payload that uses a field its op does not have, or misses one it needs."""
    extra = _set_fields(payload) - _KEYS[op]
    _require(not extra, f"{op.value} takes no {sorted(extra)}")
    one_target = (payload.message_id is None) != (payload.target is None)
    if op is Op.SEND_TEXT:
        _require(bool(payload.text) or payload.after is not None, "send_text needs text")
        _require(payload.text_if_after_failed is None or payload.after is not None, "an alternative needs `after`")
    elif op is Op.EDIT_TEXT:
        _require(bool(payload.text) and one_target, "edit_text needs text and one target")
    elif op in {Op.DELETE, Op.REACT, Op.MARK_READ}:
        _require(payload.message_id is not None, f"{op.value} needs a message_id")
        _require(op is Op.DELETE or bool(payload.emoji), f"{op.value} needs an emoji")
    elif op is Op.CHAT_ACTION:
        _require(payload.action in _ACTIONS and payload.until is not None, "chat_action needs an action and until")
    elif op is Op.PIN:
        _require(one_target, "pin needs one target")
    else:
        _require((payload.path is None) != (payload.content is None), f"{op.value} needs a path or content")
        _require(payload.content is None or bool(payload.filename), "a written file needs a filename")


def payload_json(op: Op, payload: Payload, scrub: Callable[[str], str]) -> JsonObject:
    """The stored form; every text passes `scrub` before the write (SPEC §4.1 ✱, §4.5)."""
    check_payload(op, payload)
    stored: JsonObject = {}
    for name in sorted(_KEYS[op]):
        value = getattr(payload, name)
        if value is None:
            continue
        if isinstance(value, datetime):
            stored[name] = value.isoformat()
        elif name in _TEXT_FIELDS and isinstance(value, str):
            stored[name] = scrub(value)
        else:
            stored[name] = value
    return stored


def _int(stored: Mapping[str, JsonValue], name: str) -> int | None:
    raw = stored.get(name)
    _require(raw is None or (isinstance(raw, int) and not isinstance(raw, bool)), f"{name} must be an integer")
    return raw if isinstance(raw, int) else None


def _float(stored: Mapping[str, JsonValue], name: str) -> float | None:
    raw = stored.get(name)
    _require(raw is None or (isinstance(raw, (int, float)) and not isinstance(raw, bool)), f"{name} must be a number")
    return float(raw) if isinstance(raw, (int, float)) else None


def _str(stored: Mapping[str, JsonValue], name: str) -> str | None:
    raw = stored.get(name)
    _require(raw is None or isinstance(raw, str), f"{name} must be text")
    return raw if isinstance(raw, str) else None


def _moment(stored: Mapping[str, JsonValue], name: str) -> datetime | None:
    raw = _str(stored, name)
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise PayloadInvalid(f"{name} must be an ISO instant") from None
    _require(moment.tzinfo is not None, f"{name} must be timezone-aware")
    return moment


def payload_from_json(op: Op, raw: JsonValue) -> Payload:
    """The inverse of `payload_json`. A key the op does not have (a keyboard above all) refuses the whole row."""
    if not isinstance(raw, dict):
        raise PayloadInvalid("payload must be an object")
    unknown = set(raw) - _KEYS[op]
    _require(not unknown, f"{op.value} payload carries unexpected keys {sorted(unknown)}")
    payload = Payload(
        text=_str(raw, "text"),
        after=_str(raw, "after"),
        text_if_after_failed=_str(raw, "text_if_after_failed"),
        message_id=_int(raw, "message_id"),
        target=_str(raw, "target"),
        every_s=_float(raw, "every_s"),
        emoji=_str(raw, "emoji"),
        action=_str(raw, "action"),
        until=_moment(raw, "until"),
        path=_str(raw, "path"),
        content=_str(raw, "content"),
        filename=_str(raw, "filename"),
        mime=_str(raw, "mime"),
        caption=_str(raw, "caption"),
        duration_s=_int(raw, "duration_s"),
        question_id=_int(raw, "question_id"),
        decision_id=_int(raw, "decision_id"),
    )
    check_payload(op, payload)
    return payload


# --- where a message goes, what it is about, and where it stands ------------------------------------------------
@dataclass(frozen=True, slots=True)
class Thread:
    """The forum topic a message goes to, named by the caller (`enqueue_*(thread=…)`): `id` is the topic's
    `message_thread_id`, None the chat itself (General in a forum). A caller that names none gets the topic of the
    project the message is about (`About.project_id`), else the chat itself."""

    id: int | None = None


GENERAL: Final = Thread()
# The ops that go into a topic: everything that posts, and the typing hint. The rest name a message by its id.
THREADED_OPS: Final = frozenset({*MESSAGE_OPS, Op.CHAT_ACTION})


@dataclass(frozen=True, slots=True)
class About:
    """Becomes the `message_links` row of each message posted, so routing can follow a reply (SPEC §3.2.6)."""

    project_id: str | None = None
    turn_id: int | None = None
    question_id: int | None = None
    decision_id: int | None = None


@dataclass(frozen=True, slots=True)
class Link:
    """A message we posted, as routing reads it."""

    chat_id: int
    message_id: int
    kind: Kind
    about: About
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Delivery:
    """Where one queued row stands; `message_id` once it posted a message."""

    dedupe_key: str
    status: Status
    attempts: int
    last_error_code: str | None
    message_id: int | None
    sent_at: datetime | None


# --- what the sender looks at ------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Queued:
    """A pending or in-flight row, as the ordering and failure rules need it."""

    id: int
    chat_id: int
    op: Op
    kind: Kind
    priority: int
    status: Status
    next_attempt_at: datetime
    attempts: int
    dedupe_key: str
    coalesce_key: str | None = None
    thread_id: int | None = None  # the forum topic it goes to; None: the chat itself


def sendable(rows: Iterable[Queued], *, now: datetime, parked: Mapping[int, datetime]) -> tuple[Queued, ...]:
    """The rows one pass may try now, grouped by chat, each chat in (priority, id) order.

    Per chat: the run of due messages from the head of its ordered lane (a row not yet due, or in flight, stops the
    run, so nothing overtakes it), plus every due out-of-band row. A parked chat gets nothing."""
    by_chat: dict[int, list[Queued]] = {}
    for row in sorted(rows, key=lambda queued: (queued.chat_id, queued.priority, queued.id)):
        by_chat.setdefault(row.chat_id, []).append(row)
    picked: list[Queued] = []
    for chat_id, chat_rows in by_chat.items():
        until = parked.get(chat_id)
        if until is not None and until > now:
            continue
        run: list[Queued] = []
        for row in (queued for queued in chat_rows if queued.op in ORDERED_OPS):
            if row.status is not Status.PENDING or row.next_attempt_at > now:
                break
            run.append(row)
        loose = [
            row
            for row in chat_rows
            if row.op not in ORDERED_OPS and row.status is Status.PENDING and row.next_attempt_at <= now
        ]
        picked.extend(sorted(run + loose, key=lambda queued: (queued.priority, queued.id)))
    return tuple(picked)


# --- rate limits (NFR-CAP-02) ------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Window:
    count: int
    period: timedelta


PER_CHAT: Final = Window(1, timedelta(seconds=1))
PER_GROUP: Final = Window(20, timedelta(minutes=1))
OVERALL: Final = Window(30, timedelta(seconds=1))
RATE_MEMORY: Final = max(PER_CHAT.period, PER_GROUP.period, OVERALL.period)


def is_group(chat_id: int) -> bool:
    """Groups and supergroups have negative ids; a private chat's id is the user's."""
    return chat_id < 0


def free_at(sent: Iterable[datetime], window: Window, now: datetime) -> datetime:
    """When one more call fits `window`, given the calls already made (any order)."""
    recent = sorted(at for at in sent if at > now - window.period)
    if len(recent) < window.count:
        return now
    return max(now, recent[-window.count] + window.period)


# A group's 20 calls a minute are not all for background traffic. The last ones stay free for what the owner is
# waiting for (priority 0: an answer, a question, a decision), then for results and echoes of other work (1); progress,
# notices, files and the heartbeat (2–3) stop short of them. Without this, fifteen turns' progress edits spent the whole
# minute and a "X qanday?" answer waited up to 30 s for a slot (T37 load test; D10, NFR-SPD-01).
GROUP_RESERVED: Final[Mapping[int, int]] = {0: 0, 1: 2}  # priority → slots of PER_GROUP it leaves free
BACKGROUND_RESERVED: Final = 5  # every lower priority


def group_window(priority: int) -> Window:
    """The share of `PER_GROUP` a call of `priority` may use."""
    return Window(PER_GROUP.count - GROUP_RESERVED.get(priority, BACKGROUND_RESERVED), PER_GROUP.period)


def next_send_at(
    *,
    now: datetime,
    chat_id: int,
    chat_sent: Iterable[datetime],
    all_sent: Iterable[datetime],
    priority: int = HIGHEST_PRIORITY,
) -> datetime:
    """The earliest moment a counted call of `priority` to `chat_id` stays within every Telegram limit (and, in a
    group, outside the slots kept for higher priorities)."""
    chat_times = list(chat_sent)
    at = free_at(chat_times, PER_CHAT, now)
    if is_group(chat_id):
        at = max(at, free_at(chat_times, group_window(priority), now))
    return max(at, free_at(all_sent, OVERALL, now))


# --- coalescing and typing ---------------------------------------------------------------------------------------
def edit_due_at(now: datetime, last_sent_at: datetime | None, every_s: float | None) -> datetime:
    """At most one update of a message per `every_s` (UC-11: ≤ 1 edit / 5 s per progress message)."""
    if last_sent_at is None or every_s is None:
        return now
    return max(now, last_sent_at + timedelta(seconds=every_s))


def typing_until(now: datetime) -> datetime:
    return now + TYPING_FOR


def first_typing_at(now: datetime, last_sent_at: datetime | None) -> datetime:
    """Deduped per chat: a typing hint sent less than 4 s ago is still showing."""
    if last_sent_at is None or now - last_sent_at >= TYPING_EVERY:
        return now
    return last_sent_at + TYPING_EVERY


def next_typing_at(sent_at: datetime, until: datetime) -> datetime | None:
    """After a hint went out: when to repeat it, or None once the 60 s are up."""
    repeat = sent_at + TYPING_EVERY
    return repeat if repeat < until else None


def ends_typing(op: Op, kind: Kind) -> bool:
    """A reply clears Telegram's typing hint, so the repeats stop with it."""
    return op in MESSAGE_OPS and kind not in KEEPS_TYPING


# --- preparing a call --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Resolved:
    """What the service looked up for one row before its call."""

    target_status: Status | None = None  # the row named by `target` (None: no such row)
    target_message_id: int | None = None
    after_status: Status | None = None  # the row named by `after` (None: no such row)
    last_sent_at: datetime | None = None  # the newest call for this row's coalesce key


@dataclass(frozen=True, slots=True)
class Ready:
    text: str | None = field(default=None, repr=False)  # send_text / edit_text: the stored text to scrub and render
    message_id: int | None = None  # edit, pin, delete, react


@dataclass(frozen=True, slots=True)
class NotYet:
    at: datetime


@dataclass(frozen=True, slots=True)
class Drop:
    status: Status
    code: Code


type Preparation = Ready | NotYet | Drop


def _target(payload: Payload, resolved: Resolved, now: datetime) -> int | NotYet | Drop:
    if payload.message_id is not None:
        return payload.message_id
    if resolved.target_status in QUEUED:
        return NotYet(now + NOT_READY_DELAY)
    if resolved.target_status is Status.SENT and resolved.target_message_id is not None:
        return resolved.target_message_id
    return Drop(Status.FAILED, Code.TARGET_MISSING)


def _prepare_text(payload: Payload, resolved: Resolved, now: datetime) -> Preparation:
    """A reply that waits for its `after` row, then picks the text for how that row ended."""
    if payload.after is not None and resolved.after_status in QUEUED:
        return NotYet(now + NOT_READY_DELAY)
    failed_before = resolved.after_status in {Status.FAILED, Status.CANCELLED, Status.SUPERSEDED}
    text = payload.text_if_after_failed if failed_before and payload.text_if_after_failed else payload.text
    return Ready(text=text) if text else Drop(Status.CANCELLED, Code.NOTHING_TO_SEND)


def _prepare_targeted(op: Op, payload: Payload, resolved: Resolved, now: datetime) -> Preparation:
    """An edit or a pin of a message that exists; an edit also keeps its spacing."""
    target = _target(payload, resolved, now)
    if not isinstance(target, int):
        return target
    if op is Op.EDIT_TEXT and (due := edit_due_at(now, resolved.last_sent_at, payload.every_s)) > now:
        return NotYet(due)
    return Ready(text=payload.text, message_id=target)


def prepare(op: Op, payload: Payload, resolved: Resolved, *, now: datetime) -> Preparation:
    """Whether a claimed row can be called now, and with what."""
    if op is Op.SEND_TEXT:
        return _prepare_text(payload, resolved, now)
    if op in {Op.EDIT_TEXT, Op.PIN}:
        return _prepare_targeted(op, payload, resolved, now)
    if op is Op.CHAT_ACTION and (payload.until is None or payload.until <= now):
        return Drop(Status.CANCELLED, Code.TYPING_ENDED)
    return Ready(message_id=payload.message_id)


# --- after a call ------------------------------------------------------------------------------------------------
class Failure(StrEnum):
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"  # an idempotent call failed in a retryable way: try again later
    UNKNOWN_DELIVERY = "unknown_delivery"  # a message may or may not have been posted
    REFUSED = "refused"  # Telegram refused the request itself; the same request fails again


def failure_of(op: Op, error: ExternalError) -> Failure:
    """The Telegram adapter's contract (`adapters/telegram/translate.py`): a retryable error on a send means the
    request may have reached Telegram; it never resends one itself."""
    if isinstance(error, RateLimited):
        return Failure.RATE_LIMITED
    if not error.retryable:
        return Failure.REFUSED
    return Failure.UNKNOWN_DELIVERY if op in MESSAGE_OPS else Failure.TRANSIENT


def timeout_failure(op: Op) -> Failure:
    """The sender's own bound ran out: for a send, nobody knows whether it went out."""
    return Failure.UNKNOWN_DELIVERY if op in MESSAGE_OPS else Failure.TRANSIENT


def resend_on_unknown(key: str) -> bool:
    """Decided by the kind in the `dedupe_key`: a resend reuses that key, so it is the same row, never a new one."""
    return kind_of_key(key) not in AT_MOST_ONCE_KINDS


def max_attempts(op: Op) -> int:
    if op in {Op.DELETE, Op.REACT, Op.MARK_READ, Op.CHAT_ACTION}:
        return ACK_MAX_ATTEMPTS
    if op in {Op.EDIT_TEXT, Op.PIN}:
        return EDIT_MAX_ATTEMPTS
    return MESSAGE_MAX_ATTEMPTS


def backoff(attempts: int) -> timedelta:
    """1, 2, 4 … 60 s after the 1st, 2nd, 3rd … failed attempt. One sender, so no jitter is needed."""
    exponent = max(0, min(attempts - 1, 16))
    return min(BACKOFF_MAX, BACKOFF_BASE * (1 << exponent))


@dataclass(frozen=True, slots=True)
class Retry:
    at: datetime
    attempts: int  # the count to store: a rate limit gives its attempt back


@dataclass(frozen=True, slots=True)
class Park:
    """Telegram said `retry_after`: the whole chat waits, durably (every pending row of it moves)."""

    until: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class GiveUp:
    code: str


type AfterFailure = Retry | Park | GiveUp


def after_failure(
    row: Queued, failure: Failure, *, error_code: str, now: datetime, retry_after: float | None = None
) -> AfterFailure:
    """What a failed call leads to. `row.attempts` already counts the call that failed."""
    if failure is Failure.RATE_LIMITED:
        wait = timedelta(seconds=retry_after) if retry_after is not None else PARK_WITHOUT_RETRY_AFTER
        return Park(until=now + max(wait, NOT_READY_DELAY), attempts=max(0, row.attempts - 1))
    if failure is Failure.REFUSED:
        return GiveUp(code=error_code)
    if failure is Failure.UNKNOWN_DELIVERY and not resend_on_unknown(row.dedupe_key):
        return GiveUp(code=Code.DELIVERY_UNKNOWN)
    if row.attempts >= max_attempts(row.op):
        return GiveUp(code=error_code)
    return Retry(at=now + backoff(row.attempts), attempts=row.attempts)


# --- retention ---------------------------------------------------------------------------------------------------
def sent_cutoff(now: datetime) -> datetime:
    """A finished row is deleted once `created_at` is strictly older than this. Pending rows are never touched."""
    return now - SENT_KEEP


def link_cutoff(now: datetime) -> datetime:
    return now - LINK_KEEP


def recent_cutoff(now: datetime) -> datetime:
    """Sends older than this no longer matter to any rate window."""
    return now - RATE_MEMORY


def keep_recent(times: Sequence[datetime], now: datetime) -> list[datetime]:
    cutoff = recent_cutoff(now)
    return [at for at in times if at > cutoff]
