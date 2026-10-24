"""Routing rules, pure (SPEC §3.2.6, §6.1 step 3, §4.2 `route_decisions` / `clarifications`; UC-10, UC-02, UC-05).

The owner talks in one flat group with no Topics and rarely replies: he names a project, or just describes the work.
A closed burst (`TurnInput`) is routed by these signals, in this order:

1. **Names** (`find_mentions`, no LLM): a folder name or an alias, with Uzbek suffixes ("BonviHR'da", "hrmentorga"),
   or one typo away for words of ≥ 6 letters. Only `Project.routable` projects match loosely; archived and `*-wt-*`
   projects answer only to their exact name, and a near miss of one ("HRMentor wt agent api") is a look-alike, so the
   bot asks. An exact name beats a typo, a typo that fits two names asks, and a digit is never a typo
   (`BonviHR` ≠ `BonviHR2`). A short name that is also a word ("AI", "BS") counts only with a suffix ("AI'da").
2. **Reply** to a bot message linked to a project (`message_links`), unless another project is named.
3. **Recent context**: the project the owner spoke about last, the one that reported last, and running turns, within
   `routing.context_window_min`. Context informs the LLM and is its fallback; it never routes against the content.
4. **LLM** (`routing` kind, fast tier, `OUTPUT_SCHEMA`): `{intent, project_id, candidates, confidence, needs_fresh,
   is_new_project}`. Its pick must come from the allowed set: the named projects when several are named, a family
   (`Wunderkind*`) when one word starts several names, else the routable projects.

`decide` routes when the pick clears `routing.min_confidence` and leads the next candidate by `routing.min_margin`,
else it asks exactly one question naming ≤ 3 candidates (`question`). `with_follow_up` runs
`burst.domain.decide_dev_follow_up` first, so a continuation of a running turn is merged, never asked about (UC-02).
`choose` reads the owner's answer ("2", "ikkinchisi", a name, voice); `wrong_target_of` reads "bu X uchun edi".
"""

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import json_value
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText
from yordamchi.dev.projects.domain import Project, name_key, shorten
from yordamchi.shared.burst.domain import (
    RouteHint,
    RunningTurn,
    TranscriptState,
    TurnInput,
    WindowSettings,
    decide_dev_follow_up,
    window_settings,
)
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot

EXPLICIT_CONFIDENCE: Final = 1.0  # SPEC §3.2.6 step 1
TYPO_CONFIDENCE: Final = 0.9  # one typo away, or another spelling: still routes and may steer (≥ merge 0.85)
REPLY_CONFIDENCE: Final = 0.95  # step 2
CONTEXT_CONFIDENCE: Final = 0.8  # the LLM is unavailable and one project is active: routes, never steers on its own
TYPO_MIN_CHARS: Final = 6  # "Damerau-Levenshtein ≤ 1 for tokens of ≥ 6 chars"
SHORT_NAME_MAX: Final = 3  # "AI", "BS": words as much as names
FAMILY_MIN_CHARS: Final = 5  # "bonvi", "wunderkind", "megaplus": a word that starts several names
NEAR_WINDOW: Final = 4  # words read together for a name written apart ("bonvi hr", "HRMentor wt agent api")
MAX_CANDIDATES: Final = 3  # SPEC §3.2.6 step 5: candidates[≤ 3]
MIN_CANDIDATE_CONFIDENCE: Final = 0.05  # an LLM alternative below this is not worth naming
# SPEC §4.2 `clarifications`: "expires 30 min". No §4.3 key yet (`routing.clarify_expire_min` is proposed).
CLARIFICATION_TTL: Final = timedelta(minutes=30)
# "bu X uchun edi" corrects the latest route at most this old: a turn may run up to `turns.max_wall_min` (60) and the
# owner notices at its result. No §4.3 key yet (`routing.correct_window_min` is proposed).
CORRECTION_WINDOW: Final = timedelta(hours=2)
ROUTE_KEEP: Final = timedelta(days=90)  # SPEC §4.2 `route_decisions`: 90 d
CLARIFICATION_KEEP: Final = timedelta(days=30)  # `clarifications`: row 30 d
EXCERPT_MAX: Final = 60  # the material a question quotes (UC-02 AC)
VOICE_ECHO_MAX: Final = 300  # UC-05: the transcript echoed in one line with the routed project
LLM_TEXT_MAX: Final = 2000  # characters of one part sent to the routing LLM
LLM_DESCRIPTION_MAX: Final = 160
# T12 measured a cold routing ask at default effort at 7 s. No §4.3 key yet (`models.routing_effort` is proposed).
ROUTING_EFFORT: Final = "low"


class Method(StrEnum):
    """`route_decisions.method` (SPEC §4.2)."""

    EXPLICIT = "explicit"  # a name or an alias in the message
    REPLY = "reply"  # a reply to a project's bot message
    BINDING = "binding"  # an answer bound to an open item: here, the answer to a clarification
    DRAFT = "draft"  # an open new-project draft took the message (P2)
    LLM = "llm"
    CONTEXT = "context"  # the LLM was unavailable; the one active project took it


class Intent(StrEnum):
    INSTRUCT = "instruct_project"  # work in a project (UC-11)
    REPORT = "report_project"  # a question about one project's state (UC-15 fast path)
    REPORT_ALL = "report_all"
    INVESTIGATE = "investigate"  # read-only diagnosis; a lone error screenshot
    SERVER_STATUS = "server_status"
    NEW_PROJECT = "new_project"  # P2 draft
    ANSWER = "answer_clarification"  # the burst answered a clarification (method `binding`)
    WRONG_TARGET = "wrong_target"  # "bu X uchun edi" (UC-10 F)


LLM_INTENTS: Final = (
    Intent.INSTRUCT,
    Intent.REPORT,
    Intent.REPORT_ALL,
    Intent.INVESTIGATE,
    Intent.SERVER_STATUS,
    Intent.NEW_PROJECT,
)
# Only an instruction joins a running turn: a report never touches a project session (UC-15), an investigation has
# its own read-only one.
STEERABLE: Final = frozenset({Intent.INSTRUCT})
# Intents that do not name the decided-on route of work (context and corrections skip them).
NOT_WORK: Final = frozenset({Intent.ANSWER, Intent.WRONG_TARGET})


class Outcome(StrEnum):
    ROUTE = "route"  # to `project_id`: a new turn, queued behind a running one if needed
    MERGE = "merge"  # into the running turn of `project_id` as steering (`deliveries.mode=next`, UC-02)
    ASK = "ask"  # one clarification naming `candidates`
    NO_PROJECT = "no_project"  # report_all, server_status, new_project: nothing to route to


class ClarificationKind(StrEnum):
    WHICH_PROJECT = "which_project"
    WHICH_ITEM = "which_item"
    WHICH_RUNNING = "which_running"
    WHICH_DECISION = "which_decision"


class ClarificationStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class ActivityKind(StrEnum):
    SPOKEN = "spoken"  # the owner's message was routed there
    REPORTED = "reported"  # the project's bot message went out (a result, a question)
    RUNNING = "running"  # a turn works there now


# --- text -----------------------------------------------------------------------------------------------------------
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ’‘`\u00b4", "'"))  # \u00b4: acute accent
_WORD: Final = re.compile(r"[^\s,;:!?()\[\]{}\"«»“”„<>|/\\]+")
_WORD_EDGES: Final = ".'-_*~"
_SEPARATORS: Final = re.compile(r"[\s\-_.']+")
_DIGITS: Final = re.compile(r"\d+")
# Uzbek case endings and particles, longest first. Stacked endings are listed whole ("dagini" = da + gi + ni).
_SUFFIXES: Final = (
    "nikidagi",
    "larning",
    "dagilar",
    "dagini",
    "larni",
    "larga",
    "larda",
    "dagi",
    "ning",
    "niki",
    "gacha",
    "dan",
    "dek",
    "day",
    "cha",
    "lar",
    "da",
    "ga",
    "ka",
    "qa",
    "ni",
    "mi",
    "chi",
)
_UNQUOTED_STEM_MIN: Final = 5  # "hrmentorga" → "hrmentor", but never "aida" → "ai"


def fold(text: str) -> str:
    """How routing compares text: NFC, case-folded, one apostrophe (ʻ ʼ ’ ‘ ` → '), single spaces."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().translate(_APOSTROPHES).split())


def compact(text: str) -> str:
    """`text` without spaces, hyphens, dots, underscores and apostrophes: `MegaPlus ERP` → `megapluserp`."""
    return _SEPARATORS.sub("", fold(text))


def tokens(text: str) -> tuple[str, ...]:
    """The words of `text`, folded. Hyphens, dots and apostrophes stay inside a word, so `HRMentor-wt-agent-api` and
    `BonviHR'da` are one word each."""
    found = (match.group(0).strip(_WORD_EDGES) for match in _WORD.finditer(fold(text)))
    return tuple(token for token in found if token)


def typo_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (optimal string alignment): insertions, deletions, substitutions and swaps of two
    neighbours. Names are short (≤ 64), so the full table is cheap."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[len(b)]


def within_one_typo(a: str, b: str) -> bool:
    """One insertion, deletion, substitution or swap of two neighbours at most (SPEC: "Damerau-Levenshtein ≤ 1")."""
    return abs(len(a) - len(b)) <= 1 and typo_distance(a, b) <= 1


@dataclass(frozen=True, slots=True)
class _Stem:
    text: str
    marked: bool  # its suffix was set off by an apostrophe or a hyphen ("AI'da", "BonviHR-ga")


def _stems(token: str) -> tuple[_Stem, ...]:
    """The word, then the word without an Uzbek suffix: after an apostrophe or a hyphen any suffix, written on
    (unquoted) only a known one, and only when at least `_UNQUOTED_STEM_MIN` letters remain."""
    found = [_Stem(token, marked=False)]
    head, quote, tail = token.partition("'")
    if quote and head and tail.isalpha():
        found.append(_Stem(head, marked=True))
    head, dash, tail = token.rpartition("-")
    if dash and head and tail in _SUFFIXES:
        found.append(_Stem(head, marked=True))
    if "'" not in token:
        found.extend(
            _Stem(token[: -len(suffix)], marked=False)
            for suffix in _SUFFIXES
            if token.endswith(suffix) and len(token) - len(suffix) >= _UNQUOTED_STEM_MIN
        )
    unique: dict[str, _Stem] = {}
    for stem in found:
        unique.setdefault(stem.text, stem)
    return tuple(unique.values())


# --- names (signal 1) -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Name:
    project_id: str
    words: tuple[str, ...]
    compact: str
    short: bool  # a folder name of ≤ SHORT_NAME_MAX letters: a bare mention is only a hint
    loose: bool  # routable: a typo, a split spelling or a family word may reach it
    exact_only: bool  # archived or a worktree: only its exact name; a near miss asks


def _names(projects: Iterable[Project]) -> list[_Name]:
    names: list[_Name] = []
    for project in projects:
        exact_only = project.exact_name_only and not project.missing
        if words := tokens(project.id):
            short = len(compact(project.id)) <= SHORT_NAME_MAX
            names.append(_Name(project.id, words, compact(project.id), short, project.routable, exact_only))
        # An alias is the owner's own word for the project, so even a short one counts bare.
        names.extend(
            _Name(project.id, alias_words, compact(alias), False, project.routable, exact_only)
            for alias in project.aliases
            if (alias_words := tokens(alias))
        )
    return names


def _digits(text: str) -> tuple[str, ...]:
    return tuple(_DIGITS.findall(text))


def _typo_of(text: str, name: _Name, *, edits: int = 1) -> bool:
    """At most `edits` typos from `name`, never across a digit: `BonviHR2` and `BonviHR3` are different projects."""
    return (
        len(text) >= TYPO_MIN_CHARS
        and len(name.compact) >= TYPO_MIN_CHARS
        and _digits(text) == _digits(name.compact)
        and abs(len(text) - len(name.compact)) <= edits
        and typo_distance(text, name.compact) <= edits
    )


class _Hit(StrEnum):
    NAMED = "named"
    TYPO = "typo"
    WEAK = "weak"
    FAMILY = "family"
    LOOKALIKE = "lookalike"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Mentions:
    """Which projects a text names, each list in text order and distinct."""

    named: tuple[str, ...] = ()  # named outright: exact name or alias (suffixes allowed), or one typo away
    typo: tuple[str, ...] = ()  # those of `named` reached through a typo or another spelling
    weak: tuple[str, ...] = ()  # maybe meant: a bare short name, a name written apart, the one name a word starts
    family: tuple[str, ...] = ()  # one word starts several names ("Wunderkind"): one of these, unsaid which
    lookalike: tuple[str, ...] = ()  # an exact-only project almost named ("HRMentor wt"): ask, never route
    ambiguous: tuple[str, ...] = ()  # one word is one typo from two names: ask

    @property
    def hints(self) -> tuple[str, ...]:
        """`weak`, then `family`: a name written apart says more than a family word."""
        return _distinct([*self.weak, *self.family])


def _distinct(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _window_compacts(words: Sequence[str]) -> tuple[str, ...]:
    """`words` joined without separators, once per stem of the last word ("bonvi hr'da" → "bonvihrda", "bonvihr")."""
    if not words:
        return ()
    head = compact("".join(words[:-1]))
    return _distinct(head + compact(stem.text) for stem in _stems(words[-1]))


def _exact_hit(window: Sequence[str], name: _Name) -> _Hit | None:
    """`window` is `name` word for word, the last word maybe with a suffix. A bare short folder name is weak."""
    if len(window) != len(name.words) or tuple(window[:-1]) != name.words[:-1]:
        return None
    for stem in _stems(window[-1]):
        if stem.text == name.words[-1]:
            return _Hit.WEAK if name.short and not stem.marked else _Hit.NAMED
    return None


def _lookalike(window: Sequence[str], name: _Name, bases: Collection[str]) -> bool:
    """`window` nearly names exact-only `name` without naming it: another spelling, one typo, or a routable name
    (`bases`) written on into it ("HRMentor wt" → `HRMentor-wt-agent-api`, "BonviCall clone" → `… Clone Archive`)."""
    if _exact_hit(window, name) is not None:
        return False
    for text in _window_compacts(window):
        if text == name.compact or _typo_of(text, name):
            return True
        extends = any(text.startswith(base) and len(text) > len(base) for base in bases)
        if len(window) >= 2 and extends and name.compact.startswith(text):
            return True
    return False


class _Scan:
    """One text against the registry. Each method is one matching rule; `find_mentions` runs them in order. A strong
    hit takes its words, so a later, looser rule never reads them again."""

    def __init__(self, words: tuple[str, ...], names: list[_Name]) -> None:
        self.words = words
        self.names = names
        self.loose = [name for name in names if name.loose]
        self.bases = frozenset(name.compact for name in self.loose)
        self.taken = [False] * len(words)
        self.hits: list[tuple[int, _Hit, str]] = []

    def _windows(self, *, free: bool, min_size: int = 1) -> Iterator[tuple[int, tuple[str, ...]]]:
        """Every run of up to `NEAR_WINDOW` words; `free`: only runs no strong hit has taken."""
        for start in range(len(self.words)):
            for size in range(1, min(NEAR_WINDOW, len(self.words) - start) + 1):
                if free and any(self.taken[start : start + size]):
                    break
                if size >= min_size:
                    yield start, self.words[start : start + size]

    def _take(self, start: int, size: int) -> None:
        self.taken[start : start + size] = [True] * size

    def lookalikes(self) -> None:
        """An exact-only project almost named, anywhere, even over words a routable name will take."""
        exact_only = [name for name in self.names if name.exact_only]
        for start, window in self._windows(free=False):
            self.hits.extend(
                (start, _Hit.LOOKALIKE, name.project_id) for name in exact_only if _lookalike(window, name, self.bases)
            )

    def exact(self) -> None:
        """Folder names and aliases word for word, the longest first ("BonviCall Clone Archive" before "BonviCall")."""
        for name in sorted(self.names, key=lambda item: -len(item.words)):
            size = len(name.words)
            for start in range(len(self.words) - size + 1):
                if any(self.taken[start : start + size]):
                    continue
                hit = _exact_hit(self.words[start : start + size], name)
                if hit is not None:
                    self.hits.append((start, hit, name.project_id))
                    if hit is _Hit.NAMED:
                        self._take(start, size)

    def _one_typo(self, stems: Sequence[str]) -> tuple[str, ...]:
        return _distinct(name.project_id for name in self.loose if any(_typo_of(stem, name) for stem in stems))

    def _cut_short(self, stems: Sequence[str]) -> tuple[str, ...]:
        """Names a word may be the cut-off start of, two typos away ("bonviimpo" → `BonviImport`)."""
        return _distinct(
            name.project_id
            for name in self.loose
            if any(name.compact.startswith(stem) and _typo_of(stem, name, edits=2) for stem in stems)
        )

    def single_words(self) -> None:
        """One word that is a routable name spelled otherwise ("megapluserp", "Wunderkind-LC"), else one typo of one
        ("hrmentr"). Another spelling beats a typo. A typo fitting two names is ambiguous, and so is one whose word is
        also a name cut short: "bonviimpo" is one typo from `BonviImzo` and the start of `BonviImport`."""
        for start, word in enumerate(self.words):
            if self.taken[start]:
                continue
            stems = _window_compacts([word])
            same = _distinct(name.project_id for name in self.loose if not name.short and name.compact in stems)
            typos = self._one_typo(stems) if not same else ()
            found = same or (_distinct([*typos, *self._cut_short(stems)]) if typos else ())
            if found:
                kind = _Hit.TYPO if len(found) == 1 else _Hit.AMBIGUOUS
                self.hits.extend((start, kind, project) for project in found)
                self._take(start, 1)

    def _starts_family(self, word: str) -> bool:
        head = compact(word)
        return len(head) >= FAMILY_MIN_CHARS and sum(1 for name in self.loose if name.compact.startswith(head)) > 1

    def written_apart(self) -> None:
        """A multi-word name with one typo ("megaplus platfrom"), or a name written as several words. Written apart it
        is named when its first word starts a family ("Wunderkind LMS", "Bonvi Call"), else only a hint ("ai phone",
        "russian speaking": ordinary words too)."""
        for start, window in self._windows(free=True, min_size=2):
            if any(self.taken[start : start + len(window)]):
                continue
            joined = _window_compacts(window)
            for name in self.loose:
                one_typo = len(name.words) == len(window) and any(_typo_of(text, name) for text in joined)
                if one_typo or (name.compact in joined and self._starts_family(window[0])):
                    self.hits.append((start, _Hit.TYPO, name.project_id))
                    self._take(start, len(window))
                    break
                if name.compact in joined:
                    self.hits.append((start, _Hit.WEAK, name.project_id))

    def prefixes(self) -> None:
        """A word that starts routable names: several is a family ("Wunderkind"), one is a hint ("Claude")."""
        for start, word in enumerate(self.words):
            if self.taken[start]:
                continue
            for stem in _stems(word):
                head = compact(stem.text)
                if len(head) < FAMILY_MIN_CHARS:
                    continue
                starts = _distinct(n.project_id for n in self.loose if n.compact.startswith(head) and n.compact != head)
                if starts:
                    kind = _Hit.FAMILY if len(starts) > 1 else _Hit.WEAK
                    self.hits.extend((start, kind, project) for project in starts)
                    break

    def mentions(self) -> Mentions:
        ordered = sorted(self.hits, key=lambda hit: hit[0])

        def of(*kinds: _Hit) -> tuple[str, ...]:
            return _distinct(project for _, kind, project in ordered if kind in kinds)

        named = of(_Hit.NAMED, _Hit.TYPO)
        exact = set(of(_Hit.NAMED))
        return Mentions(
            named=named,
            typo=tuple(project for project in of(_Hit.TYPO) if project not in exact),
            weak=tuple(project for project in of(_Hit.WEAK) if project not in named),
            family=tuple(project for project in of(_Hit.FAMILY) if project not in named),
            # Named exactly somewhere ("BonviCall Clone Archive'dagi"), a project is no near miss of itself.
            lookalike=tuple(project for project in of(_Hit.LOOKALIKE) if project not in exact),
            ambiguous=of(_Hit.AMBIGUOUS),
        )


def find_mentions(text: str, projects: Iterable[Project]) -> Mentions:
    """The projects `text` names (signal 1). In order: look-alikes of exact-only projects; exact names and aliases;
    one word spelled otherwise or with one typo; names written apart; words that start names. Archived, worktree and
    missing projects match their exact name only."""
    scan = _Scan(tokens(text), _names(projects))
    scan.lookalikes()
    scan.exact()
    scan.single_words()
    scan.written_apart()
    scan.prefixes()
    return scan.mentions()


# --- context (signals 2 and 3) --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Activity:
    project_id: str
    at: datetime
    kind: ActivityKind


@dataclass(frozen=True, slots=True)
class RouteFacts:
    """What other features know about a burst, gathered by the router before `route_burst` (T24/T34)."""

    reply_projects: Mapping[int, str] = field(default_factory=dict)  # bot message id → project (`message_links`)
    reported: tuple[Activity, ...] = ()  # the latest bot message per project (results, questions)
    running: tuple[RunningTurn, ...] = ()  # dev turns working now (`turns`)
    open_questions: tuple[str, ...] = ()  # projects waiting for the owner's answer
    others_pending: bool = False  # a question or decision is open: a bare "ha" is not a clarification answer


NO_FACTS: Final = RouteFacts()


@dataclass(frozen=True, slots=True)
class PastRoute:
    """One stored `route_decisions` row as the domain sees it."""

    route_id: int
    burst_id: int | None
    at: datetime
    method: Method
    intent: Intent
    project_id: str | None
    corrected_to: str | None
    asked: bool

    @property
    def project(self) -> str | None:
        """Where the work really belongs: the correction, else the route."""
        return self.corrected_to or self.project_id


def spoken_activity(past: Iterable[PastRoute]) -> tuple[Activity, ...]:
    """The owner's recent messages, as activity of the project each went to."""
    return tuple(Activity(route.project, route.at, ActivityKind.SPOKEN) for route in past if route.project is not None)


def recent_activity(
    activity: Iterable[Activity], running: Iterable[RunningTurn], *, now: datetime, window: timedelta
) -> tuple[Activity, ...]:
    """The latest activity of each (project, kind) within `window` of `now`, most recent first. A running turn is
    active now."""
    latest: dict[tuple[str, ActivityKind], Activity] = {}
    for item in [*activity, *(Activity(t.project_id, now, ActivityKind.RUNNING) for t in running if t.project_id)]:
        key = (item.project_id, item.kind)
        if now - item.at <= window and (key not in latest or item.at > latest[key].at):
            latest[key] = item
    return tuple(sorted(latest.values(), key=lambda item: (now - item.at, item.project_id, item.kind.value)))


def recent_projects(
    activity: Iterable[Activity], running: Iterable[RunningTurn], *, now: datetime, window: timedelta
) -> tuple[str, ...]:
    """Projects active within `window` of `now`, most recent first; a running turn is active now."""
    return _distinct(item.project_id for item in recent_activity(activity, running, now=now, window=window))


def reply_project(item: TurnInput, reply_projects: Mapping[int, str], known: Collection[str]) -> str | None:
    """The project the burst replies to (signal 2), when its replies point at exactly one registered project."""
    found = {
        reply_projects[part.reply_to_message_id]
        for part in item.parts
        if part.reply_to_message_id is not None and part.reply_to_message_id in reply_projects
    }
    only = next(iter(found)) if len(found) == 1 else None
    return only if only in known else None


# --- settings -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RouteSettings:
    """The ROUTING settings of one routing pass (SPEC §4.3), read once from the snapshot."""

    min_confidence: float
    min_margin: float
    context_window: timedelta
    merge_min_confidence: float
    window: WindowSettings  # the burst windows, for `decide_dev_follow_up`
    model: str
    timeout_s: float


def route_settings(snap: Snapshot) -> RouteSettings:
    return RouteSettings(
        min_confidence=snap[defaults.ROUTING_MIN_CONFIDENCE],
        min_margin=snap[defaults.ROUTING_MIN_MARGIN],
        context_window=timedelta(minutes=snap[defaults.ROUTING_CONTEXT_WINDOW_MIN]),
        merge_min_confidence=snap[defaults.BURST_MERGE_MIN_CONFIDENCE],
        window=window_settings(snap),
        model=snap[defaults.MODELS_ROUTING],
        timeout_s=snap[defaults.TIMEOUTS_ROUTING_S],
    )


# --- the plan (deterministic signals) -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Candidate:
    project_id: str
    confidence: float


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """What the deterministic signals settled before the LLM is asked."""

    mentions: Mentions
    reply_project: str | None
    recent: tuple[str, ...]  # every project active within the window, most recent first
    context: tuple[str, ...]  # those of `recent` that content routing may pick
    shadowed: tuple[str, ...]  # exact-only projects in `recent`: a look-alike of one must not take a follow-up
    allowed: frozenset[str]  # what the LLM may pick: `pool` when set, else the routable projects
    fixed: Candidate | None = None  # the route the names or the reply decided
    fixed_method: Method | None = None
    ask_first: tuple[str, ...] = ()  # a look-alike or a typo between two names: ask these, whatever the LLM says
    ask_reason: str = ""
    pool: tuple[str, ...] = ()  # the LLM must choose among these (several named, a reply against a name, a family)
    media_only: bool = False

    @property
    def lone_context(self) -> str | None:
        """The one active project, when exactly one is active, content routing may pick it and it fits the pool."""
        if len(self.recent) != 1 or len(self.context) != 1:
            return None
        only = self.context[0]
        return only if not self.pool or only in self.pool else None


def require_routable(item: TurnInput) -> None:
    """An interrupt ("to'xta", by `TurnInput.interrupt`, the one interrupt-word list) and the self-test never reach
    routing: pre-parse takes them first (SPEC §6.1 steps 1–2). Routing one would turn "to'xta" into work."""
    if item.interrupt is not None or item.synthetic:
        msg = f"burst {item.burst_id} is an interrupt or the self-test: pre-parse handles it, never routing"
        raise ValueError(msg)


def item_text(item: TurnInput) -> str:
    """Everything the owner said in the burst: texts, captions and transcripts, in order."""
    return " ".join(part.text for part in item.parts if part.text)


def plan_route(
    item: TurnInput,
    projects: Sequence[Project],
    facts: RouteFacts,
    activity: Iterable[Activity],
    *,
    now: datetime,
    window: timedelta,
) -> RoutePlan:
    """Signals 1–3 in order (SPEC §3.2.6): names, then the reply, then recent context."""
    known = {project.id: project for project in projects}
    routable = frozenset(project.id for project in projects if project.routable)
    mentions = find_mentions(item_text(item), projects)
    reply = reply_project(item, facts.reply_projects, known)
    recent = tuple(
        project
        for project in recent_projects([*activity, *facts.reported], facts.running, now=now, window=window)
        if project in known
    )
    base = RoutePlan(
        mentions=mentions,
        reply_project=reply,
        recent=recent,
        context=tuple(project for project in recent if project in routable),
        shadowed=tuple(project for project in recent if known[project].exact_name_only and not known[project].missing),
        allowed=routable,
        media_only=item.is_media_only,
    )
    return _settle(base)


def _settle(plan: RoutePlan) -> RoutePlan:
    """What the names and the reply settle, in order:
    - a look-alike or a typo between two names → ask those, whatever else is said;
    - one project named, and no reply to another one → that project (`explicit`);
    - nothing named, a reply → the replied-to project (`reply`);
    - else the LLM picks, among the named projects (with the replied-to one, when a reply and a name disagree), or
      among a family a word starts, or among every routable project."""
    mentions, reply = plan.mentions, plan.reply_project
    named = mentions.named
    if mentions.lookalike or mentions.ambiguous:
        reason = "lookalike" if mentions.lookalike else "typo_ambiguous"
        return replace(plan, ask_first=_distinct([*named, *mentions.ambiguous, *mentions.lookalike]), ask_reason=reason)
    if len(named) == 1 and reply in {None, named[0]}:
        confidence = TYPO_CONFIDENCE if named[0] in mentions.typo else EXPLICIT_CONFIDENCE
        return replace(plan, fixed=Candidate(named[0], confidence), fixed_method=Method.EXPLICIT)
    if not named and reply is not None:
        return replace(plan, fixed=Candidate(reply, REPLY_CONFIDENCE), fixed_method=Method.REPLY)
    if named:
        pool = _distinct([*named, *([reply] if reply is not None and len(named) == 1 else [])])
    elif len(mentions.family) > 1:
        pool = _distinct([*(project for project in mentions.weak if project in mentions.family), *mentions.family])
    else:
        pool = ()
    return replace(plan, pool=pool, allowed=frozenset(pool)) if pool else plan


# --- the LLM (signal 4) ---------------------------------------------------------------------------------------------
_PROBABILITY: Final[JsonObject] = {"type": "number", "minimum": 0, "maximum": 1}
OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [intent.value for intent in LLM_INTENTS]},
        "project_id": {"type": ["string", "null"]},
        "candidates": {
            "type": "array",
            "maxItems": MAX_CANDIDATES,
            "items": {
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "confidence": _PROBABILITY},
                "required": ["project_id", "confidence"],
                "additionalProperties": False,
            },
        },
        "confidence": _PROBABILITY,
        "needs_fresh": {"type": "boolean"},
        "is_new_project": {"type": "boolean"},
    },
    "required": ["intent", "project_id", "candidates", "confidence", "needs_fresh", "is_new_project"],
    "additionalProperties": False,
}


def prompt_slots() -> dict[str, str]:
    """The slots of `prompts/dev/routing_system.md`: fixed parts only (SPEC §5.4), no persona."""
    return {
        "intents": ", ".join(f"`{intent.value}`" for intent in LLM_INTENTS),
        "max_candidates": str(MAX_CANDIDATES),
    }


def _part_json(kind: UpdateKind, text: str | None) -> JsonObject:
    part: JsonObject = {"kind": kind.value}
    if text:
        part["text"] = text[:LLM_TEXT_MAX]
    return part


def _project_json(project: Project) -> JsonObject:
    row: JsonObject = {"id": project.id}
    if project.aliases:
        row["aliases"] = list[JsonValue](project.aliases)
    if project.description:
        row["description"] = shorten(project.description, LLM_DESCRIPTION_MAX)
    return row


def llm_prompt(
    item: TurnInput,
    plan: RoutePlan,
    projects: Sequence[Project],
    facts: RouteFacts,
    *,
    activity: Iterable[Activity],
    now: datetime,
    window: timedelta,
) -> str:
    """The routing ask as one JSON object (the system prompt explains its fields). Canonical encoding, so the same
    burst and state give the same text: the replay fixtures are keyed by it. With the project already fixed (a name
    or a reply), the table holds only that project: the model then only classifies the intent."""
    known = {project.id: project for project in projects}
    table_ids = set(plan.allowed)
    if plan.fixed is not None:
        table_ids = {plan.fixed.project_id}
    elif plan.reply_project is not None:
        table_ids.add(plan.reply_project)
    table = sorted((known[pid] for pid in table_ids if pid in known), key=lambda p: (name_key(p.id), p.id))
    recent: list[JsonValue] = [
        {
            "project": entry.project_id,
            "kind": entry.kind.value,
            "minutes_ago": int((now - entry.at).total_seconds() // 60),
        }
        for entry in recent_activity([*activity, *facts.reported], (), now=now, window=window)
        if entry.project_id in known
    ]
    payload: JsonObject = {
        "message": [_part_json(part.kind, part.text) for part in item.parts],
        "named": list[JsonValue](plan.mentions.named),
        "maybe_named": list[JsonValue](plan.mentions.hints),
        "reply_to": plan.reply_project,
        "recent": recent,
        "running": list[JsonValue](_distinct(t.project_id for t in facts.running if t.project_id is not None)),
        "open_questions": list[JsonValue](_distinct(facts.open_questions)),
        "projects": [_project_json(project) for project in table],
    }
    if plan.fixed is not None or plan.pool:
        payload["choose_from"] = list[JsonValue]([plan.fixed.project_id] if plan.fixed else plan.pool)
    return json_value.dumps(payload)


@dataclass(frozen=True, slots=True)
class LlmRoute:
    """The routing LLM's answer, its project ids resolved to registered ones."""

    intent: Intent
    project_id: str | None
    confidence: float
    ranked: tuple[Candidate, ...]  # the pick first, then the alternatives by confidence; unknown ids dropped
    needs_fresh: bool = False


def _probability(raw: JsonValue) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return min(1.0, max(0.0, float(raw)))


def _resolve(raw: JsonValue, known: Mapping[str, str]) -> str | None:
    """A project id as the model wrote it → the registered id (exact, else by case-folded name)."""
    if not isinstance(raw, str):
        return None
    return known.get(raw) or known.get(name_key(raw))


def parse_llm_route(raw: JsonValue, projects: Iterable[Project]) -> LlmRoute | None:
    """The structured reply checked against `OUTPUT_SCHEMA`; None when it cannot be used."""
    if not isinstance(raw, dict):
        return None
    known: dict[str, str] = {}
    for registered in projects:
        known.setdefault(name_key(registered.id), registered.id)
        known[registered.id] = registered.id
    intent_raw, confidence = raw.get("intent"), _probability(raw.get("confidence"))
    if not isinstance(intent_raw, str) or intent_raw not in {intent.value for intent in LLM_INTENTS}:
        return None
    if confidence is None:
        return None
    intent = Intent.NEW_PROJECT if raw.get("is_new_project") is True else Intent(intent_raw)
    pick = _resolve(raw.get("project_id"), known)
    ranked: dict[str, float] = {pick: confidence} if pick is not None else {}
    others = raw.get("candidates")
    for entry in others if isinstance(others, list) else []:
        if not isinstance(entry, dict):
            continue
        project, probability = _resolve(entry.get("project_id"), known), _probability(entry.get("confidence"))
        if project is not None and probability is not None and project not in ranked:
            ranked[project] = probability
    alternatives = sorted(((p, c) for p, c in ranked.items() if p != pick), key=lambda item: (-item[1], item[0]))
    ordered = ([Candidate(pick, confidence)] if pick is not None else []) + [Candidate(p, c) for p, c in alternatives]
    return LlmRoute(
        intent=intent,
        project_id=pick,
        confidence=confidence,
        ranked=tuple(ordered),
        needs_fresh=raw.get("needs_fresh") is True,
    )


# --- the decision ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RouteDecision:
    outcome: Outcome
    method: Method
    intent: Intent
    project_id: str | None  # ROUTE and MERGE: the target; ASK and NO_PROJECT: None
    confidence: float
    candidates: tuple[Candidate, ...] = ()  # ASK: the projects the question names, in that order
    needs_fresh: bool = False
    reason: str = ""  # snake_case, for logs and route_decisions analysis

    @property
    def asked(self) -> bool:
        return self.outcome is Outcome.ASK

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.project_id for candidate in self.candidates)


def _with_project(intent: Intent) -> Intent:
    """The intent once a project is fixed: "report on everything" about one project is a report on it."""
    if intent is Intent.REPORT_ALL:
        return Intent.REPORT
    return Intent.INSTRUCT if intent is Intent.NEW_PROJECT else intent


def _ask_candidates(plan: RoutePlan, ranked: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """Who the one question names. A look-alike or a typo between names: exactly those. Else the LLM's plausible
    picks; when it gave fewer than two, the pool, the hints and the active projects fill up (to 2 after one pick, to 3
    after none)."""
    if plan.ask_first:
        return tuple(Candidate(project, 0.0) for project in plan.ask_first[:MAX_CANDIDATES])
    picks = [candidate for candidate in ranked if candidate.confidence >= MIN_CANDIDATE_CONFIDENCE][:MAX_CANDIDATES]
    target = MAX_CANDIDATES if not picks else max(len(picks), 2)
    have = {candidate.project_id for candidate in picks}
    fill = [*plan.pool, *plan.mentions.weak, *plan.context, *plan.shadowed, *plan.mentions.family]
    for project in _distinct(fill):
        if len(picks) >= target:
            break
        if project not in have and (project in plan.allowed or project in plan.shadowed):
            picks.append(Candidate(project, 0.0))
            have.add(project)
    return tuple(picks)


def _ask(
    plan: RoutePlan, ranked: Sequence[Candidate], *, intent: Intent, method: Method, reason: str, fresh: bool
) -> RouteDecision:
    candidates = _ask_candidates(plan, ranked)
    top = candidates[0].confidence if candidates else 0.0
    return RouteDecision(Outcome.ASK, method, _with_project(intent), None, top, candidates, fresh, reason)


def _shadows(project: str, plan: RoutePlan) -> tuple[str, ...]:
    """Exact-only projects active right now whose name runs on from `project`'s (`HRMentor` → `HRMentor-wt-…`)."""
    mine = compact(project)
    return tuple(other for other in plan.shadowed if compact(other).startswith(mine) or mine.startswith(compact(other)))


def _route(
    project: str, confidence: float, *, method: Method, intent: Intent, fresh: bool, reason: str
) -> RouteDecision:
    return RouteDecision(
        Outcome.ROUTE,
        method,
        _with_project(intent),
        project,
        confidence,
        (Candidate(project, confidence),),
        fresh,
        reason,
    )


def decide(plan: RoutePlan, llm: LlmRoute | None, cfg: RouteSettings) -> RouteDecision:
    """Signal 4 and the decision (SPEC §3.2.6 steps 5–6). `llm=None`: it timed out, failed or answered unusably."""
    intent = llm.intent if llm is not None else Intent.INVESTIGATE if plan.media_only else Intent.INSTRUCT
    settled = _settled(plan, llm, cfg, intent=intent)
    if settled is not None:
        return settled
    if llm is None:
        # SPEC: "LLM timeout → the context project (activity ≤ 15 min) if there is exactly one, else ask."
        if (lone := plan.lone_context) is not None:
            return _route(lone, CONTEXT_CONFIDENCE, method=Method.CONTEXT, intent=intent, fresh=False, reason="no_llm")
        return _ask(plan, (), intent=intent, method=Method.LLM, reason="no_llm", fresh=False)
    return _by_llm(plan, llm, cfg, intent=intent)


def _settled(plan: RoutePlan, llm: LlmRoute | None, cfg: RouteSettings, *, intent: Intent) -> RouteDecision | None:
    """What holds whatever the LLM picked: a look-alike asks; a confident new project, the named or replied-to project,
    and a projectless report go through. None: the project is still open."""
    fresh = llm.needs_fresh if llm is not None else False
    if plan.ask_first:
        return _ask(plan, (), intent=intent, method=Method.EXPLICIT, reason=plan.ask_reason, fresh=fresh)
    new = llm is not None and llm.intent is Intent.NEW_PROJECT
    if llm is not None and new and confidence_milli(llm.confidence) >= confidence_milli(cfg.min_confidence):
        return RouteDecision(Outcome.NO_PROJECT, Method.LLM, Intent.NEW_PROJECT, None, llm.confidence, reason="new")
    if plan.fixed is not None and plan.fixed_method is not None:
        method = plan.fixed_method
        return _route(
            plan.fixed.project_id, plan.fixed.confidence, method=method, intent=intent, fresh=fresh, reason=method.value
        )
    if llm is not None and llm.project_id is None and llm.intent in {Intent.REPORT_ALL, Intent.SERVER_STATUS}:
        # Read-only and answered from the digest: no project, and no question even when unsure.
        return RouteDecision(Outcome.NO_PROJECT, Method.LLM, llm.intent, None, llm.confidence, (), fresh, "projectless")
    return None


def _by_llm(plan: RoutePlan, llm: LlmRoute, cfg: RouteSettings, *, intent: Intent) -> RouteDecision:
    """The LLM's pick routes when it is allowed, is no look-alike of an exact-only project in context, clears
    `min_confidence` and leads the next allowed candidate by `min_margin`. Else one question."""
    fresh = llm.needs_fresh
    ranked = tuple(candidate for candidate in llm.ranked if candidate.project_id in plan.allowed)
    pick = llm.ranked[0] if llm.project_id is not None and llm.ranked else None  # null: the model did not commit
    if pick is not None and (not ranked or ranked[0] != pick):
        return _ask(plan, ranked, intent=intent, method=Method.LLM, reason="outside_choice", fresh=fresh)
    if pick is not None and (shadowed := _shadows(pick.project_id, plan)):
        lookalikes = (pick, *(Candidate(other, 0.0) for other in shadowed))
        return _ask(plan, lookalikes, intent=intent, method=Method.LLM, reason="lookalike_context", fresh=fresh)
    # Thresholds compare in thousandths, as stored: 0.75 - 0.55 is 0.2 here, not 0.19999… as floats have it.
    second = confidence_milli(ranked[1].confidence) if len(ranked) > 1 else 0
    sure = pick is not None and confidence_milli(pick.confidence) >= confidence_milli(cfg.min_confidence)
    leads = pick is not None and confidence_milli(pick.confidence) - second >= confidence_milli(cfg.min_margin)
    if pick is not None and sure and leads:
        return _route(pick.project_id, pick.confidence, method=Method.LLM, intent=intent, fresh=fresh, reason="llm")
    if plan.media_only and (lone := plan.lone_context) is not None:
        # UC-02: a lone screenshot proceeds with a sensible interpretation instead of a question.
        return _route(lone, CONTEXT_CONFIDENCE, method=Method.CONTEXT, intent=intent, fresh=fresh, reason="media")
    reason = "no_pick" if pick is None else "small_margin" if sure else "low_confidence"
    return _ask(plan, ranked, intent=intent, method=Method.LLM, reason=reason, fresh=fresh)


def with_follow_up(
    decision: RouteDecision, item: TurnInput, running: Sequence[RunningTurn], cfg: RouteSettings
) -> RouteDecision:
    """UC-02: a burst that closed while a turn works joins it when `burst.domain.decide_dev_follow_up` says so — a
    continuation even when routing alone would ask. Only an instruction steers; a follow-up routed to another project
    never merges, and one that would join two running turns stays as it is."""
    if decision.intent not in STEERABLE or decision.outcome not in {Outcome.ROUTE, Outcome.ASK}:
        return decision
    hint = RouteHint(decision.project_id if decision.outcome is Outcome.ROUTE else None, decision.confidence)
    joins = {
        turn.project_id: decide_dev_follow_up(
            item, turn, route=hint, settings=cfg.window, min_confidence=cfg.merge_min_confidence
        )
        for turn in running
        if turn.project_id is not None
    }
    merging = [(project, verdict) for project, verdict in joins.items() if verdict.merge]
    if len(merging) != 1:
        return decision
    project, verdict = merging[0]
    confidence = decision.confidence if decision.outcome is Outcome.ROUTE else 0.0
    return replace(
        decision,
        outcome=Outcome.MERGE,
        project_id=project,
        confidence=confidence,
        candidates=(Candidate(project, confidence),),
        reason=verdict.reason.value,
    )


# --- what the owner sees --------------------------------------------------------------------------------------------
_MATERIAL: Final = {
    UpdateKind.PHOTO: S.ROUTING_MATERIAL_PHOTO,
    UpdateKind.VIDEO: S.ROUTING_MATERIAL_VIDEO,
    UpdateKind.DOCUMENT: S.ROUTING_MATERIAL_FILE,
    UpdateKind.AUDIO: S.ROUTING_MATERIAL_AUDIO,
    UpdateKind.VOICE: S.ROUTING_MATERIAL_VOICE,
    UpdateKind.LINK: S.ROUTING_MATERIAL_LINK,
}


def material(item: TurnInput, scrub: Callable[[str], str]) -> BotText:
    """What a question quotes (UC-02 AC "quotes or names the material"): the start of the text, else the media."""
    text = " ".join(item_text(item).split())
    if text:
        return S.ROUTING_MATERIAL_QUOTE(text=scrub(shorten(text, EXCERPT_MAX)))
    return _MATERIAL.get(item.parts[0].kind, S.ROUTING_MATERIAL_MESSAGE)()


def question(candidates: Sequence[str], material: BotText) -> BotText:
    """The one clarification (UC-10 F): "«…» — BonviHR'gami yoki HRMentor'gami?". Its order is what "2" means."""
    names = list(candidates[:MAX_CANDIDATES])
    if len(names) == 3:
        return S.ROUTING_WHICH_PROJECT_THREE(material=material, first=names[0], second=names[1], third=names[2])
    if len(names) == 2:
        return S.ROUTING_WHICH_PROJECT_TWO(material=material, first=names[0], second=names[1])
    if len(names) == 1:
        return S.ROUTING_WHICH_PROJECT_ONE(material=material, first=names[0])
    return S.ROUTING_WHICH_PROJECT_ANY(material=material)


def _transcripts(item: TurnInput) -> str:
    said = (part.transcript_text for part in item.parts if part.transcript is TranscriptState.DONE)
    return " ".join(" ".join(text.split()) for text in said if text)


def first_reply(decision: RouteDecision, item: TurnInput, scrub: Callable[[str], str]) -> BotText | None:
    """The first reply always names the target (UC-10 AC): "→ BonviHR", the voice echo "Ovoz: «…» → BonviHR"
    (UC-05), or the question naming the candidates. A projectless intent answers with its report: None."""
    if decision.outcome is Outcome.ASK:
        return question(decision.candidate_ids, material(item, scrub))
    if decision.project_id is None:
        return None
    merged = decision.outcome is Outcome.MERGE
    if said := _transcripts(item):
        echo = scrub(shorten(said, VOICE_ECHO_MAX))
        if merged:
            return S.VOICE_ECHO_MERGED(text=echo, project=decision.project_id)
        return S.VOICE_ECHO(text=echo, project=decision.project_id)
    return S.ROUTED_MERGED(project=decision.project_id) if merged else S.ROUTED(project=decision.project_id)


# --- the owner's answer to a clarification (SPEC §6.1 step 3) -------------------------------------------------------
class ChoiceKind(StrEnum):
    PICKED = "picked"
    CANCELLED = "cancelled"  # "hech biri": none of them
    OUT_OF_RANGE = "out_of_range"  # "3" when two were named
    NONE = "none"  # not an answer: route the burst on its own


@dataclass(frozen=True, slots=True)
class Choice:
    kind: ChoiceKind
    project_id: str | None = None
    pure: bool = True  # only the answer; False: it says more, and is routed on its own as well


_ORDINALS: Final = {
    "birinchi": 1,
    "ikkinchi": 2,
    "uchinchi": 3,
    "first": 1,
    "second": 2,
    "third": 3,
    "первый": 1,
    "второй": 2,
    "третий": 3,
    "oxirgi": -1,
    "last": -1,
    "последний": -1,
}
_NUMBER_WORDS: Final = {"bir": 1, "ikki": 2, "uch": 3, "one": 1, "two": 2, "three": 3, "один": 1, "два": 2, "три": 3}
_NUMBER: Final = re.compile(r"(\d{1,2})(?:-?(?:chi|chisi|si|nchi|nchisi|inchi|inchisi|ga|chisiga))?")
_YES: Final = frozenset({"ha", "xa", "ha shu", "shu", "o'sha", "ok", "yes", "da", "mayli", "to'g'ri", "togri"})
_NONE_OF_THEM: Final = frozenset(
    {"hech biri", "hech qaysi", "hech qaysisi", "hech qaysi biri", "hech birortasi", "none", "neither", "ни один"}
)
# Words an answer may carry around the choice and still be only the answer ("ha, BonviHR'ga", "ikkinchisi bo'lsin").
_ANSWER_WORDS: Final = frozenset(
    {
        "ha",
        "xa",
        "yo'q",
        "yoq",
        "shu",
        "o'sha",
        "bu",
        "u",
        "uchun",
        "ga",
        "loyiha",
        "loyihaga",
        "loyihasi",
        "project",
        "bo'lsin",
        "albatta",
        "iltimos",
        "edi",
        "emas",
        "yoki",
        "ok",
        "da",
        "to'g'ri",
        "togri",
        "kerak",
        "men",
        "aytdim",
    }
)


def _ordinal(word: str) -> int | None:
    if (match := _NUMBER.fullmatch(word)) is not None:
        return int(match.group(1))
    for root, index in _ORDINALS.items():
        if word.startswith(root):
            return index
    return _NUMBER_WORDS.get(word)


def _name_words(project: Project) -> set[str]:
    return {word for name in (project.id, *project.aliases) for word in tokens(name)}


def _leftover(words: Sequence[str], chosen: Project | None) -> list[str]:
    """Words beyond the answer itself: not the chosen project's name (or a piece of it written apart, "bonvi hr"),
    not a number or ordinal, not filler."""
    mine = _name_words(chosen) if chosen is not None else set()
    pieces = {compact(name) for name in (chosen.id, *chosen.aliases)} if chosen is not None else set()
    left: list[str] = []
    for word in words:
        stems = {stem.text for stem in _stems(word)}
        if stems & mine or stems & _ANSWER_WORDS or _ordinal(word) is not None:
            continue
        if any(_part_of(compact(stem), piece) for stem in stems for piece in pieces):
            continue
        left.append(word)
    return left


def _part_of(word: str, name: str) -> bool:
    """`word` is a piece of `name` written apart ("bonvi" of `bonvihr`), or `name` with one typo ("hrmentr")."""
    if len(word) < 2:
        return False
    return word in name or (len(word) >= TYPO_MIN_CHARS and within_one_typo(word, name))


def choose(text: str, candidates: Sequence[str], projects: Sequence[Project], *, accept_yes: bool) -> Choice:
    """Read the owner's answer to "X'gami yoki Y'gami?" ("2", "ikkinchisi", a name, a voice note saying either). A
    name not among the candidates counts only when the answer is nothing else ("yo'q, BonviHR'ga"); a longer message
    about another project is a new message. "ha" picks the one candidate only when nothing else is pending."""
    folded = fold(text).strip(" .!?,")
    words = tokens(folded)
    if folded in _NONE_OF_THEM:
        return Choice(ChoiceKind.CANCELLED)
    found = _by_number(words, candidates) or _by_name(text, words, candidates, projects)
    if found is not None:
        return found
    if accept_yes and len(candidates) == 1 and folded in _YES:
        return Choice(ChoiceKind.PICKED, candidates[0])
    return Choice(ChoiceKind.NONE)


def _by_number(words: Sequence[str], candidates: Sequence[str]) -> Choice | None:
    """ "2", "2-si", "ikkinchisi", "oxirgisi", alone or with filler; a number past the list is out of range."""
    numbers = [index for word in words if (index := _ordinal(word)) is not None]
    if len(numbers) != 1 or _leftover(words, None):
        return None
    index = numbers[0]
    if index == -1 and candidates:
        return Choice(ChoiceKind.PICKED, candidates[-1])
    if 1 <= index <= len(candidates):
        return Choice(ChoiceKind.PICKED, candidates[index - 1])
    return Choice(ChoiceKind.OUT_OF_RANGE)


def _by_name(text: str, words: Sequence[str], candidates: Sequence[str], projects: Sequence[Project]) -> Choice | None:
    """One candidate named — even loosely, since the question just spelled it out — or, when the answer is nothing
    else, one other project named outright ("yo'q, BonviHR'ga")."""
    known = {project.id: project for project in projects}
    mentions = find_mentions(text, projects)
    for group in (mentions.lookalike, mentions.ambiguous, mentions.named, mentions.weak, mentions.family):
        inside = _distinct(project for project in group if project in candidates)
        if len(inside) == 1:
            return Choice(ChoiceKind.PICKED, inside[0], pure=not _leftover(words, known.get(inside[0])))
    if len(mentions.named) == 1 and not _leftover(words, known.get(mentions.named[0])):
        return Choice(ChoiceKind.PICKED, mentions.named[0])
    return None


@dataclass(frozen=True, slots=True)
class OpenClarification:
    id: int
    burst_id: int | None
    kind: ClarificationKind
    candidates: tuple[str, ...]
    message_id: int | None
    asked_at: datetime


def live_clarification(
    items: Iterable[OpenClarification], *, replies_to: Collection[int], now: datetime
) -> OpenClarification | None:
    """The open project question an answer binds to: the one replied to, else the newest one not yet expired."""
    alive = [
        item
        for item in items
        if item.kind is ClarificationKind.WHICH_PROJECT and now - item.asked_at < CLARIFICATION_TTL
    ]
    replied = [item for item in alive if item.message_id is not None and item.message_id in replies_to]
    pool = replied or alive
    return max(pool, key=lambda item: (item.asked_at, item.id)) if pool else None


def expired(items: Iterable[OpenClarification], *, now: datetime) -> tuple[OpenClarification, ...]:
    return tuple(item for item in items if now - item.asked_at >= CLARIFICATION_TTL)


def clarification_expired_text(item: OpenClarification) -> BotText:
    return S.ROUTING_CLARIFY_EXPIRED(candidates=" / ".join(item.candidates) or "?")


def out_of_range_text(candidates: Sequence[str]) -> BotText:
    options = ", ".join(f"{number}) {project}" for number, project in enumerate(candidates, start=1))
    return S.ROUTING_CHOICE_OUT_OF_RANGE(count=len(candidates), options=options)


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
# Words "bu X uchun edi" is made of; a message with any other word is an instruction, not a correction.
_CORRECTION_WORDS: Final = frozenset(
    {
        "bu",
        "u",
        "shu",
        "buni",
        "uni",
        "yo'q",
        "yoq",
        "xato",
        "noto'g'ri",
        "notogri",
        "uchun",
        "edi",
        "ekan",
        "kerak",
        "loyiha",
        "loyihaga",
        "loyihasi",
        "loyihaniki",
        "boshqa",
        "yubor",
        "yuborish",
        "yuborishing",
        "yuborilishi",
        "qilish",
        "wrong",
        "project",
        "it",
        "was",
        "for",
        "meant",
        "это",
        "было",
        "для",
        "не",
        "туда",
        "тот",
        "проект",
    }
)
_CORRECTION_MARKERS: Final = frozenset({"edi", "ekan", "xato", "noto'g'ri", "notogri", "wrong", "было", "туда"})
_CORRECTION_OPENERS: Final = frozenset({"bu", "buni", "shu", "shuni", "uni"})  # "bu HRMentor uchun", "buni X'ga yubor"
CORRECTION_MAX_WORDS: Final = 8


def wrong_target_of(text: str, projects: Sequence[Project]) -> str | None:
    """The project "bu HRMentor uchun edi" says the last work was meant for, or None when `text` is not that: a short
    message that names exactly one project, opens with "bu"/"buni" or holds a "was"/"wrong" marker, and says nothing
    else. "BonviHR'da buni oldin qilish kerak edi, endi tuzat" is an instruction: it says more."""
    words = tokens(text)
    if not words or len(words) > CORRECTION_MAX_WORDS:
        return None
    mentions = find_mentions(text, projects)
    if len(mentions.named) != 1 or mentions.lookalike or mentions.ambiguous:
        return None
    target = next(project for project in projects if project.id == mentions.named[0])
    rest = [{stem.text for stem in _stems(word)} for word in _leftover(words, target)]
    if not all(found & _CORRECTION_WORDS for found in rest):
        return None
    marked = set(words) & _CORRECTION_MARKERS or words[0] in _CORRECTION_OPENERS
    return target.id if marked else None


class CorrectionKind(StrEnum):
    MOVED = "moved"  # the latest work went elsewhere: it moves to the named project
    SAME = "same"  # it already went to the named project
    NOTHING = "nothing"  # no recent work to move


def route_to_correct(
    past: Iterable[PastRoute], *, now: datetime, reply_project: str | None, burst_id: int
) -> PastRoute | None:
    """The route "bu X uchun edi" corrects: the latest work routed within `CORRECTION_WINDOW` (to the replied-to
    project, when the correction is a reply), never the correction itself."""
    found = [
        route
        for route in past
        if route.intent not in NOT_WORK
        and route.project is not None
        and route.burst_id != burst_id
        and now - route.at <= CORRECTION_WINDOW
        and (reply_project is None or route.project == reply_project)
    ]
    return max(found, key=lambda route: (route.at, route.route_id)) if found else None


# --- rows -----------------------------------------------------------------------------------------------------------
def confidence_milli(confidence: float) -> int:
    return round(min(1.0, max(0.0, confidence)) * 1000)


def candidates_json(candidates: Iterable[Candidate]) -> JsonValue:
    return [
        {"project_id": candidate.project_id, "confidence_milli": confidence_milli(candidate.confidence)}
        for candidate in candidates
    ]


def ids_json(ids: Iterable[str]) -> JsonValue:
    return list[JsonValue](ids)


def ids_from_json(raw: JsonValue) -> tuple[str, ...]:
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def member[E: StrEnum](enum: type[E], raw: str, default: E) -> E:
    try:
        return enum(raw)
    except ValueError:
        return default


def route_cutoff(now: datetime) -> datetime:
    return now - ROUTE_KEEP


def clarification_cutoff(now: datetime) -> datetime:
    return now - CLARIFICATION_KEEP
