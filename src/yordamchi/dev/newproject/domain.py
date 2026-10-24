"""New-project rules, pure (SPEC §3.2.8, §3.2.6 step 4, §4.2 `project_drafts`; UC-14; NFR-SEC-11).

A new project goes through one `project_drafts` row:

    collecting → creating → planning ⇄ awaiting_approval → building → done
                                  ↖ revising ↙
    (collecting | awaiting_approval) → cancelled · creating → failed

- **collecting**: the owner's description, in as many bursts as he likes (`part_of`). It closes on "tamom" /
  "boshla" as its own message or sentence (`split_close`), or after `newproject.draft_silence_s` of silence
  (`silent`). A bare "yo'q" / "bekor qil" cancels it (`is_cancel`).
- **creating**: a folder under `projects.root` named from his words (`propose_name`), the first free of `<Name>`,
  `<Name>2`, … (`name_candidates`; an existing folder is never touched), `git init -b main`, the project registered,
  and the private GitHub repo made before the first push.
- **planning / revising**: a `plan` turn (gate policy PLAN_ONLY: only `docs/**`, `README.md`, `.gitignore`). Messages
  meanwhile steer that turn; they never become a coding turn of a project nobody approved.
- **awaiting_approval**: the plan summary and the docs went to the owner with "tasdiqlaysizmi?". His answer
  (`plan_answer`): "ha" → a `build` turn (FULL); "o'zgartir: …" or any other words → a revision; "yo'q" → cancelled.

`pick_draft` decides whether a message belongs to an open draft at all (routing step 4: "unless another project is
named"). Every input is an argument, `now` and setting values included (C6, A1).
"""

import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import dropwhile
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.core.text import BotText, join
from yordamchi.dev.gate import domain as gate_domain
from yordamchi.dev.gate.domain import OwnerVerdict
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.shared.burst.domain import TurnInput

# --- fixed values of the SPEC and of this feature (not owner behaviour; §4.3 has no key for them) ------------------
DEFAULT_BRANCH: Final = "main"  # SPEC §3.2.8 step 3: `git init -b main`
REMOTE: Final = "origin"
TICK_S: Final = 5.0  # how often `run_drafts` closes silent drafts and catches turn ends a restart missed
GIT_TIMEOUT_S: Final = 10.0  # one local git call
NAME_MAX: Final = 40  # a PascalCase folder name that still reads well (the id column holds 64)
NAME_WORDS: Final = 3  # words of the description that make a name
FALLBACK_NAME: Final = "YangiLoyiha"
NAME_ATTEMPTS: Final = 5  # folders tried when another one appears between the check and the mkdir
GITHUB_NAME_CHECKS: Final = 5  # candidate names checked against the GitHub account
PART_MAX: Final = 4000  # characters kept of one burst of the description
DESCRIPTION_MAX: Final = 20000  # characters of the whole description in the plan prompt
DOCS_MAX: Final = 8  # docs/*.md files sent with the plan
DOC_MAX_BYTES: Final = 512 * 1024  # a larger plan document is named, not sent
DOCS_DIR: Final = "docs"
DOC_SUFFIX: Final = ".md"
DOC_MIME: Final = "text/markdown"
# After the plan turn's own result (priority 1) and before progress notices (2): result → documents → question.
PLAN_PRIORITY: Final = 1
DRAFT_KEEP: Final = timedelta(days=400)  # SPEC §4.2 `project_drafts`: 400 d


class DraftStatus(StrEnum):
    COLLECTING = "collecting"
    CREATING = "creating"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    BUILDING = "building"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


# The drafts an un-named owner message may belong to (routing step 4). A building project is approved: its messages
# are routed like any other project's.
OPEN: Final = frozenset(
    {
        DraftStatus.COLLECTING,
        DraftStatus.CREATING,
        DraftStatus.PLANNING,
        DraftStatus.AWAITING_APPROVAL,
        DraftStatus.REVISING,
    }
)
PLANNING: Final = frozenset({DraftStatus.PLANNING, DraftStatus.REVISING})  # a plan turn is the draft's
TERMINAL: Final = frozenset({DraftStatus.DONE, DraftStatus.CANCELLED, DraftStatus.FAILED})


# --- the description --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DraftPart:
    """One burst of the description, as the agent will read it (already redacted by the inbox)."""

    text: str = field(repr=False)
    at: datetime
    burst_id: int | None = None


def parts_json(parts: Iterable[DraftPart]) -> list[JsonValue]:
    return [{"text": part.text, "at": part.at.isoformat(), "burst_id": part.burst_id} for part in parts]


def parts_from_json(raw: JsonValue) -> tuple[DraftPart, ...]:
    """The inverse of `parts_json`; a malformed entry is left out."""
    if not isinstance(raw, list):
        return ()
    parts: list[DraftPart] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text, at, burst = item.get("text"), item.get("at"), item.get("burst_id")
        if not isinstance(text, str) or not isinstance(at, str):
            continue
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            continue
        burst_id = burst if isinstance(burst, int) and not isinstance(burst, bool) else None
        parts.append(DraftPart(text=text, at=moment, burst_id=burst_id))
    return tuple(parts)


_CLOSE: Final = re.compile(r"(?:\A|[\n.!?])\s*(?:tamom|boshla|boshlang)\s*[.!]*\s*\Z", re.IGNORECASE)


def split_close(text: str) -> tuple[str, bool]:
    """ "tamom" / "boshla" as the whole message, its own line or its own last sentence closes the description; the
    word itself is not part of it. "…bazasidan boshla" (a word before it) is description."""
    match = _CLOSE.search(text)
    if match is None:
        return text, False
    head = text[: match.start()]
    if match.group(0)[:1] in {".", "!", "?"}:
        head += match.group(0)[0]  # the sentence before the close word keeps its full stop
    return head.rstrip(), True


@dataclass(frozen=True, slots=True)
class Burst:
    """A closed burst as the draft reads it: its description part (None when it held only the close word) and
    whether it closes the description."""

    part: DraftPart | None
    closes: bool = False


def part_of(item: TurnInput) -> Burst:
    """The burst's parts in arrival order, one paragraph each, labelled as the agent reads them ("[voice note,
    transcribed] …", "[image]"). Only the last part may close the description."""
    paragraphs: list[str] = []
    closes = False
    last = len(item.parts) - 1
    for index, burst_part in enumerate(item.parts):
        words = burst_part.text
        if index == last and words is not None:
            words, closes = split_close(words)
        line = turns_domain.part_text(burst_part.kind, words, burst_part.transcript)
        if closes and not (words or "").strip():
            line = ""  # the close word alone: a voice note saying "tamom" adds no "[voice note]" paragraph
        if line:
            paragraphs.append(line)
    text = "\n\n".join(paragraphs)[:PART_MAX]
    part = DraftPart(text=text, at=item.last_part_at, burst_id=item.burst_id) if text else None
    return Burst(part=part, closes=closes)


def description(parts: Iterable[DraftPart]) -> str:
    """The owner's whole description, in arrival order, for the plan prompt."""
    return "\n\n".join(part.text for part in parts if part.text.strip())[:DESCRIPTION_MAX]


def has_content(parts: Iterable[DraftPart]) -> bool:
    """More than "yangi loyiha": at least one word that is not a trigger or filler word."""
    return any(_content_words(_latin(part.text)) for part in parts)


def silent(last_part_at: datetime, now: datetime, *, silence_s: float) -> bool:
    """`newproject.draft_silence_s` without a new part closes the description (UC-14: 2 minutes)."""
    return now - last_part_at >= timedelta(seconds=silence_s)


# --- the owner's words ----------------------------------------------------------------------------------------------
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ’‘`´", "'"))  # noqa: RUF001  # the variants owners type
_CANCEL_PHRASES: Final = frozenset({"bekor qil", "bekor qiling", "bekor qilamiz", "bekor qilaylik", "kerak emas"})
_REVISE: Final = re.compile(r"(?:^|\s)o'?zgartir(?:ing|ib)?\b\s*[:,\-–—]?\s*", re.IGNORECASE)


def _folded(text: str) -> str:
    return " ".join(re.sub(r"[.!?,]+", " ", text.translate(_APOSTROPHES).casefold()).split())


def is_cancel(text: str, *, names: Iterable[str] = ()) -> bool:
    """A bare "yo'q" (the gate's denial words) or "bekor qil": the owner drops the new project."""
    if gate_domain.reply_verdict(text, names=names) is OwnerVerdict.DECLINE:
        return True
    return _folded(text) in _CANCEL_PHRASES


class PlanVerdict(StrEnum):
    APPROVE = "approve"
    CANCEL = "cancel"
    REVISE = "revise"


@dataclass(frozen=True, slots=True)
class PlanAnswer:
    verdict: PlanVerdict
    feedback: str = field(default="", repr=False)  # REVISE: what to change ("" = he did not say)


def plan_answer(text: str, *, names: Iterable[str] = ()) -> PlanAnswer:
    """The owner's answer to "Rejani tasdiqlaysizmi?". "ha" → build; "o'zgartir: X" → revise with X; a bare "yo'q" →
    cancel; anything else ("ha, lekin Django bo'lsin", "yo'q, mobil ham kerak", plain words) is what to change."""
    normalized = text.translate(_APOSTROPHES)
    if (match := _REVISE.search(normalized)) is not None:
        return PlanAnswer(PlanVerdict.REVISE, normalized[match.end() :].strip())
    if is_cancel(text, names=names):
        return PlanAnswer(PlanVerdict.CANCEL)
    if gate_domain.reply_verdict(text, names=names) is OwnerVerdict.APPROVE:
        return PlanAnswer(PlanVerdict.APPROVE)
    return PlanAnswer(PlanVerdict.REVISE, text.strip())


# --- the name (ASSUMPTIONS 2026-10-23: PascalCase Latin; a collision takes the first free <Name>2) -----------------
# Uzbek Cyrillic (and Russian) → the Uzbek Latin alphabet, lower case; `cyrillic:latin` pairs.
_CYRILLIC_PAIRS: Final = (
    "а:a б:b в:v г:g д:d е:e ё:yo ж:j з:z и:i й:y к:k л:l м:m н:n о:o п:p р:r с:s т:t у:u ф:f х:x "  # noqa: RUF001
    "ц:ts ч:ch ш:sh щ:sh ъ:' ь: ы:i э:e ю:yu я:ya ў:o' қ:q ғ:g' ҳ:h"
)
_CYRILLIC: Final[Mapping[str, str]] = {
    key: value for key, _, value in (pair.partition(":") for pair in _CYRILLIC_PAIRS.split())
}
_QUOTED: Final = re.compile(r"[«“\"]([^«»“”\"\n]{1,60})[»”\"]")
_NAMED_AFTER: Final = re.compile(r"\b(?:nomi|name|named|called)\b\s*[:=\-–—]?\s*([^\n.,;!?()]+)", re.I)
_NAMED_BEFORE: Final = re.compile(r"([A-Za-z0-9']+)\s+nomli\b", re.I)
_WORD: Final = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)*")
# Trigger and filler words of "yangi loyiha ochamiz: …" that never name a project (compared without apostrophes).
_STOP_TEXT: Final = (
    "yangi loyiha loyihasi loyihani loyihamiz loyihaga project new uchun bir ochamiz ochaylik ochish och oching "
    "qilamiz qilaylik qilish qil qiling kerak kerakli menga bizga men biz bu shu ham va bilan the a an for of to "
    "and please iltimos keling boshla boshlang tamom nomi nomli bolsin boladi deb yarat yaratamiz yaratish "
    "yarating tuz tuzamiz lets let create build make start ni da ga yoz yozamiz "
    "voice note transcribed understood not image file video audio link"  # the labels of `turns.part_text`
)
_STOP_WORDS: Final = frozenset(_STOP_TEXT.split())


def _latin(text: str) -> str:
    """Cyrillic (Uzbek and Russian) transliterated, accents dropped, apostrophe variants made one."""
    out: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        lower = char.lower()
        mapped = _CYRILLIC.get(lower)
        if mapped is None:
            out.append(char)
        else:
            out.append(mapped[:1].upper() + mapped[1:] if char != lower else mapped)
    decomposed = unicodedata.normalize("NFKD", "".join(out))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).translate(_APOSTROPHES)


def _bare(word: str) -> str:
    return word.replace("'", "")


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _content_words(text: str) -> list[str]:
    return [word for word in _words(text) if _bare(word).casefold() not in _STOP_WORDS and not word.isdigit()]


def _pascal(word: str) -> str:
    """ "kitob" → "Kitob"; "TezTaxi", "BonviHR", "CRM" keep their capitals; "KUTUBXONA" → "Kutubxona"."""
    bare = _bare(word)
    if bare.isupper() and len(bare) > 5:  # a shouted word, not an acronym
        return bare.capitalize()
    if any(char.isupper() for char in bare[1:]):
        return bare[:1].upper() + bare[1:]
    return bare.capitalize()


def _assemble(words: Sequence[str]) -> str:
    """Up to `NAME_WORDS` words, PascalCase, within `NAME_MAX`; a name starts with a letter ("3d printer" →
    "Printer"), and one word longer than the limit is cut."""
    usable = list(dropwhile(lambda word: not word[:1].isalpha(), words))
    name = ""
    for word in usable[:NAME_WORDS]:
        piece = _pascal(word)
        if len(name) + len(piece) > NAME_MAX:
            break
        name += piece
    if not name and usable:
        name = _pascal(usable[0])[:NAME_MAX]
    return name


def propose_name(text: str) -> str:
    """The folder name from the owner's own words: a quoted name («Oila Byudjeti»), else the words after "nomi:" or
    before "nomli", else the first words of the description that are not "yangi loyiha" filler. PascalCase, Latin
    letters and digits only, at most `NAME_MAX` characters, never empty."""
    plain = _latin(text)
    candidates: list[list[str]] = []
    if (quoted := _QUOTED.search(plain)) is not None and 1 <= len(found := _words(quoted.group(1))) <= NAME_WORDS + 1:
        candidates.append(found)
    if (after := _NAMED_AFTER.search(plain)) is not None:
        candidates.append(_content_words(after.group(1)))
    if (before := _NAMED_BEFORE.search(plain)) is not None:
        candidates.append([before.group(1)])
    candidates.append(_content_words(plain))
    for words in candidates:
        if words and (name := _assemble(words)):
            return name
    return FALLBACK_NAME


def name_candidates(wanted: str, taken: Collection[str]) -> Iterator[str]:
    """`wanted`, then `<wanted>2`, `<wanted>3`, …, skipping every name in `taken` (compared case-insensitively, as
    macOS and GitHub compare them)."""
    folded = {name.casefold() for name in taken}
    if wanted.casefold() not in folded:
        yield wanted
    number = 2
    while True:
        candidate = f"{wanted}{number}"
        if candidate.casefold() not in folded:
            yield candidate
        number += 1


# --- which draft a message belongs to (routing step 4) --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OpenDraft:
    draft_id: int
    status: DraftStatus
    project_id: str | None  # None until the folder exists


@dataclass(frozen=True, slots=True)
class Pick:
    """`draft_id`: the draft that takes the message. `choices`: several could, so the owner is asked which."""

    draft_id: int | None = None
    choices: tuple[str, ...] = ()


_PROJECT_TRIGGER: Final = re.compile(r"\W*(?:yangi\s+loyiha|new\s+project)\b", re.IGNORECASE)


def starts_project(text: str) -> bool:
    """ "Yangi loyiha: …" (or "янги лойиҳа", "new project") at the start of a message: the owner opens a project."""
    return _PROJECT_TRIGGER.match(_latin(text)) is not None


def pick_draft(  # noqa: PLR0911  # one return per rule
    drafts: Sequence[OpenDraft],
    *,
    named: Collection[str] = (),
    targeted: Collection[str] = (),
    reply_project: str | None = None,
    starts_new: bool = False,
) -> Pick | None:
    """The draft an owner message belongs to, or None when it is not a draft's (no draft is open, or another project
    is named: routing takes it).

    - "Yangi loyiha: …" (`starts_new`) opens another project: only a description still being collected takes it, so
      a waiting plan never mistakes it for what to change.
    - A message naming a draft's project, or replying to its message, is that draft's.
    - While a description is being collected, naming another project in passing ("BonviHR kabi") stays in it; only
      work aimed at another project ("BonviHR'da …", a reply to its message) leaves.
    - Otherwise another named project leaves; an un-named message goes to the one open draft, and with several the
      owner is asked which.
    """
    open_drafts = [draft for draft in drafts if draft.status in OPEN]
    ours = {draft.project_id for draft in open_drafts if draft.project_id is not None}
    collecting = next((draft for draft in open_drafts if draft.status is DraftStatus.COLLECTING), None)
    if starts_new:
        return Pick(draft_id=collecting.draft_id) if collecting is not None else None
    signals = {*named, *([reply_project] if reply_project else [])}
    mine = [draft for draft in open_drafts if draft.project_id in signals]
    if len(mine) == 1:
        return Pick(draft_id=mine[0].draft_id)
    if mine:
        return Pick(choices=tuple(sorted(draft.project_id for draft in mine if draft.project_id)))
    aimed_elsewhere = {*targeted, *([reply_project] if reply_project else [])} - ours
    if collecting is not None and not aimed_elsewhere:
        return Pick(draft_id=collecting.draft_id)
    if signals - ours:
        return None
    rest = [draft for draft in open_drafts if draft.status is not DraftStatus.COLLECTING]
    if len(rest) == 1:
        return Pick(draft_id=rest[0].draft_id)
    if rest:
        return Pick(choices=tuple(sorted(draft.project_id or "?" for draft in rest)))
    return None


# --- plan documents -------------------------------------------------------------------------------------------------
def plan_docs(names: Iterable[str]) -> tuple[str, ...]:
    """The `docs/*.md` files sent with the plan, by name: the first `DOCS_MAX` in name order."""
    found = sorted({name for name in names if name.endswith(DOC_SUFFIX) and not name.startswith(".")})
    return tuple(found[:DOCS_MAX])


# --- what the owner reads ---------------------------------------------------------------------------------------------
def collecting_text(*, silence_s: float) -> BotText:
    return S.NEWPROJECT_COLLECTING(minutes=max(1, math.ceil(silence_s / 60)))


def planning_text(
    project: str,
    *,
    path: str,
    wanted: str | None,
    plan_max_min: int,
    repo_url: str | None,
    repo_problem: BotText | None,
) -> BotText:
    """The folder is there and the plan is being written: where, under which name (said when it differs from the
    one asked for), and where the GitHub repository is."""
    lines = [S.NEWPROJECT_PLANNING(project=project, path=path, minutes=plan_max_min)]
    if wanted is not None and wanted != project:
        lines.append(S.NEWPROJECT_RENAMED(wanted=wanted, project=project))
    if repo_url is not None:
        lines.append(S.NEWPROJECT_REPO(url=repo_url))
    elif repo_problem is not None:
        lines.append(repo_problem)
    return join(lines)


def plan_ready_text(project: str, *, natija: str | None, docs: Sequence[str], repo_url: str | None) -> BotText:
    """SPEC §3.2.8 step 5: the summary, the documents that follow, and the one question."""
    summary = natija.strip() if natija and natija.strip() else S.TURN_RESULT_NO_NATIJA()
    lines = [S.NEWPROJECT_PLAN_READY(project=project, summary=summary)]
    lines.append(S.NEWPROJECT_PLAN_DOCS(names=", ".join(docs)) if docs else S.NEWPROJECT_PLAN_NO_DOCS())
    if repo_url is not None:
        lines.append(S.NEWPROJECT_REPO(url=repo_url))
    lines.append(S.NEWPROJECT_PLAN_ASK())
    return join(lines)


def which_text(choices: Sequence[str]) -> BotText:
    return S.NEWPROJECT_WHICH(projects=", ".join(choices))


# --- retention ------------------------------------------------------------------------------------------------------
def draft_cutoff(now: datetime) -> datetime:
    """A finished draft is deleted once it closed strictly before this."""
    return now - DRAFT_KEEP
