"""Burst assembly rules, pure (SPEC §4.4, §3.2.1 step 4, §3.2.5, §2.7; UC-02, UC-03).

The owner often sends a screenshot, then text, then a voice note, with pauses between them. A burst gathers them
into one turn:
1. each received update becomes a `BurstPart` (`part_from_update`);
2. `place` decides where it goes: into the chat's assembling burst, into a new one, or alone — an interrupt word
   ("to'xta") or the supervisor's self-test closes at once and is never merged;
3. `window` says when the burst closes if nothing else arrives, or that it holds (a transcript is pending, or the
   owner said "kut": "wait, more is coming", never "stop"; with nothing assembling it opens a burst that holds for
   the next part);
4. at close its content parts become one `TurnInput` for pre-parse and routing;
5. `decide_dev_follow_up` / `decide_personal_follow_up` say whether a burst that closed while a turn was already
   working belongs to that turn (UC-02).

Every instant comes from the parts (when each arrived, when its transcript came back), never from "now", so a restart
computes the windows the live path did (SPEC §2.7). SPEC §4.4 writes the rule as `window(parts, now, snap) →
closes_at | HOLD`: here `Window.hold` is the HOLD, `Window.closes_at` is its ceiling, and the caller compares
`closes_at` with now (`Window.is_due`).

There are two clocks. The bot's (`BurstPart.at`, when a part was received) arms the timer. The owner's
(`BurstPart.sent_at`, Telegram's date) also decides placement: what Telegram held while the Mac slept, or the network
or the bot was down, arrives in one batch, so only the send times still tell messages written minutes apart (T36
BUG-1). Live, the two agree to within Telegram's whole second.

Forum topics (D4 revised 2026-10-26): a burst never holds parts of two topics. A chat still has one assembling burst
at a time (its unique index is frozen in `0001`), so a part from another topic closes the assembling one first
(`CloseReason.THREAD`), or, while that one waits for a transcript, waits itself (`Into.WAIT`) until it can go.
"""

import itertools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.shared.inbox import domain as inbox
from yordamchi.shared.inbox.domain import Content, StoredUpdate, UpdateKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot

ROW_KEEP: Final = timedelta(days=30)  # SPEC §4.2 `bursts`: 30 d
# Telegram reports a voice note's duration in whole seconds, so its recording may have begun up to 1 s earlier.
DURATION_PRECISION: Final = timedelta(seconds=1)
# Telegram dates a message in whole seconds, so two send times may read up to 1 s further apart than they were.
DATE_PRECISION: Final = timedelta(seconds=1)
# SPEC §3.2.5: a voice note counts as an interrupt only if its transcript is this short.
VOICE_INTERRUPT_MAX_WORDS: Final = 4
# Parts that wait for a transcript from `shared/voice` (T19): the window holds until it arrives.
TRANSCRIBED_KINDS: Final = frozenset({UpdateKind.VOICE})
# Parts that wait like a bare file when they carry no caption (SPEC §4.4).
FILE_KINDS: Final = frozenset({UpdateKind.PHOTO, UpdateKind.VIDEO, UpdateKind.DOCUMENT, UpdateKind.AUDIO})
COMPLETE_TEXT_MIN_WORDS: Final = 3  # SPEC §4.4: "≥ 3 words and ends with ?, . or !"


class BurstStatus(StrEnum):
    OPEN = "open"  # assembling: closes at `closes_at` unless another part arrives
    HOLDING = "holding"  # assembling, waiting for an event: a transcript, or the owner's next part after "kut"
    CLOSED = "closed"  # one turn input, waiting for pre-parse and routing
    ROUTED = "routed"
    MERGED = "merged"  # delivered into a turn that was already working (UC-02 follow-up)
    CANCELLED = "cancelled"  # handled by the bot itself, or a "kut" hold that nothing followed


ASSEMBLING: Final = frozenset({BurstStatus.OPEN, BurstStatus.HOLDING})
# Retention deletes only these; an assembling or closed burst still has work ahead of it.
TERMINAL: Final = frozenset({BurstStatus.ROUTED, BurstStatus.MERGED, BurstStatus.CANCELLED})


class CloseReason(StrEnum):
    QUIET = "quiet"  # the quiet window after the last part passed
    CAP = "cap"  # `max_total_s` from the first part, plus any time spent in a "kut" hold
    HOLD_EXPIRED = "hold_expired"  # a "kut" or a pending transcript held for `hold_max_s`
    INTERRUPTED = "interrupted"  # an interrupt word arrived: this burst closed at once, without it
    INTERRUPT = "interrupt"  # this burst is the interrupt word itself, never merged (UC-03)
    SYNTHETIC = "synthetic"  # the supervisor's self-test: window 0 (SPEC §3.2.12)
    THREAD = "thread"  # a part from another forum topic arrived: this burst closed at once, without it


class HoldReason(StrEnum):
    TRANSCRIPT = "transcript"  # a voice part is being transcribed
    OWNER = "owner"  # the owner said "kut": more is coming


class PartRole(StrEnum):
    CONTENT = "content"
    HOLD = "hold"  # a "kut": it holds the window for the next part and never reaches the turn


class TranscriptState(StrEnum):
    NONE = "none"  # not a transcribed part
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"  # the text is voice's "not understood" marker; the part stays in the burst (UC-05 F)


class InterruptKind(StrEnum):
    STOP = "stop"
    STOP_ALL = "stop_all"
    REDIRECT = "redirect"
    HOLD = "hold"  # "kut": holds the window for the next part; it never stops anything (T34b)


# --- parts --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BurstPart:
    """One owner message inside a burst. Its content is the inbox's redacted content; no text shows in a repr."""

    update_id: int
    kind: UpdateKind
    at: datetime  # when the update was received
    sent_at: datetime | None = None  # when the owner sent it (Telegram's date); None: unknown, `at` decides alone
    content: Content = field(default_factory=Content)
    message_id: int | None = None
    media_group_id: str | None = None
    reply_to_message_id: int | None = None
    synthetic: bool = False
    role: PartRole = PartRole.CONTENT
    transcript: TranscriptState = TranscriptState.NONE
    transcript_text: str | None = field(default=None, repr=False)
    transcript_at: datetime | None = None

    @property
    def text(self) -> str | None:
        """What the owner said in this part: a voice note's transcript, else the text, else the caption."""
        if self.transcript_text is not None:
            return self.transcript_text
        return self.content.text if self.content.text is not None else self.content.caption

    @property
    def forwarded(self) -> bool:
        """Someone else's message the owner forwarded: material, never a command (`inbox.ForwardSource`)."""
        return self.content.forwarded

    @property
    def started_at(self) -> datetime:
        """When the owner began this part. A voice note was being recorded for its duration before it arrived."""
        attachment = self.content.attachment
        if self.kind in TRANSCRIBED_KINDS and attachment is not None and attachment.duration_s:
            return self.at - timedelta(seconds=attachment.duration_s) - DURATION_PRECISION
        return self.at

    @property
    def is_bare_media(self) -> bool:
        """A photo, video or file with no caption, or a text of nothing but links (SPEC §4.4)."""
        if self.kind is UpdateKind.LINK:
            return True
        return self.kind in FILE_KINDS and not self.text


def part_from_update(update: StoredUpdate) -> BurstPart:
    """A received inbox update as a burst part; a voice note starts out waiting for its transcript."""
    return BurstPart(
        update_id=update.update_id,
        kind=update.kind,
        at=update.received_at,
        sent_at=update.sent_at,
        content=update.content,
        message_id=update.message_id,
        media_group_id=update.media_group_id,
        reply_to_message_id=update.reply_to_message_id,
        synthetic=update.synthetic,
        transcript=TranscriptState.PENDING if update.kind in TRANSCRIBED_KINDS else TranscriptState.NONE,
    )


# --- settings -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WindowSettings:
    """The BURST settings of one pass, in seconds (SPEC §4.3), read once from the snapshot."""

    after_complete_text_s: float
    after_text_s: float
    after_media_s: float
    album_gap_s: float
    after_voice_s: float
    hold_max_s: float
    max_total_s: float


def window_settings(snap: Snapshot) -> WindowSettings:
    return WindowSettings(
        after_complete_text_s=snap[defaults.BURST_AFTER_COMPLETE_TEXT_S],
        after_text_s=snap[defaults.BURST_AFTER_TEXT_S],
        after_media_s=snap[defaults.BURST_AFTER_MEDIA_S],
        album_gap_s=snap[defaults.BURST_ALBUM_GAP_S],
        after_voice_s=snap[defaults.BURST_AFTER_VOICE_S],
        hold_max_s=snap[defaults.BURST_HOLD_MAX_S],
        max_total_s=snap[defaults.BURST_MAX_TOTAL_S],
    )


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


# --- text ---------------------------------------------------------------------------------------------------------
def word_count(text: str) -> int:
    """Whitespace-separated tokens holding a letter or a digit ("—" and "..." are not words)."""
    return sum(1 for token in text.split() if any(char.isalnum() for char in token))


def is_complete_text(text: str) -> bool:
    """SPEC §4.4: at least 3 words and a final `?`, `.` or `!`. A trailing ellipsis says more is coming."""
    stripped = text.strip()
    if not stripped.endswith(("?", ".", "!")) or stripped.endswith(".."):
        return False
    return word_count(stripped) >= COMPLETE_TEXT_MIN_WORDS


def _text_quiet_s(text: str, settings: WindowSettings) -> float:
    stripped = text.strip()
    if inbox.kind_for_text(stripped) is UpdateKind.LINK:
        return settings.after_media_s  # nothing but links: the explanation usually follows
    if stripped.endswith(":"):
        return settings.after_media_s  # "mana xato:" announces the screenshot that is still uploading
    if is_complete_text(stripped):
        return settings.after_complete_text_s
    return settings.after_text_s


# --- the window ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Window:
    """When an assembling burst closes if nothing else arrives first."""

    closes_at: datetime  # in the past: close at once
    reason: CloseReason  # what closes it then: QUIET, CAP or HOLD_EXPIRED
    hold: HoldReason | None = None  # set while the burst holds; `closes_at` is then the hold's ceiling

    @property
    def status(self) -> BurstStatus:
        return BurstStatus.OPEN if self.hold is None else BurstStatus.HOLDING

    def is_due(self, now: datetime) -> bool:
        return self.closes_at <= now

    def admits(self, at: datetime) -> bool:
        """A part received at `at` still belongs to this burst. The boundary instant counts as inside."""
        return at <= self.closes_at


def _last_unit(parts: Sequence[BurstPart]) -> Sequence[BurstPart]:
    """The last part, or the whole album it ends (the trailing parts that share its `media_group_id`)."""
    group = parts[-1].media_group_id
    if group is None:
        return parts[-1:]
    start = len(parts) - 1
    while start > 0 and parts[start - 1].media_group_id == group:
        start -= 1
    return parts[start:]


def _unit_quiet_s(unit: Sequence[BurstPart], settings: WindowSettings) -> float:
    """The quiet window after the last unit. An album waits as one item, and at least `album_gap_s`."""
    last = unit[-1]
    if last.media_group_id is not None:
        caption = next((part.text for part in unit if part.text), None)
        quiet = settings.after_media_s if caption is None else _text_quiet_s(caption, settings)
        return max(settings.album_gap_s, quiet)
    if last.kind in TRANSCRIBED_KINDS:
        return settings.after_voice_s
    if last.kind is UpdateKind.OTHER:  # a sticker, a location: no text to judge, not a file to explain
        return settings.after_text_s
    if last.is_bare_media:
        return settings.after_media_s
    return _text_quiet_s(last.text or "", settings)


def _time_held(parts: Sequence[BurstPart], hold_max: timedelta) -> timedelta:
    """Time spent in "kut" holds that a later part released: SPEC §4.4 exempts it from the hard cap."""
    held = timedelta(0)
    for current, following in itertools.pairwise(parts):
        if current.role is PartRole.HOLD:
            held += max(timedelta(0), min(following.at - current.at, hold_max))
    return held


def window(parts: Sequence[BurstPart], settings: WindowSettings) -> Window:
    """SPEC §4.4, in order:
    - a part still being transcribed → HOLD until its transcript, at most `hold_max_s` after that part arrived. The
      hard cap does not cut it: closing then would hand the turn a voice note nobody has read;
    - the last part a "kut" → HOLD for at most `hold_max_s` (also when the "kut" is all the burst holds yet);
    - else quiet after the last part: `after_complete_text_s` after a complete text, `after_text_s` after other text,
      `after_media_s` after a bare photo, file or link (or a text ending in ":"), and an album as one unit, at least
      `album_gap_s` after its last item; and `after_voice_s` after each transcript;
    - but never later than `max_total_s` from the first part, plus the time any "kut" held it.
    """
    if not parts:
        msg = "a burst has at least one part"
        raise ValueError(msg)
    hold_max = _seconds(settings.hold_max_s)
    pending = [part.at + hold_max for part in parts if part.transcript is TranscriptState.PENDING]
    if pending:
        return Window(max(pending), CloseReason.HOLD_EXPIRED, HoldReason.TRANSCRIPT)
    last = parts[-1]
    if last.role is PartRole.HOLD:
        return Window(last.at + hold_max, CloseReason.HOLD_EXPIRED, HoldReason.OWNER)
    after_voice = _seconds(settings.after_voice_s)
    unit = _last_unit(parts)
    quiet = max(
        [
            unit[-1].at + _seconds(_unit_quiet_s(unit, settings)),
            *(part.transcript_at + after_voice for part in parts if part.transcript_at is not None),
        ]
    )
    cap = parts[0].at + _seconds(settings.max_total_s) + _time_held(parts, hold_max)
    if quiet <= cap:
        return Window(quiet, CloseReason.QUIET)
    return Window(cap, CloseReason.CAP)


# --- interrupt words ----------------------------------------------------------------------------------------------
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ’‘`", "'"))
_EDGE_PUNCTUATION: Final = " .,!?;:…"
_PHRASES: Final[dict[str, InterruptKind]] = {
    **dict.fromkeys(("to'xta", "to'xtat", "toxta", "toxtat", "stop", "bas", "bas qil", "стоп"), InterruptKind.STOP),
    **dict.fromkeys(
        ("hammasini to'xtat", "hammasini to'xta", "hammasini toxtat", "hammasini toxta", "hammasini stop", "stop all"),
        InterruptKind.STOP_ALL,
    ),
    **dict.fromkeys(("kut", "kutib tur", "biroz kut", "bir oz kut", "kuting", "wait"), InterruptKind.HOLD),
}
_REDIRECT_PREFIXES: Final = ("yo'q, boshqacha", "yo'q boshqacha", "yoq, boshqacha", "yoq boshqacha")


def normalise(text: str) -> str:
    """Lowercase, one apostrophe (ʻ ʼ ’ ‘ ` → '), single spaces, no punctuation at either end."""
    return " ".join(text.lower().translate(_APOSTROPHES).split()).strip(_EDGE_PUNCTUATION)


def interrupt_of(text: str) -> InterruptKind | None:
    """The interrupt a message is (SPEC §3.2.5, §6.1), or None. `HOLD` ("kut") is the one that stops nothing: it
    holds the window for the next part (`place`).

    Only a whole phrase counts, or a phrase as the first comma-separated clause ("kut, rasm yuboraman"), or a
    redirect prefix ("yo'q, boshqacha qil: …"). "serverni to'xtat" is an instruction for a project, not an interrupt:
    pre-parse, which knows the project names, handles a stop that names its target.
    """
    phrase = normalise(text)
    if phrase.startswith(_REDIRECT_PREFIXES):
        return InterruptKind.REDIRECT
    first_clause = normalise(phrase.split(",", 1)[0])
    return _PHRASES.get(phrase) or _PHRASES.get(first_clause)


def interrupt_of_part(part: BurstPart) -> InterruptKind | None:
    """A text message that is an interrupt word, or a voice note whose transcript is one and has at most
    `VOICE_INTERRUPT_MAX_WORDS` words. A caption never is: a photo captioned "to'xta" is content. A forwarded message
    never is either: someone else's "to'xta" stops nothing."""
    if part.forwarded:
        return None
    if part.kind is UpdateKind.TEXT and part.content.text is not None:
        return interrupt_of(part.content.text)
    transcript = part.transcript_text
    is_short = transcript is not None and word_count(transcript) <= VOICE_INTERRUPT_MAX_WORDS
    if part.transcript is TranscriptState.DONE and transcript is not None and is_short:
        return interrupt_of(transcript)
    return None


# --- placement ----------------------------------------------------------------------------------------------------
class Into(StrEnum):
    CURRENT = "current"  # the chat's assembling burst
    EDIT = "edit"  # replaces the part of the same message in the assembling burst
    NEW = "new"  # a new assembling burst
    ALONE = "alone"  # a burst of its own, closed at once and never merged
    WAIT = "wait"  # another topic's burst waits for a transcript: this part stays received until it can go


@dataclass(frozen=True, slots=True)
class Placement:
    into: Into
    part: BurstPart  # as it goes in: a "kut" joins with role HOLD
    close_current: CloseReason | None = None  # first close the chat's assembling burst, for this reason
    close_alone: CloseReason | None = None  # `Into.ALONE`: why the part's own burst closes at once


def _edits(part: BurstPart, parts: Sequence[BurstPart]) -> bool:
    return (
        part.kind is UpdateKind.EDITED
        and part.message_id is not None
        and any(item.message_id == part.message_id and item.role is PartRole.CONTENT for item in parts)
    )


def _as_sent(part: BurstPart) -> BurstPart:
    """The part on the owner's clock: received the instant he sent it, and its transcript back as long after that as
    it really took (STT time is the bot's, not a pause of his). A part with no send time stays as it is."""
    if part.sent_at is None:
        return part
    delay = part.at - part.sent_at
    transcript_at = None if part.transcript_at is None else part.transcript_at - delay
    return replace(part, at=part.sent_at, transcript_at=transcript_at)


def _stays_with(part: BurstPart, assembling: Sequence[BurstPart]) -> bool:
    """An item of an album the burst holds, or an edit of a message it holds: a send time never cuts it off."""
    group = part.media_group_id
    return (group is not None and any(item.media_group_id == group for item in assembling)) or _edits(part, assembling)


def _ended(
    part: BurstPart, assembling: Sequence[BurstPart], current: Window, settings: WindowSettings
) -> CloseReason | None:
    """Why the assembling burst, whose window is `current`, ended before `part`; None while `part` belongs to it.

    By the bot's clock first: `part` was received after the window (a late timer, a restart). Then by the owner's,
    when every part has a send time (two clocks are never compared): he sent `part` after the window his own send
    times give, allowing Telegram's whole second, however close together the two arrived. Never by the owner's clock
    for an album item or an edit of a part the burst holds, nor while the burst waits for a transcript: it keeps its
    window rather than close on a voice note nobody has read.
    """
    if not current.admits(part.at):
        return current.reason
    sent_at = part.sent_at
    if sent_at is None or current.hold is HoldReason.TRANSCRIPT or _stays_with(part, assembling):
        return None
    if any(item.sent_at is None for item in assembling):
        return None
    sent = window([_as_sent(item) for item in assembling], settings)
    return None if sent.admits(sent_at - DATE_PRECISION) else sent.reason


def place(  # noqa: PLR0911  # one return per placement rule
    part: BurstPart, assembling: Sequence[BurstPart], settings: WindowSettings, *, other_thread: bool = False
) -> Placement:
    """Where a newly received part goes, given the parts of the chat's assembling burst (empty if there is none).
    `other_thread`: that burst's parts were written in another forum topic (`_other_thread` has those rules).

    - The self-test stands alone with window 0 and leaves the owner's burst alone.
    - "kut" means "wait, more is coming", never "stop": it joins the assembling burst as a hold, or, with nothing
      assembling, opens a burst that holds for the next part (at most `hold_max_s`). Nothing is closed for it but a
      burst whose window had already ended.
    - Any other interrupt word stands alone, closed at once, and closes the assembling burst at once too
      (`interrupted`) — unless that burst still waits for a transcript, which keeps its window rather than lose the
      voice note.
    - A part received after the assembling burst's window ended opens a new burst, and the old one closes for its
      own reason first: the rule decides by arrival time, so a late timer or a restart changes nothing.
    - So does a part the owner sent after that window by his own send times, though it arrived inside it: Telegram
      hands over what it held while the bot was away in one batch (`_ended`). Album items stay together.
    - An edit of a message in the assembling burst replaces that part; any other part joins.
    """
    if part.synthetic:
        return Placement(Into.ALONE, part, close_alone=CloseReason.SYNTHETIC)
    current = window(assembling, settings) if assembling else None
    ended = None if current is None else _ended(part, assembling, current, settings)
    still_open = current is not None and ended is None
    interrupt = interrupt_of_part(part)
    if other_thread and current is not None:
        return _other_thread(part, current, ended=ended, interrupt=interrupt)
    if interrupt is InterruptKind.HOLD:
        held = replace(part, role=PartRole.HOLD)
        return Placement(Into.CURRENT, held) if still_open else Placement(Into.NEW, held, close_current=ended)
    if interrupt is not None:
        keeps_window = current is not None and current.hold is HoldReason.TRANSCRIPT
        close = CloseReason.INTERRUPTED if still_open and not keeps_window else ended
        return Placement(Into.ALONE, part, close_current=close, close_alone=CloseReason.INTERRUPT)
    if not still_open:
        return Placement(Into.NEW, part, close_current=ended)
    if _edits(part, assembling):
        return Placement(Into.EDIT, part)
    return Placement(Into.CURRENT, part)


def _other_thread(
    part: BurstPart, current: Window, *, ended: CloseReason | None, interrupt: InterruptKind | None
) -> Placement:
    """A part written in another forum topic than the chat's assembling burst. It never joins that burst:
    - an interrupt word stands alone and leaves the other topic's burst as it is (unless its window had ended);
    - anything else takes the chat's one assembling slot: the other burst closes first (`THREAD`), or for its own
      reason if its window had ended; but one still waiting for a transcript keeps its window rather than lose the
      voice note, so the part waits (`WAIT`) and goes once that transcript is in."""
    if interrupt is not None and interrupt is not InterruptKind.HOLD:
        return Placement(Into.ALONE, part, close_current=ended, close_alone=CloseReason.INTERRUPT)
    if ended is None and current.hold is HoldReason.TRANSCRIPT:
        return Placement(Into.WAIT, part)
    held = replace(part, role=PartRole.HOLD) if interrupt is InterruptKind.HOLD else part
    return Placement(Into.NEW, held, close_current=ended or CloseReason.THREAD)


def _edited(original: BurstPart, edit: BurstPart) -> BurstPart:
    """The original part with the edit's text; an edited caption keeps the original's file."""
    content = edit.content
    if content.attachment is None:
        content = replace(content, attachment=original.content.attachment)
    return replace(original, content=content)


def joined(parts: Sequence[BurstPart], placement: Placement) -> tuple[BurstPart, ...]:
    """The assembling burst's parts after a CURRENT or EDIT placement."""
    if placement.into is Into.EDIT:
        edit = placement.part
        return tuple(
            _edited(part, edit) if part.message_id == edit.message_id and part.role is PartRole.CONTENT else part
            for part in parts
        )
    return (*parts, placement.part)


# --- transcripts --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TranscriptOutcome:
    parts: tuple[BurstPart, ...]  # the burst's parts after the transcript
    alone: BurstPart | None = None  # a voice interrupt that leaves the burst: its own burst, closed at once
    close_now: CloseReason | None = None  # the burst itself closes at once, for this reason


def _pending_index(parts: Sequence[BurstPart], update_id: int) -> int | None:
    for index, part in enumerate(parts):
        if part.update_id == update_id and part.transcript is TranscriptState.PENDING:
            return index
    return None


def waits_for(parts: Sequence[BurstPart], update_id: int) -> bool:
    """Part `update_id` of this burst is still waiting for its transcript."""
    return _pending_index(parts, update_id) is not None


def apply_transcript(
    parts: Sequence[BurstPart], update_id: int, *, text: str, ok: bool, at: datetime
) -> TranscriptOutcome:
    """The burst once the transcript of part `update_id` arrived at `at` (`ok=False`: `text` is the failure marker).

    A short transcript that is an interrupt word is handled like the typed word (SPEC §3.2.5): a "kut" becomes a hold,
    whatever else the burst holds; any other interrupt leaves the burst, which closes at once (`interrupted`) unless
    another transcript is still pending; a voice note that is the burst's only content makes the burst the interrupt
    itself.
    """
    index = _pending_index(parts, update_id)
    if index is None:
        msg = f"part {update_id} is not waiting for a transcript"
        raise ValueError(msg)
    state = TranscriptState.DONE if ok else TranscriptState.FAILED
    voice = replace(parts[index], transcript=state, transcript_text=text, transcript_at=at)
    others = (*parts[:index], *parts[index + 1 :])
    with_voice = (*parts[:index], voice, *parts[index + 1 :])
    interrupt = interrupt_of_part(voice)
    if interrupt is None:
        return TranscriptOutcome(with_voice)
    if interrupt is InterruptKind.HOLD:
        return TranscriptOutcome((*parts[:index], replace(voice, role=PartRole.HOLD), *parts[index + 1 :]))
    if not has_content(others):
        return TranscriptOutcome(with_voice, close_now=CloseReason.INTERRUPT)
    still_pending = any(part.transcript is TranscriptState.PENDING for part in others)
    return TranscriptOutcome(others, alone=voice, close_now=None if still_pending else CloseReason.INTERRUPTED)


# --- the turn input -----------------------------------------------------------------------------------------------
def has_content(parts: Iterable[BurstPart]) -> bool:
    """The burst holds something for a turn: a "kut" hold alone is no input (it lapses, `cancelled`)."""
    return any(part.role is PartRole.CONTENT for part in parts)


@dataclass(frozen=True, slots=True)
class TurnInput:
    """One closed burst, as pre-parse, routing and the turn see it (SPEC §4.4 "burst end → one TurnInput")."""

    burst_id: int
    chat_id: int
    parts: tuple[BurstPart, ...]  # content parts in arrival order; "kut" holds are left out
    opened_at: datetime
    closed_at: datetime
    close_reason: CloseReason
    thread_id: int | None = None  # the forum topic it was written in; None: General (or no forum)

    @property
    def interrupt(self) -> InterruptKind | None:
        """The interrupt this burst is, if it is one (it has exactly that one part)."""
        if self.close_reason is not CloseReason.INTERRUPT:
            return None
        return interrupt_of_part(self.parts[0])

    @property
    def synthetic(self) -> bool:
        return any(part.synthetic for part in self.parts)

    @property
    def own_parts(self) -> tuple[BurstPart, ...]:
        """The parts in the owner's own words: everything but what he forwarded."""
        return tuple(part for part in self.parts if not part.forwarded)

    @property
    def own_text(self) -> str:
        """The owner's own words, in order: the only text that may be read as a command (an approval, an answer to a
        question or a clarification, a settings request, a correction). Forwarded parts are material only."""
        return " ".join(part.text for part in self.own_parts if part.text)

    @property
    def own_reply_to(self) -> int | None:
        """The message the owner's own words reply to, if any (a forwarded message replies to nothing here)."""
        return next((p.reply_to_message_id for p in self.own_parts if p.reply_to_message_id is not None), None)

    @property
    def forwarded(self) -> bool:
        return any(part.forwarded for part in self.parts)

    @property
    def last_part_at(self) -> datetime:
        return max(part.at for part in self.parts)

    @property
    def message_ids(self) -> frozenset[int]:
        return frozenset(part.message_id for part in self.parts if part.message_id is not None)

    @property
    def media_group_ids(self) -> frozenset[str]:
        return frozenset(part.media_group_id for part in self.parts if part.media_group_id is not None)

    @property
    def is_media_only(self) -> bool:
        """Only bare photos, files or links: proceed with a sensible interpretation, never ask (SPEC §4.4, UC-02)."""
        return all(part.is_bare_media for part in self.parts)


def turn_input(
    burst_id: int,
    chat_id: int,
    parts: Sequence[BurstPart],
    *,
    opened_at: datetime,
    closed_at: datetime,
    reason: CloseReason,
    thread_id: int | None = None,
) -> TurnInput:
    content = tuple(part for part in parts if part.role is PartRole.CONTENT)
    if not content:
        msg = f"burst {burst_id} has no content part: a lone hold lapses (`has_content`), it has no turn input"
        raise ValueError(msg)
    return TurnInput(
        burst_id, chat_id, content, opened_at=opened_at, closed_at=closed_at, close_reason=reason, thread_id=thread_id
    )


def in_route_order(items: Iterable[TurnInput]) -> tuple[TurnInput, ...]:
    """Interrupts first (UC-03: a stop must not wait behind routing another burst), then oldest first."""
    return tuple(sorted(items, key=lambda item: (item.close_reason is not CloseReason.INTERRUPT, item.burst_id)))


@dataclass(frozen=True, slots=True)
class BurstEvents:
    """What one pass changed that other features act on."""

    closed: tuple[TurnInput, ...] = ()  # ready for pre-parse and routing, in `in_route_order`
    transcribe: tuple[BurstPart, ...] = ()  # voice parts whose STT job must start (`shared/voice`)

    def __bool__(self) -> bool:
        return bool(self.closed or self.transcribe)

    def __add__(self, other: "BurstEvents") -> "BurstEvents":
        return BurstEvents(
            closed=in_route_order((*self.closed, *other.closed)),
            transcribe=(*self.transcribe, *other.transcribe),
        )


# --- follow-ups (UC-02) -------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunningTurn:
    """The turn a follow-up may join: a dev turn working on one project, or a personal answer still being written."""

    last_part_at: datetime  # when the latest owner part already in the turn arrived
    project_id: str | None = None  # dev: the turn's project
    message_ids: frozenset[int] = frozenset()  # owner and bot messages of the turn: a reply to one continues it
    media_group_ids: frozenset[str] = frozenset()  # albums already in the turn: a late item continues it
    answered: bool = False  # personal: the reply already went out


@dataclass(frozen=True, slots=True)
class RouteHint:
    """Where routing sends the follow-up on its own (dev): a project, or None when it would ask, and its confidence."""

    project_id: str | None
    confidence: float


class FollowUpReason(StrEnum):
    INTERRUPT = "interrupt"  # an interrupt or the self-test is never merged (UC-03)
    OTHER_PROJECT = "other_project"  # routed to another project: never merged (UC-02 F)
    CONTINUATION = "continuation"  # a reply into the turn, a late album item, or begun right after its last part
    CONFIDENT = "confident"  # routed to the turn's project with at least `merge_min_confidence`
    UNSURE = "unsure"  # below that, or no project: a turn of its own
    ANSWERED = "answered"  # personal: the reply was already sent, so this is the next turn
    UNANSWERED = "unanswered"  # personal: the reply is still being written and will cover this too


@dataclass(frozen=True, slots=True)
class FollowUpDecision:
    merge: bool
    reason: FollowUpReason


def continues(follow_up: TurnInput, running: RunningTurn, settings: WindowSettings) -> bool:
    """The follow-up is the owner still talking about the running turn: it replies to one of its messages, it is a
    late item of one of its albums, or it began within `after_text_s` of its last part. The last covers a voice note
    recorded right after a text: it arrives a minute later, but its recording began inside the window (UC-02)."""
    replies = any(
        part.reply_to_message_id is not None and part.reply_to_message_id in running.message_ids
        for part in follow_up.parts
    )
    late_album = any(
        part.media_group_id is not None and part.media_group_id in running.media_group_ids for part in follow_up.parts
    )
    began = min(part.started_at for part in follow_up.parts)
    return replies or late_album or began <= running.last_part_at + _seconds(settings.after_text_s)


def decide_dev_follow_up(
    follow_up: TurnInput,
    running: RunningTurn,
    *,
    route: RouteHint,
    settings: WindowSettings,
    min_confidence: float,
) -> FollowUpDecision:
    """Does a burst that closed while `running` works belong to it (steering, `deliveries.mode=next`)? In order:
    an interrupt never does; a follow-up routed to another project never does; a continuation does, even when
    routing alone would have asked; else only a route to the same project with `merge_min_confidence` or more.
    Decide before routing asks a clarification, so a continuation is never asked about."""
    if follow_up.interrupt is not None or follow_up.synthetic:
        return FollowUpDecision(merge=False, reason=FollowUpReason.INTERRUPT)
    if route.project_id is not None and route.project_id != running.project_id:
        return FollowUpDecision(merge=False, reason=FollowUpReason.OTHER_PROJECT)
    if continues(follow_up, running, settings):
        return FollowUpDecision(merge=True, reason=FollowUpReason.CONTINUATION)
    if route.project_id is not None and route.confidence >= min_confidence:
        return FollowUpDecision(merge=True, reason=FollowUpReason.CONFIDENT)
    return FollowUpDecision(merge=False, reason=FollowUpReason.UNSURE)


def decide_personal_follow_up(follow_up: TurnInput, running: RunningTurn) -> FollowUpDecision:
    """UC-02 F, personal bot: anything that arrives before the reply is sent is answered in that reply."""
    if follow_up.interrupt is not None or follow_up.synthetic:
        return FollowUpDecision(merge=False, reason=FollowUpReason.INTERRUPT)
    if running.answered:
        return FollowUpDecision(merge=False, reason=FollowUpReason.ANSWERED)
    return FollowUpDecision(merge=True, reason=FollowUpReason.UNANSWERED)


# --- input_json ---------------------------------------------------------------------------------------------------
def _instant(value: datetime | None) -> JsonValue:
    return None if value is None else value.isoformat()


def _part_json(part: BurstPart, scrub: Callable[[str], str]) -> JsonObject:
    stored: JsonObject = {
        "update_id": part.update_id,
        "kind": part.kind.value,
        "at": _instant(part.at),
        "content": inbox.payload_json(part.content, scrub),
        "message_id": part.message_id,
        "media_group_id": part.media_group_id,
        "reply_to_message_id": part.reply_to_message_id,
        "synthetic": part.synthetic,
        "role": part.role.value,
        "transcript": part.transcript.value,
        "transcript_text": None if part.transcript_text is None else scrub(part.transcript_text),
        "transcript_at": _instant(part.transcript_at),
    }
    if part.sent_at is not None:  # only when known: a part without one keeps the shape it always had
        stored["sent_at"] = _instant(part.sent_at)
    return stored


def parts_json(parts: Iterable[BurstPart], scrub: Callable[[str], str]) -> JsonValue:
    """The stored form of a burst's parts; every content string passes `scrub` (SPEC §4.1 ✱)."""
    return [_part_json(part, scrub) for part in parts]


def _str(raw: JsonValue) -> str | None:
    return raw if isinstance(raw, str) else None


def _int(raw: JsonValue) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _member[E: StrEnum](enum: type[E], raw: JsonValue) -> E | None:
    if not isinstance(raw, str):
        return None
    try:
        return enum(raw)
    except ValueError:
        return None


def _datetime(raw: JsonValue) -> datetime | None:
    text = _str(raw)
    if text is None:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _part_from_json(raw: JsonValue) -> BurstPart | None:
    if not isinstance(raw, dict):
        return None
    update_id, at = _int(raw.get("update_id")), _datetime(raw.get("at"))
    kind = _member(UpdateKind, raw.get("kind"))
    role = _member(PartRole, raw.get("role"))
    transcript = _member(TranscriptState, raw.get("transcript"))
    if update_id is None or at is None or kind is None or role is None or transcript is None:
        return None
    return BurstPart(
        update_id=update_id,
        kind=kind,
        at=at,
        sent_at=_datetime(raw.get("sent_at")),  # None in a part stored before send times were kept
        content=inbox.content_from_json(raw.get("content")),
        message_id=_int(raw.get("message_id")),
        media_group_id=_str(raw.get("media_group_id")),
        reply_to_message_id=_int(raw.get("reply_to_message_id")),
        synthetic=raw.get("synthetic") is True,
        role=role,
        transcript=transcript,
        transcript_text=_str(raw.get("transcript_text")),
        transcript_at=_datetime(raw.get("transcript_at")),
    )


def parts_from_json(raw: JsonValue) -> tuple[BurstPart, ...]:
    """The inverse of `parts_json`. A malformed entry is dropped; a row with none left reads as empty."""
    if not isinstance(raw, list):
        return ()
    return tuple(part for part in map(_part_from_json, raw) if part is not None)


# --- retention ----------------------------------------------------------------------------------------------------
def row_cutoff(now: datetime) -> datetime:
    """A finished burst is deleted once `closed_at` is strictly older than this."""
    return now - ROW_KEEP
