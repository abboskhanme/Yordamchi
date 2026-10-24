"""Digest rules, pure (SPEC §3.2.4, §4.2 `digests`; UC-15, D10, NFR-SPD-01).

A **digest** is what Yordamchi keeps per project so that a report never needs a session: the git state (branch,
ahead and behind, uncommitted files), the latest commits and the files changed today, the last results (`Natija:`
lines), counts of what waits for the owner, and the handoff note. A report adds the live turn rows ("hozir: <title>,
ishlayapti, N daq") and the latest server checks. This module decides, from values only:
- which path a question takes (`plan_report`): the template at once, the warm report Assistant over the digest, or a
  fresh read-only investigation ("Tekshiryapman"), and whether it is about one project, all of them or the servers;
- when a digest is stale (`is_stale`), when a project's git is polled again (`poll_due`) and whether the poll saw a
  change (`git_changed`);
- what the owner reads: the project report, the cross-project report, the freshness line every report ends with;
- what the report Assistant is given (`answer_prompt`, `synthesis_prompt`) and how a synthesized handoff note is
  completed with the files and commits from git (`synthesized_note`).

Every input is an argument, `now` and setting values included; nothing here does I/O (C6, A1).
"""

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from yordamchi.core import json_value, timefmt
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText, join
from yordamchi.dev.projects.domain import Project, shorten
from yordamchi.dev.routing import domain as routing
from yordamchi.dev.routing.domain import Intent, RouteDecision
from yordamchi.dev.turns import domain as turns
from yordamchi.dev.turns.domain import (
    ACTIVE,
    HANDOFF_BLOCK_MAX,
    INVESTIGATION_WALL,
    WORKING,
    HandoffNote,
    HandoffSource,
    TurnKind,
    TurnStatus,
    TurnSummary,
)
from yordamchi.ports.git import Commit, GitStatus
from yordamchi.shared.burst.domain import TurnInput

type Scrub = Callable[[str], BotText]  # `core.redact.scrubbed`: stored text and model output reach the owner masked

# --- fixed values of the SPEC (not owner behaviour; §4.3 has no key for them) ---------------------------------------
ACTIVE_WITHIN: Final = timedelta(days=7)  # §3.2.4: projects active within 7 days poll every digest_active_poll_s
RECENT_COMMIT_DAYS: Final = 7  # §3.2.4: a change → `git log -10 --since=7.days`
RECENT_COMMITS_MAX: Final = 10
RECENT_FILES_KEPT: Final = 20
RESULTS_KEPT: Final = 5
RECENT_TURNS_READ: Final = 20  # turns read per project: its live ones and its last results
GIT_TIMEOUT_S: Final = 10.0  # one git call of a poll or a background refresh (as turns; `timeouts.git_s` is proposed)
# NFR-SPD / UC-15: a report reaches the owner within 4 s. The answer is queued within this budget; the outbox sender
# (a pass every 0.25 s) and one Telegram call take the rest.
REPORT_BUDGET: Final = timedelta(seconds=3.5)
ON_DEMAND_REFRESH: Final = timedelta(seconds=1.0)  # a stale digest's git refresh inside a report (local git: ~0.1 s)
MIN_ASK_S: Final = 0.5  # less of the budget left than this: the template answers at once
MIN_GIT_TIMEOUT_S: Final = 0.05
SYNTHESIS_TIMEOUT_S: Final = 20.0  # a handoff note written by the report Assistant, in the background
INVESTIGATION_MINUTES: Final = round(INVESTIGATION_WALL.total_seconds() / 60)  # "Tekshiryapman (~3 daqiqa)"
SUBJECTS_SHOWN: Final = 3
FILES_SHOWN: Final = 5
TURNS_SHOWN: Final = 3
SUBJECT_MAX: Final = 80
TITLE_SHOWN_MAX: Final = 80
ANSWER_MAX: Final = 3000  # the Assistant's answer, within one Telegram message with its frame
LLM_QUESTION_MAX: Final = 2000
LLM_DESCRIPTION_MAX: Final = 160
LLM_NOTE_MAX: Final = 1500
# The turn statuses that end with a result the digest keeps ("the last results").
RESULT_STATUSES: Final = frozenset({TurnStatus.DONE, TurnStatus.FAILED, TurnStatus.INTERRUPTED})
REPORT_INTENTS: Final = frozenset({Intent.REPORT, Intent.REPORT_ALL, Intent.SERVER_STATUS})


# --- which path a question takes (UC-15, SPEC §3.2.4 steps 2 and 4) ------------------------------------------------
class ReportScope(StrEnum):
    PROJECT = "project"  # `report_project`: one project's digest
    ALL = "all"  # `report_all`: every digest at once ("bugun qaysi loyihalarda nima o'zgardi?")
    SERVERS = "servers"  # `server_status` with no project: the latest server checks (T31)


class ReportMode(StrEnum):
    TEMPLATE = "template"  # a plain status question: `S.REPORT_*` at once, no model
    ASSISTANT = "assistant"  # a free-form question: the warm report Assistant over the digest; the template on timeout
    INVESTIGATE = "investigate"  # needs a fresh look: "Tekshiryapman" ≤ 2 s, then a read-only investigation turn


@dataclass(frozen=True, slots=True)
class ReportQuestion:
    """A report-shaped burst, as routing decided it (`question_of`). `text` is already redacted by the inbox."""

    text: str = field(repr=False)
    project_id: str | None
    intent: Intent = Intent.REPORT
    needs_fresh: bool = False  # routing's flag: the answer needs a fresh look (SPEC §3.2.6 step 5)
    burst_id: int | None = None
    route_id: int | None = None


def question_of(item: TurnInput, decision: RouteDecision, *, route_id: int | None = None) -> ReportQuestion:
    """The routed burst as a report question. Only a report intent comes here; anything else is a wiring bug."""
    if decision.intent not in REPORT_INTENTS:
        msg = f"intent {decision.intent.value} is not a report (burst {item.burst_id})"
        raise ValueError(msg)
    return ReportQuestion(
        text=routing.item_text(item),
        project_id=decision.project_id,
        intent=decision.intent,
        needs_fresh=decision.needs_fresh,
        burst_id=item.burst_id,
        route_id=route_id,
    )


@dataclass(frozen=True, slots=True)
class ReportPlan:
    scope: ReportScope
    mode: ReportMode
    reason: str  # snake_case, for logs


# Words of a plain status question ("BonviHR qanday?", "holat", "bugun nima o'zgardi?"): the template answers it.
# Folded as routing folds text (lower case, one apostrophe). A question with any other word is free-form.
_PLAIN_WORDS: Final = frozenset(
    {
        # Uzbek
        "qanday", "qanaqa", "qalay", "holat", "holati", "holatda", "holatini", "ahvol", "ahvoli", "nima", "nimalar",
        "gap", "gaplar", "bo'ldi", "bo'lyapti", "o'zgardi", "o'zgarish", "o'zgarishlar", "o'zgarishi", "yangilik",
        "yangiliklar", "bugun", "hozir", "qaysi", "loyiha", "loyihalar", "loyihada", "loyihalarda", "loyihalarning",
        "loyihasi", "hamma", "hammasi", "hammasida", "barcha", "barchasi", "ish", "ishlar", "ishi", "ishlari",
        "ketyapti", "turibdi", "qayerda", "status", "statusi", "hisobot", "va", "da", "bu", "u", "endi", "bormi",
        "nimada",
        # Russian
        "как", "дела", "что", "нового", "новое", "статус", "сегодня", "изменилось", "там", "по", "в", "на", "проект",
        "проекты", "проекту", "проектах", "всё", "все", "состояние",
        # English
        "how", "is", "are", "what", "what's", "whats", "today", "changed", "new", "going", "the", "state",
        "project", "projects", "all", "update", "updates", "report",
    }
)  # fmt: skip
# A question that needs a fresh look rather than the digest: a cause ("nega", "почему"), a server, logs, something
# failing right now. Matched as word beginnings; `_DEEP_WORDS` exactly (so "login" is not "log").
_DEEP_STEMS: Final = (
    "nega", "nimaga", "sabab", "server", "tekshir", "yiqilyap", "ishlamay", "buzil", "why", "crash", "debug",
    "investig", "diagnos", "failing", "почему", "зачем", "отчего", "сервер", "провер", "падает",
)  # fmt: skip
_DEEP_WORDS: Final = frozenset((
    "log", "loglar", "logda", "logida", "loglarda", "loglarida", "logni", "logini", "loglarni", "logs",
    "лог", "логи", "логах", "логов",
))  # fmt: skip
_DEEP_PHRASES: Final = ("nima uchun", "не работает", "what's wrong", "what is wrong")


def _name_keys(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(key for key in {routing.compact(name) for name in names} if len(key) >= 2)


def _is_name(word: str, keys: Sequence[str]) -> bool:
    """A project name or alias, with or without an Uzbek suffix ("BonviHR'da", "hrmentorga")."""
    folded = routing.compact(word)
    return any(folded.startswith(key) for key in keys)


def question_words(text: str, names: Iterable[str]) -> tuple[str, ...]:
    """The question's words without the project names in it."""
    keys = _name_keys(names)
    return tuple(word for word in routing.tokens(text) if not _is_name(word, keys))


def is_deep(text: str, words: Sequence[str]) -> bool:
    folded = routing.fold(text)
    if any(phrase in folded for phrase in _DEEP_PHRASES):
        return True
    return any(word in _DEEP_WORDS or word.startswith(_DEEP_STEMS) for word in words)


def is_plain(words: Sequence[str]) -> bool:
    """Only status words (or nothing but the name): "BonviHR qanday?", "holat", "bugun nima o'zgardi?"."""
    return all(word in _PLAIN_WORDS for word in words)


def plan_report(question: ReportQuestion, names: Iterable[str]) -> ReportPlan:
    """The path of one report question. A deep question about a project becomes an investigation (UC-15 F); about
    no project there is nothing to investigate in, so the Assistant answers from every digest."""
    words = question_words(question.text, names)
    deep = question.needs_fresh or is_deep(question.text, words)
    plain = not deep and is_plain(words)
    if question.project_id is None:
        if question.intent is Intent.SERVER_STATUS:
            return ReportPlan(ReportScope.SERVERS, ReportMode.TEMPLATE, "servers")
        mode = ReportMode.TEMPLATE if plain else ReportMode.ASSISTANT
        return ReportPlan(ReportScope.ALL, mode, "plain" if plain else "deep_without_project" if deep else "free_form")
    if question.needs_fresh or question.intent is Intent.SERVER_STATUS or deep:
        why = "needs_fresh" if question.needs_fresh else "deep_words" if deep else "server_status"
        return ReportPlan(ReportScope.PROJECT, ReportMode.INVESTIGATE, why)
    mode = ReportMode.TEMPLATE if plain else ReportMode.ASSISTANT
    return ReportPlan(ReportScope.PROJECT, mode, "plain" if plain else "free_form")


# --- the digest -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GitState:
    """`git status --porcelain=v2 --branch` plus the head commit's subject and time. `error_code`: git could not be
    read (not a repository, a timeout); the other fields are then empty."""

    branch: str | None = None
    head: str | None = None
    head_subject: str | None = field(default=None, repr=False)
    head_at: datetime | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: int = 0  # tracked entries with changes not yet committed
    untracked: int = 0
    error_code: str | None = None

    @property
    def readable(self) -> bool:
        return self.error_code is None

    @property
    def signature(self) -> tuple[object, ...]:
        """What the cheap poll compares: a difference means a full refresh (SPEC §3.2.4)."""
        return (self.branch, self.head, self.upstream, self.ahead, self.behind, self.dirty, self.untracked)


def git_state_of(status: GitStatus, head: Commit | None) -> GitState:
    return GitState(
        branch=status.branch,
        head=status.head,
        head_subject=head.subject if head is not None else None,
        head_at=head.committed_at if head is not None else None,
        upstream=status.upstream,
        ahead=status.ahead,
        behind=status.behind,
        dirty=status.changed,
        untracked=status.untracked,
    )


def git_changed(stored: GitState, polled: GitStatus) -> bool:
    """The poll saw a change: a new head, branch or upstream, other ahead/behind counts, other uncommitted files. A
    digest whose git was unreadable is refreshed by any successful poll."""
    return not stored.readable or stored.signature != git_state_of(polled, None).signature


@dataclass(frozen=True, slots=True)
class CommitLine:
    sha: str
    subject: str = field(repr=False)
    at: datetime


@dataclass(frozen=True, slots=True)
class ResultLine:
    """One finished turn: its `Natija:` line (done) or how it ended (failed, interrupted)."""

    turn_id: int
    status: TurnStatus
    title: str = field(repr=False)
    natija: str | None = field(default=None, repr=False)
    tests: str | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HandoffState:
    """`digests.handoff_note`: what the next session of the project starts from (SPEC §3.2.4)."""

    text: str = field(repr=False)
    at: datetime
    source: HandoffSource
    turn_id: int | None


@dataclass(frozen=True, slots=True)
class Digest:
    """One `digests` row. `checked_at`: when git was last read (every report's freshness line counts from it);
    `updated_at`: when anything in it last changed."""

    project_id: str
    checked_at: datetime
    updated_at: datetime
    git: GitState
    commits: tuple[CommitLine, ...] = ()  # newest first, ≤ 10 within 7 days
    files: tuple[str, ...] = field(default=(), repr=False)  # changed today (committed or not), ≤ 20 kept
    files_total: int = 0
    results: tuple[ResultLine, ...] = ()  # newest first, ≤ 5
    open_questions: int = 0
    pending_decisions: int = 0
    queued: int = 0
    parked: int = 0
    handoff: HandoffState | None = None
    executor: str = turns.EXECUTOR
    session_id: str | None = None

    @property
    def last_done(self) -> ResultLine | None:
        """The latest turn that ended with a result (`digests.last_natija`)."""
        return next((result for result in self.results if result.status is TurnStatus.DONE), None)

    @property
    def last_turn_id(self) -> int | None:
        return self.results[0].turn_id if self.results else None


@dataclass(frozen=True, slots=True)
class GitRead:
    """What one refresh read from git: the state, the latest commits and the files changed today."""

    state: GitState
    commits: tuple[CommitLine, ...] = ()
    files: tuple[str, ...] = field(default=(), repr=False)


def commit_lines(commits: Iterable[Commit], scrub: Scrub) -> tuple[CommitLine, ...]:
    return tuple(
        CommitLine(commit.sha, scrub(shorten(" ".join(commit.subject.split()), SUBJECT_MAX)), commit.committed_at)
        for commit in commits
    )


def day_start(now: datetime, timezone: str) -> datetime:
    """Midnight of the owner's day (`ui.timezone`), as an aware datetime: "bugun" starts here."""
    local = timefmt.local(now, timezone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def commits_today(commits: Iterable[CommitLine], *, now: datetime, timezone: str) -> tuple[CommitLine, ...]:
    start = day_start(now, timezone)
    return tuple(commit for commit in commits if commit.at >= start)


def files_base(commits: Sequence[CommitLine], state: GitState, *, now: datetime, timezone: str) -> str | None:
    """The ref today's changed files are counted from: the parent of today's oldest commit (of those read), else
    `HEAD` when only uncommitted changes exist; None when there is nothing to read."""
    if state.head is None or not state.readable:
        return None
    today = commits_today(commits, now=now, timezone=timezone)
    if today:
        return f"{today[-1].sha}^"
    return "HEAD" if state.dirty else None


def results_of(summaries: Iterable[TurnSummary], scrub: Scrub, *, keep: int = RESULTS_KEPT) -> tuple[ResultLine, ...]:
    """The latest finished turns (done, failed, interrupted), newest first. Smoke turns are not the owner's work."""
    ended = [
        turn
        for turn in summaries
        if turn.status in RESULT_STATUSES and turn.ended_at is not None and turn.kind is not TurnKind.SMOKE
    ]
    ended.sort(key=lambda turn: (turn.ended_at or turn.queued_at, turn.turn_id), reverse=True)
    return tuple(
        ResultLine(
            turn_id=turn.turn_id,
            status=turn.status,
            title=scrub(turn.title),
            natija=scrub(turn.natija) if turn.natija else None,
            tests=turn.tests,
            at=turn.ended_at,
        )
        for turn in ended[:keep]
    )


def active_of(summaries: Iterable[TurnSummary]) -> tuple[TurnSummary, ...]:
    """Working turns first, then waiting ones, oldest first within each (queued turns are only counted)."""
    shown = [turn for turn in summaries if turn.status in ACTIVE and turn.kind is not TurnKind.SMOKE]
    return tuple(sorted(shown, key=lambda t: (t.status not in WORKING, t.started_at or t.queued_at, t.turn_id)))


def queued_of(summaries: Iterable[TurnSummary]) -> int:
    return sum(1 for turn in summaries if turn.status is TurnStatus.QUEUED and turn.kind is not TurnKind.SMOKE)


def questions_of(summaries: Iterable[TurnSummary]) -> int:
    """Open agent questions, as the turns waiting for an answer (`dev/questions`, T29, may refine this)."""
    return sum(1 for turn in summaries if turn.status is TurnStatus.WAITING_QUESTION)


@dataclass(frozen=True, slots=True)
class HandoffStep:
    """What a refresh does with the latest handoff note: keep `handoff` and, when set, synthesize `synthesize`."""

    handoff: HandoffState | None
    synthesize: HandoffNote | None = None


def handoff_step(previous: HandoffState | None, note: HandoffNote | None) -> HandoffStep:
    """A note already taken stays. A note with its executor block is taken as it is (with the files and commits
    from git); one without a block keeps the previous note until the report Assistant has written it."""
    if note is None or (previous is not None and previous.turn_id == note.turn_id):
        return HandoffStep(previous)
    if not note.needs_synthesis:
        return HandoffStep(HandoffState(note.text(), note.at, note.source, note.turn_id))
    return HandoffStep(previous, synthesize=note)


def synthesized_note(reply: str | None, note: HandoffNote, *, fallback: str) -> str:
    """The note the report Assistant wrote (the four sections), with the files touched and the commits added from
    git, within 4,000 characters. An empty or failed answer → `fallback` (the last steps, `digest_handoff_fallback`)."""
    block = reply.strip() if reply is not None else ""
    return replace(note, block=shorten(block or fallback.strip(), HANDOFF_BLOCK_MAX)).text()


def steps_text(progress: Sequence[str]) -> str:
    """The last progress lines as `- step` lines, for the fallback note."""
    return "\n".join(f"- {line}" for line in progress if line.strip()) or "- (none recorded)"


def build_digest(
    project_id: str,
    *,
    at: datetime,
    git: GitRead,
    summaries: Sequence[TurnSummary],
    decisions: int,
    handoff: HandoffState | None,
    executor: str,
    previous: Digest | None,
    scrub: Scrub,
) -> Digest:
    """The refreshed digest. When git could not be read, the commits and files read before stay; when the turns
    read held no finished turn, so do the results read before."""
    if git.state.readable or previous is None:
        commits, files, files_total = git.commits, git.files[:RECENT_FILES_KEPT], len(git.files)
    else:
        commits, files, files_total = previous.commits, previous.files, previous.files_total
    results = results_of(summaries, scrub) or (previous.results if previous is not None else ())
    fresh = Digest(
        project_id=project_id,
        checked_at=at,
        updated_at=at,
        git=git.state,
        commits=commits,
        files=files,
        files_total=files_total,
        results=results,
        open_questions=questions_of(summaries),
        pending_decisions=decisions,
        queued=queued_of(summaries),
        parked=0,  # P2 park/resume
        handoff=handoff,
        executor=executor,
        session_id=previous.session_id if previous is not None else None,
    )
    if previous is not None and replace(fresh, checked_at=previous.checked_at, updated_at=previous.updated_at) == (
        previous
    ):
        return replace(fresh, updated_at=previous.updated_at)
    return fresh


# --- when to refresh ------------------------------------------------------------------------------------------------
def is_stale(digest: Digest | None, *, now: datetime, stale_after_min: int) -> bool:
    """`digest.stale_after_min`: a report on older data refreshes it first (and says so)."""
    return digest is None or now - digest.checked_at > timedelta(minutes=stale_after_min)


def is_active(project: Project, digest: Digest | None, *, has_turn: bool, now: datetime) -> bool:
    """Active within 7 days (SPEC §3.2.4): a turn now, a commit or a finished turn within 7 days."""
    if has_turn:
        return True
    moments = [project.last_activity_at]
    if digest is not None:
        moments += [digest.git.head_at, *(result.at for result in digest.results[:1])]
    latest = max((moment for moment in moments if moment is not None), default=None)
    return latest is not None and now - latest <= ACTIVE_WITHIN


def poll_due(digest: Digest | None, *, active: bool, now: datetime, active_poll_s: int, idle_poll_min: int) -> bool:
    """`schedule.digest_active_poll_s` for active projects, `schedule.digest_idle_poll_min` for the rest."""
    if digest is None:
        return True
    every = timedelta(seconds=active_poll_s) if active else timedelta(minutes=idle_poll_min)
    return now - digest.checked_at >= every


# --- what the owner reads -------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ServerCheck:
    """The latest read-only check of one server (`dev/servers`, T31, provides these through `DigestState`)."""

    host: str
    at: datetime
    ok: bool
    summary: str = field(default="", repr=False)  # one redacted line


@dataclass(frozen=True, slots=True)
class ProjectState:
    """A project as a report sees it: its digest plus the live turn rows (newest first) and decisions now."""

    project_id: str
    digest: Digest | None
    summaries: tuple[TurnSummary, ...] = ()
    decisions: int = 0
    description: str = field(default="", repr=False)

    @property
    def active(self) -> tuple[TurnSummary, ...]:
        return active_of(self.summaries)

    @property
    def queued(self) -> int:
        return queued_of(self.summaries)

    @property
    def questions(self) -> int:
        return questions_of(self.summaries)

    def results(self, scrub: Scrub) -> tuple[ResultLine, ...]:
        """Live from the turn rows when they hold a finished turn, else what the digest kept."""
        return results_of(self.summaries, scrub) or (self.digest.results if self.digest is not None else ())


@dataclass(frozen=True, slots=True)
class Freshness:
    """How old a report's data is: when git was last read, and whether this report refreshed a stale digest."""

    checked_at: datetime | None
    refreshed: bool = False
    previous_at: datetime | None = None  # the stale digest's `checked_at`, when this report refreshed it
    failed: bool = False  # stale, and the refresh did not finish within the report's budget


def age_text(seconds: float) -> BotText:
    """`3 daqiqa`, `2 soat`, `4 kun`."""
    minutes = timefmt.whole_minutes(seconds)
    if minutes < 60:
        return S.AGE_MINUTES(count=max(1, minutes))
    hours = round(seconds / 3600)
    if hours < 48:
        return S.AGE_HOURS(count=hours)
    return S.AGE_DAYS(count=round(seconds / 86400))


def freshness_line(fresh: Freshness, *, now: datetime) -> BotText:
    """The line every report ends with (`S.FRESHNESS`): "Ma'lumot: 3 daqiqa oldin"."""
    if fresh.refreshed:
        if fresh.previous_at is None:
            return S.FRESHNESS_COLLECTED()
        return S.FRESHNESS_REFRESHED(age=age_text((now - fresh.previous_at).total_seconds()))
    if fresh.checked_at is None:
        return S.FRESHNESS_NONE()
    seconds = max(0.0, (now - fresh.checked_at).total_seconds())
    if fresh.failed:
        return S.FRESHNESS_REFRESH_FAILED(age=age_text(seconds))
    if seconds < 60:
        return S.FRESHNESS_JUST_NOW()
    return S.FRESHNESS(age=age_text(seconds))


def freshness_note(fresh: Freshness, *, now: datetime, stale_after: timedelta, always: bool) -> BotText | None:
    """The freshness line only when it tells the owner something (owner, 2026-10-24: not under every answer): the data
    is older than `stale_after`, could not be refreshed, or was never collected. Every time when `always`."""
    if always or fresh.failed or fresh.checked_at is None:
        return freshness_line(fresh, now=now)
    if fresh.refreshed or now - fresh.checked_at < stale_after:
        return None
    return freshness_line(fresh, now=now)


def when_text(moment: datetime, *, now: datetime, timezone: str) -> str:
    """`14:05` today, `23.10 14:05` on another day (numbers only: the words live in the catalog)."""
    local = timefmt.local(moment, timezone)
    if local.date() == timefmt.local(now, timezone).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def _minutes(turn: TurnSummary, now: datetime) -> int:
    return turns.minutes_of(max(0.0, (now - (turn.started_at or turn.queued_at)).total_seconds()))


def _state_word(turn: TurnSummary) -> BotText:
    entry = S.REPORT_TURN_STATES.get(turn.status.value) or S.REPORT_TURN_STATES[TurnStatus.RUNNING.value]
    return entry()


def turn_lines(state: ProjectState, *, now: datetime, scrub: Scrub) -> list[BotText]:
    """ "BonviHR · hozir: «Login sahifasini tuzat», ishlayapti, 12 daq" (+ up to two more), or "hozir ish yo'q"."""
    active = state.active
    if not active:
        return [S.REPORT_IDLE(project=state.project_id)]
    first, *rest = active[:TURNS_SHOWN]
    lines = [
        S.REPORT_TURN(
            project=state.project_id,
            title=scrub(shorten(first.title, TITLE_SHOWN_MAX)),
            state=_state_word(first),
            minutes=_minutes(first, now),
        )
    ]
    lines += [
        S.REPORT_TURN_MORE(
            title=scrub(shorten(turn.title, TITLE_SHOWN_MAX)), state=_state_word(turn), minutes=_minutes(turn, now)
        )
        for turn in rest
    ]
    return lines


def _result_line(result: ResultLine, *, now: datetime, timezone: str, scrub: Scrub) -> BotText:
    time = when_text(result.at, now=now, timezone=timezone) if result.at is not None else "—"
    if result.status is TurnStatus.DONE:
        natija = scrub(result.natija) if result.natija else S.TURN_RESULT_NO_NATIJA()
        tests = S.TESTS_WORDS.get(result.tests or "unknown") or S.TESTS_WORDS["unknown"]
        return S.REPORT_LAST_RESULT(time=time, natija=natija, tests=tests())
    ended = S.REPORT_ENDED_STATES.get(result.status.value) or S.REPORT_ENDED_STATES[TurnStatus.FAILED.value]
    return S.REPORT_LAST_ENDED(time=time, state=ended(), title=scrub(shorten(result.title, TITLE_SHOWN_MAX)))


def git_line(git: GitState | None) -> BotText:
    """ "Git: main · 2 commit push qilinmagan · 3 fayl commit qilinmagan"."""
    if git is None or not git.readable:
        return S.REPORT_GIT_UNKNOWN()
    branch = git.branch or S.REPORT_GIT_DETACHED()
    if git.head is None:
        return S.REPORT_GIT_EMPTY(branch=branch)
    if git.upstream is None:
        sync = S.REPORT_GIT_NO_UPSTREAM()
    elif git.ahead and git.behind:
        sync = S.REPORT_GIT_DIVERGED(ahead=git.ahead, behind=git.behind)
    elif git.ahead:
        sync = S.REPORT_GIT_AHEAD(count=git.ahead)
    elif git.behind:
        sync = S.REPORT_GIT_BEHIND(count=git.behind)
    else:
        sync = S.REPORT_GIT_EVEN(upstream=git.upstream)
    uncommitted = git.dirty + git.untracked
    tree = S.REPORT_GIT_DIRTY(count=uncommitted) if uncommitted else S.REPORT_GIT_CLEAN()
    return S.REPORT_GIT(branch=branch, sync=sync, tree=tree)


def _quoted(subjects: Iterable[str]) -> str:
    return "; ".join(f"«{subject}»" for subject in subjects)


def commits_line(digest: Digest, *, now: datetime, timezone: str, scrub: Scrub) -> BotText:
    today = commits_today(digest.commits, now=now, timezone=timezone)
    if today:
        subjects = _quoted(scrub(commit.subject) for commit in today[:SUBJECTS_SHOWN])
        capped = len(today) == len(digest.commits) == RECENT_COMMITS_MAX
        entry = S.REPORT_TODAY_CAPPED if capped else S.REPORT_TODAY
        return entry(count=len(today), subjects=subjects)
    if digest.commits:
        last = digest.commits[0]
        return S.REPORT_LAST_COMMIT(time=when_text(last.at, now=now, timezone=timezone), subject=scrub(last.subject))
    return S.REPORT_NO_COMMITS()


def _names(paths: Sequence[str], shown: int) -> str:
    names = [PurePosixPath(path).name or path for path in paths[:shown]]
    return ", ".join(names) + (", …" if len(paths) > shown else "")


def project_report(state: ProjectState, *, now: datetime, timezone: str, scrub: Scrub) -> BotText:
    """The template answer to "BonviHR qanday?" (`S.REPORT_*`), without the freshness line: the live turn, the
    queue, the last result, git, today's commits and files, what waits for the owner."""
    digest = state.digest
    lines = turn_lines(state, now=now, scrub=scrub)
    if state.queued:
        lines.append(S.REPORT_QUEUED(count=state.queued))
    results = state.results(scrub)
    lines.append(_result_line(results[0], now=now, timezone=timezone, scrub=scrub) if results else S.REPORT_NO_RESULT())
    lines.append(git_line(digest.git if digest is not None else None))
    if digest is not None and digest.git.readable:
        lines.append(commits_line(digest, now=now, timezone=timezone, scrub=scrub))
        if digest.files_total:
            lines.append(S.REPORT_FILES(count=digest.files_total, names=scrub(_names(digest.files, FILES_SHOWN))))
    if state.questions or state.decisions:
        lines.append(S.REPORT_PENDING(questions=state.questions, decisions=state.decisions))
    return join(lines)


def _today_done(results: Iterable[ResultLine], start: datetime) -> tuple[ResultLine, ...]:
    return tuple(r for r in results if r.status is TurnStatus.DONE and r.at is not None and r.at >= start)


def all_report(
    states: Sequence[ProjectState], servers: Sequence[ServerCheck], *, now: datetime, timezone: str, scrub: Scrub
) -> BotText:
    """The template answer to "bugun qaysi loyihalarda nima o'zgardi?": every project changed today (its commits
    and its result), what runs now, what waits for the owner, the servers. From the digests only."""
    start = day_start(now, timezone)
    changed: list[tuple[ProjectState, tuple[CommitLine, ...], tuple[ResultLine, ...]]] = []
    for state in states:
        digest = state.digest
        today = commits_today(digest.commits, now=now, timezone=timezone) if digest is not None else ()
        done = _today_done(state.results(scrub), start)
        if today or done:
            changed.append((state, today, done))
    lines = [S.REPORT_ALL_HEADER(count=len(changed)) if changed else S.REPORT_ALL_NONE()]
    for state, today, done in changed:
        parts: list[BotText] = []
        if today and state.digest is not None:
            capped = len(today) == len(state.digest.commits) == RECENT_COMMITS_MAX
            entry = S.REPORT_ALL_COMMITS_CAPPED if capped else S.REPORT_ALL_COMMITS
            parts.append(entry(count=len(today), subject=scrub(today[0].subject)))
        if done and done[0].natija:
            parts.append(S.REPORT_ALL_RESULT(natija=scrub(done[0].natija)))
        lines.append(S.REPORT_ALL_LINE(project=state.project_id, summary="; ".join(parts)))
    running = [
        S.REPORT_ALL_RUNNING_ITEM(
            project=state.project_id, title=scrub(shorten(turn.title, TITLE_SHOWN_MAX)), minutes=_minutes(turn, now)
        )
        for state in states
        for turn in state.active
        if turn.status in WORKING
    ]
    if running:
        lines.append(S.REPORT_ALL_RUNNING(items=", ".join(running)))
    waiting = [state.project_id for state in states if state.questions or state.decisions]
    if waiting:
        lines.append(S.REPORT_ALL_PENDING(projects=", ".join(waiting)))
    if changed and len(states) > len(changed):
        lines.append(S.REPORT_ALL_QUIET(count=len(states) - len(changed)))
    if servers:
        lines.append(servers_line(servers, now=now))
    return join(lines)


def servers_line(checks: Sequence[ServerCheck], *, now: datetime) -> BotText:
    """ "Serverlar: web ✓ (12 daqiqa oldin), db ✗ disk 95% (12 daqiqa oldin)"."""
    hosts = [
        S.REPORT_SERVER_OK(host=check.host, age=age_text((now - check.at).total_seconds()))
        if check.ok
        else S.REPORT_SERVER_FAILED(
            host=check.host, summary=check.summary, age=age_text((now - check.at).total_seconds())
        )
        for check in sorted(checks, key=lambda item: item.host)
    ]
    return S.REPORT_SERVERS(hosts=", ".join(hosts))


def servers_fresh(checks: Sequence[ServerCheck], *, now: datetime, stale_after_min: int) -> bool:
    """SPEC §3.2.4 step 5: a check ≤ 30 min old answers directly; otherwise `dev/servers` checks on demand."""
    limit = timedelta(minutes=stale_after_min)
    return bool(checks) and all(now - check.at <= limit for check in checks)


def assistant_report(state: ProjectState, answer: str, *, now: datetime, scrub: Scrub) -> BotText | None:
    """The report Assistant's answer framed for the owner: the live turn line first (it starts with the project
    name), then the answer. None when the answer is empty (the template answers instead)."""
    text = answer.strip()
    if not text:
        return None
    return join([*turn_lines(state, now=now, scrub=scrub), scrub(shorten(text, ANSWER_MAX))])


# The report Assistant's first line when the question was not about the work at all ("salomlarim", "nimalar qo'lingdan
# keladi?"): the answer goes alone, with no status and no freshness line (owner, 2026-10-24).
CHAT_MARK: Final = "[chat]"


def chat_reply(answer: str, scrub: Scrub) -> BotText | None:
    """The answer without its `[chat]` line when the Assistant marked it as small talk, else None."""
    text = answer.strip()
    if not text.startswith(CHAT_MARK):
        return None
    rest = text.removeprefix(CHAT_MARK).strip()
    return scrub(shorten(rest, ANSWER_MAX)) if rest else None


def all_assistant_report(answer: str, scrub: Scrub) -> BotText | None:
    text = answer.strip()
    return scrub(shorten(text, ANSWER_MAX)) if text else None


# --- what the report Assistant is given (prompts/dev/report_system.md explains the fields) --------------------------
def _local_iso(moment: datetime, timezone: str) -> str:
    return timefmt.local(moment, timezone).isoformat(timespec="minutes")


def state_json(state: ProjectState, *, now: datetime, timezone: str, scrub: Scrub) -> JsonObject:
    """One project's digest and live turns as the `projects` entry of the answer ask."""
    digest = state.digest
    start = day_start(now, timezone)
    running: list[JsonValue] = [
        {"title": turn.title, "state": turn.status.value, "minutes": _minutes(turn, now)} for turn in state.active
    ]
    results: list[JsonValue] = [
        {
            "status": result.status.value,
            "title": result.title,
            "natija": result.natija,
            "tests": result.tests,
            "when": _local_iso(result.at, timezone) if result.at is not None else None,
        }
        for result in state.results(scrub)
    ]
    entry: JsonObject = {
        "project": state.project_id,
        "description": shorten(state.description, LLM_DESCRIPTION_MAX),
        "now_running": running,
        "queued": state.queued,
        "waiting_for_owner": {"questions": state.questions, "decisions": state.decisions},
        "results": results,
    }
    if digest is not None:
        git = digest.git
        commits: list[JsonValue] = [
            {"subject": commit.subject, "when": _local_iso(commit.at, timezone), "today": commit.at >= start}
            for commit in digest.commits
        ]
        files: list[JsonValue] = list(digest.files)
        entry |= {
            "data_minutes_ago": timefmt.whole_minutes((now - digest.checked_at).total_seconds()),
            "git": {
                "readable": git.readable,
                "branch": git.branch,
                "upstream": git.upstream,
                "ahead": git.ahead,
                "behind": git.behind,
                "uncommitted_files": git.dirty + git.untracked,
            },
            "commits": commits,
            "files_changed_today": files,
            "files_changed_today_total": digest.files_total,
            "handoff_note": shorten(digest.handoff.text, LLM_NOTE_MAX) if digest.handoff is not None else None,
        }
    return entry


def answer_prompt(
    question: str,
    states: Sequence[ProjectState],
    servers: Sequence[ServerCheck],
    *,
    now: datetime,
    timezone: str,
    scrub: Scrub,
) -> str:
    """The answer ask as one JSON object, every string scrubbed (NFR-SEC-03: no secret reaches a model)."""
    checks: list[JsonValue] = [
        {
            "host": check.host,
            "ok": check.ok,
            "summary": check.summary,
            "minutes_ago": timefmt.whole_minutes((now - check.at).total_seconds()),
        }
        for check in servers
    ]
    payload: JsonObject = {
        "task": "answer",
        "question": shorten(" ".join(question.split()), LLM_QUESTION_MAX),
        "now": _local_iso(now, timezone),
        "projects": [state_json(state, now=now, timezone=timezone, scrub=scrub) for state in states],
        "servers": checks,
    }
    return json_value.dumps(json_value.map_strings(payload, scrub))


def synthesis_prompt(
    project_id: str, title: str, note: HandoffNote, previous: HandoffState | None, git: GitState, scrub: Scrub
) -> str:
    """The handoff-note ask (SPEC §3.2.4: the previous note, the last 20 progress lines and the git facts)."""
    progress: list[JsonValue] = list(note.progress)
    files: list[JsonValue] = list(note.files)
    commits: list[JsonValue] = [turns.short_sha(sha) for sha in note.commits]
    payload: JsonObject = {
        "task": "handoff_note",
        "project": project_id,
        "title": title,
        "previous_note": shorten(previous.text, LLM_NOTE_MAX) if previous is not None else None,
        "progress": progress,
        "files": files,
        "commits": commits,
        "git": {
            "branch": git.branch,
            "ahead": git.ahead,
            "uncommitted_files": git.dirty + git.untracked,
            "readable": git.readable,
        },
    }
    return json_value.dumps(json_value.map_strings(payload, scrub))


# --- `digests` JSON columns ----------------------------------------------------------------------------------------
def commits_json(commits: Iterable[CommitLine]) -> list[JsonValue]:
    return [{"sha": commit.sha, "subject": commit.subject, "at": commit.at.isoformat()} for commit in commits]


def _moment(raw: JsonValue) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def commits_from_json(raw: JsonValue) -> tuple[CommitLine, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[CommitLine] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sha, subject, at = item.get("sha"), item.get("subject"), _moment(item.get("at"))
        if isinstance(sha, str) and isinstance(subject, str) and at is not None:
            found.append(CommitLine(sha, subject, at))
    return tuple(found)


def results_json(results: Iterable[ResultLine]) -> list[JsonValue]:
    return [
        {
            "turn_id": result.turn_id,
            "status": result.status.value,
            "title": result.title,
            "natija": result.natija,
            "tests": result.tests,
            "at": result.at.isoformat() if result.at is not None else None,
        }
        for result in results
    ]


def results_from_json(raw: JsonValue) -> tuple[ResultLine, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[ResultLine] = []
    known = {status.value for status in RESULT_STATUSES}
    for item in raw:
        if not isinstance(item, dict):
            continue
        turn_id, status, title = item.get("turn_id"), item.get("status"), item.get("title")
        natija, tests = item.get("natija"), item.get("tests")
        if not (isinstance(turn_id, int) and isinstance(status, str) and status in known and isinstance(title, str)):
            continue
        found.append(
            ResultLine(
                turn_id=turn_id,
                status=TurnStatus(status),
                title=title,
                natija=natija if isinstance(natija, str) else None,
                tests=tests if isinstance(tests, str) else None,
                at=_moment(item.get("at")),
            )
        )
    return tuple(found)


def files_json(files: Iterable[str]) -> list[JsonValue]:
    return list(files)


def files_from_json(raw: JsonValue) -> tuple[str, ...]:
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def handoff_source(raw: str | None) -> HandoffSource | None:
    if raw == "executor":
        return "executor"
    if raw == "synthesized":
        return "synthesized"
    return None


def known_names(projects: Collection[Project]) -> tuple[str, ...]:
    """Every folder name and alias: what `plan_report` sets aside before it reads the question's words."""
    return tuple(name for project in projects for name in (project.id, *project.aliases))
