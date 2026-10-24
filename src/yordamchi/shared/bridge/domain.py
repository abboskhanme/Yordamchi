"""Bridge rules, pure (SPEC §2.3, §3.1 `shared/bridge`; UC-36, NFR-SEC-09).

The bridge is the personal bot's only read path into `dev.db`, and it is read-only by construction: the handle is
`mode=ro` + `query_only`, the store only selects, and nothing here can reach the dev executor. This module decides,
from values only:
- which tables the bridge may read at all (`BRIDGE_TABLES`, the SPEC §2.3 list);
- whether a personal message is a plain project-status question ("BonviHR qanday?") that the digest answers at once
  (`status_question`), or anything else, which goes to the assistant (it may still call the status tool);
- what the owner reads (`status_text`, always ending with the freshness line) and what the assistant's tool hands the
  model (`status_json`, data only).

Every text column read from a digest was redacted by the dev bot before it was written; it is scrubbed once more by
the caller on the way out. Every input is an argument, `now` included; nothing here does I/O (C6, A1).
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText, join

# SPEC §2.3: the personal process reads only these tables of dev.db (integrations and vault_secrets: rows assigned to
# `personal`; setting_versions: keys declared BOTH, read by `shared/settings` itself).
BRIDGE_TABLES: Final = frozenset(
    {"projects", "digests", "server_checks", "integrations", "vault_secrets", "setting_versions", "usage_records"}
)
COMMITS_SHOWN: Final = 3
RESULTS_GIVEN: Final = 3  # results handed to the model's tool
SUBJECT_MAX: Final = 80
NOTE_MAX: Final = 1200  # the handoff note given to the model
STATUS_WORDS_MAX: Final = 8  # a longer message is a real question for the assistant
SERVER_SUMMARY_MAX: Final = 160

# The words of a plain status question besides the project name (folded: lower case, one apostrophe). The dev
# report's list (dev/digest), minus what only makes sense there (servers, "bugun qaysi loyihalarda").
_STATUS_WORDS: Final = frozenset(
    {
        # Uzbek
        "qanday", "qanaqa", "qalay", "holat", "holati", "holatda", "holatini", "ahvol", "ahvoli", "nima", "nimalar",
        "gap", "gaplar", "bo'ldi", "bo'lyapti", "o'zgardi", "o'zgarish", "o'zgarishlar", "yangilik", "yangiliklar",
        "bugun", "hozir", "loyiha", "loyihasi", "loyihada", "ish", "ishlar", "ishi", "ishlari", "ketyapti",
        "turibdi", "qayerda", "status", "statusi", "hisobot", "bu", "u", "endi", "bormi", "nimada", "da", "va",
        # Russian
        "как", "дела", "что", "нового", "новое", "статус", "сегодня", "там", "по", "проект", "проекту", "состояние",
        # English
        "how", "is", "what", "what's", "whats", "new", "going", "the", "state", "project", "update",
    }
    # Russian one-letter prepositions (s, u, v in Cyrillic), escaped: they look like Latin letters.
    | {"\u0441", "\u0443", "\u0432"}
)  # fmt: skip
# Uzbek case endings a project name may carry ("BonviHR'da", "hrmentorga", "BonviHR'ning").
_SUFFIXES: Final = ("", "da", "ga", "ni", "ning", "dan", "dagi", "dagilar", "chi", "mi", "ham", "day")
_APOSTROPHES: Final = re.compile("[\u2019\u2018\u02bb\u02bc`\u00b4]")
_WORD: Final = re.compile("[\\w'\u2019\u2018\u02bb\u02bc`\u00b4-]+")
_COMPACT_DROP: Final = re.compile("[^0-9a-z\u0430-\u044f\u0451]+")  # keeps Latin and Cyrillic letters


# --- what the bridge reads ------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A `projects` row as the bridge needs it: the name, its one-line description, and whether it still exists."""

    project_id: str
    description: str = field(default="", repr=False)
    missing: bool = False


@dataclass(frozen=True, slots=True)
class ProjectName:
    """A project as the speech vocabulary takes it (`voice.domain.Named`): its name; the bridge reads no aliases."""

    id: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitView:
    subject: str = field(repr=False)
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResultView:
    """One finished task of the digest: `status` done | failed | interrupted; `natija` its one-line outcome."""

    status: str
    title: str = field(default="", repr=False)
    natija: str | None = field(default=None, repr=False)
    tests: str | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    """One project's digest, read-only. `checked_at` is when the dev bot last read its git (the freshness line)."""

    project_id: str
    description: str = field(default="", repr=False)
    checked_at: datetime | None = None
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: int = 0
    untracked: int = 0
    git_error_code: str | None = None
    commits: tuple[CommitView, ...] = ()
    results: tuple[ResultView, ...] = ()
    open_questions: int = 0
    pending_decisions: int = 0
    queued: int = 0
    parked: int = 0
    handoff_note: str | None = field(default=None, repr=False)

    @property
    def has_digest(self) -> bool:
        return self.checked_at is not None


@dataclass(frozen=True, slots=True)
class ServerView:
    """The latest read-only check of one server host."""

    host: str
    at: datetime
    ok: bool
    summary: str = field(default="", repr=False)
    error_code: str | None = None


# --- which message is a plain status question ------------------------------------------------------------------------
def fold(text: str) -> str:
    """Lower case, NFKC, every apostrophe as `'`."""
    return _APOSTROPHES.sub("'", unicodedata.normalize("NFKC", text)).casefold()


def compact(name: str) -> str:
    """A name without case, spaces, dashes or apostrophes: "Wunderkind-School" → "wunderkindschool"."""
    return _COMPACT_DROP.sub("", fold(name))


def words(text: str) -> tuple[str, ...]:
    return tuple(word.strip("-'") for word in _WORD.findall(fold(text)) if word.strip("-'"))


def _names_word(word: str, key: str) -> bool:
    folded = compact(word)
    return any(folded == key + suffix for suffix in _SUFFIXES)


def named_projects(text: str, projects: Iterable[ProjectRef]) -> tuple[str, ...]:
    """The projects `text` names (each word compared with the compacted name, an Uzbek case ending allowed), in
    registry order. Two-letter names never match: too many words look like them."""
    tokens = words(text)
    found: list[str] = []
    for project in projects:
        key = compact(project.project_id)
        if len(key) >= 3 and any(_names_word(token, key) for token in tokens):
            found.append(project.project_id)
    return tuple(found)


def status_question(text: str, projects: Sequence[ProjectRef]) -> str | None:
    """The project a message asks the plain status of ("BonviHR qanday?", "HRMentor'da nima gap?"), else None.

    Exactly one project named, at most `STATUS_WORDS_MAX` words, and every other word a status word. Anything more
    ("BonviHR'ni qanday deploy qilaman?") is a real question: the assistant answers it and may call the status tool.
    """
    tokens = words(text)
    if not tokens or len(tokens) > STATUS_WORDS_MAX:
        return None
    named = named_projects(text, projects)
    if len(named) != 1:
        return None
    key = compact(named[0])
    rest = [token for token in tokens if not _names_word(token, key)]
    return named[0] if all(token in _STATUS_WORDS for token in rest) else None


def find_project(name: str, projects: Iterable[ProjectRef]) -> ProjectRef | None:
    """The tool's lookup: the exact project, by compacted name (an ending allowed)."""
    key_of = {compact(project.project_id): project for project in projects}
    folded = compact(name)
    if folded in key_of:
        return key_of[folded]
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if suffix and folded.endswith(suffix) and folded.removesuffix(suffix) in key_of:
            return key_of[folded.removesuffix(suffix)]
    return None


# --- what the owner reads --------------------------------------------------------------------------------------------
def age_text(seconds: float) -> BotText:
    """`3 daqiqa`, `2 soat`, `4 kun` (the dev report's wording)."""
    minutes = timefmt.whole_minutes(seconds)
    if minutes < 60:
        return S.AGE_MINUTES(count=max(1, minutes))
    hours = round(seconds / 3600)
    if hours < 48:
        return S.AGE_HOURS(count=hours)
    return S.AGE_DAYS(count=round(seconds / 86400))


def freshness_line(checked_at: datetime | None, *, now: datetime) -> BotText:
    """The line every status answer ends with: "Ma'lumot: 3 daqiqa oldin"."""
    if checked_at is None:
        return S.FRESHNESS_NONE()
    seconds = max(0.0, (now - checked_at).total_seconds())
    if seconds < 60:
        return S.FRESHNESS_JUST_NOW()
    return S.FRESHNESS(age=age_text(seconds))


def when_text(moment: datetime, *, now: datetime, timezone: str) -> str:
    """`14:05` today, `23.10 14:05` on another day."""
    local = timefmt.local(moment, timezone)
    if local.date() == timefmt.local(now, timezone).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def git_line(status: ProjectStatus) -> BotText:
    if status.git_error_code is not None:
        return S.REPORT_GIT_UNKNOWN()
    branch = status.branch or S.REPORT_GIT_DETACHED()
    if status.head is None:
        return S.REPORT_GIT_EMPTY(branch=branch)
    if status.upstream is None:
        sync = S.REPORT_GIT_NO_UPSTREAM()
    elif status.ahead and status.behind:
        sync = S.REPORT_GIT_DIVERGED(ahead=status.ahead, behind=status.behind)
    elif status.ahead:
        sync = S.REPORT_GIT_AHEAD(count=status.ahead)
    elif status.behind:
        sync = S.REPORT_GIT_BEHIND(count=status.behind)
    else:
        sync = S.REPORT_GIT_EVEN(upstream=status.upstream)
    uncommitted = status.dirty + status.untracked
    tree = S.REPORT_GIT_DIRTY(count=uncommitted) if uncommitted else S.REPORT_GIT_CLEAN()
    return S.REPORT_GIT(branch=branch, sync=sync, tree=tree)


def _result_line(result: ResultView, *, now: datetime, timezone: str) -> BotText:
    time = when_text(result.at, now=now, timezone=timezone) if result.at is not None else "—"
    if result.status == "done":
        natija = result.natija or S.TURN_RESULT_NO_NATIJA()
        tests = S.TESTS_WORDS.get(result.tests or "unknown") or S.TESTS_WORDS["unknown"]
        return S.REPORT_LAST_RESULT(time=time, natija=natija, tests=tests())
    ended = S.REPORT_ENDED_STATES.get(result.status) or S.REPORT_ENDED_STATES["failed"]
    return S.REPORT_LAST_ENDED(time=time, state=ended(), title=_shorten(result.title, SUBJECT_MAX))


def _commits_line(status: ProjectStatus, *, now: datetime, timezone: str) -> BotText:
    start = timefmt.local(now, timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    today = [commit for commit in status.commits if commit.at is not None and commit.at >= start]
    if today:
        subjects = "; ".join(f"«{_shorten(commit.subject, SUBJECT_MAX)}»" for commit in today[:COMMITS_SHOWN])
        return S.REPORT_TODAY(count=len(today), subjects=subjects)
    last = status.commits[0] if status.commits else None
    if last is not None and last.at is not None:
        time = when_text(last.at, now=now, timezone=timezone)
        return S.REPORT_LAST_COMMIT(time=time, subject=_shorten(last.subject, SUBJECT_MAX))
    return S.REPORT_NO_COMMITS()


def status_text(status: ProjectStatus, *, now: datetime, timezone: str) -> BotText:
    """The template answer to "BonviHR qanday?" from the digest: the project, its last result, git, today's commits,
    what waits for the owner, then the freshness line. The live turn is the dev bot's to tell (`turns` stays there)."""
    if not status.has_digest:
        return join([S.BRIDGE_NO_DIGEST(project=status.project_id), freshness_line(None, now=now)])
    head = (
        S.BRIDGE_PROJECT(project=status.project_id, description=_shorten(status.description, SUBJECT_MAX))
        if status.description
        else S.BRIDGE_PROJECT_BARE(project=status.project_id)
    )
    lines = [head]
    lines.append(
        _result_line(status.results[0], now=now, timezone=timezone) if status.results else S.REPORT_NO_RESULT()
    )
    lines.append(git_line(status))
    if status.git_error_code is None:
        lines.append(_commits_line(status, now=now, timezone=timezone))
    if status.queued or status.parked:
        lines.append(S.BRIDGE_QUEUED_PARKED(queued=status.queued, parked=status.parked))
    if status.open_questions or status.pending_decisions:
        lines.append(S.REPORT_PENDING(questions=status.open_questions, decisions=status.pending_decisions))
    lines.append(freshness_line(status.checked_at, now=now))
    return join(lines)


# --- what the assistant's tool hands the model (data, never an instruction) ----------------------------------------
def _minutes_ago(moment: datetime | None, now: datetime) -> int | None:
    return None if moment is None else timefmt.whole_minutes(max(0.0, (now - moment).total_seconds()))


def status_json(status: ProjectStatus, *, now: datetime) -> JsonObject:
    """One project's digest as the model reads it (the dev report's `answer` shape, trimmed)."""
    if not status.has_digest:
        return {"project": status.project_id, "description": status.description, "digest": None}
    results: list[JsonValue] = [
        {
            "status": result.status,
            "title": _shorten(result.title, SUBJECT_MAX),
            "natija": result.natija,
            "tests": result.tests,
            "minutes_ago": _minutes_ago(result.at, now),
        }
        for result in status.results[:RESULTS_GIVEN]
    ]
    commits: list[JsonValue] = [
        {"subject": _shorten(commit.subject, SUBJECT_MAX), "minutes_ago": _minutes_ago(commit.at, now)}
        for commit in status.commits
    ]
    git: JsonObject = {
        "readable": status.git_error_code is None,
        "branch": status.branch,
        "upstream": status.upstream,
        "ahead": status.ahead,
        "behind": status.behind,
        "uncommitted_files": status.dirty + status.untracked,
    }
    note = _shorten(status.handoff_note, NOTE_MAX) if status.handoff_note else None
    return {
        "project": status.project_id,
        "description": status.description,
        "data_minutes_ago": _minutes_ago(status.checked_at, now),
        "results": results,
        "commits": commits,
        "git": git,
        "waiting_for_owner": {"questions": status.open_questions, "decisions": status.pending_decisions},
        "queued": status.queued,
        "parked": status.parked,
        "handoff_note": note,
    }


def unknown_project_json(name: str, projects: Iterable[ProjectRef]) -> JsonObject:
    known: list[JsonValue] = [project.project_id for project in projects if not project.missing]
    return {"error": "unknown_project", "asked": name, "known_projects": known}


def servers_json(servers: Iterable[ServerView], *, now: datetime) -> JsonObject:
    items: list[JsonValue] = [
        {
            "host": server.host,
            "ok": server.ok,
            "summary": _shorten(server.summary, SERVER_SUMMARY_MAX),
            "error": server.error_code,
            "minutes_ago": _minutes_ago(server.at, now),
        }
        for server in servers
    ]
    return {"servers": items}


# --- the digest's JSON columns (shapes written by `dev/digest`; a drift test pins them) ------------------------------
def _moment(raw: JsonValue) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def commits_from_json(raw: JsonValue) -> tuple[CommitView, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        CommitView(subject=str(item["subject"]), at=_moment(item.get("at")))
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("subject"), str)
    )


def results_from_json(raw: JsonValue) -> tuple[ResultView, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[ResultView] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("status"), str):
            continue
        title, natija, tests = item.get("title"), item.get("natija"), item.get("tests")
        found.append(
            ResultView(
                status=str(item["status"]),
                title=title if isinstance(title, str) else "",
                natija=natija if isinstance(natija, str) else None,
                tests=tests if isinstance(tests, str) else None,
                at=_moment(item.get("at")),
            )
        )
    return tuple(found)


def summary_of(raw: JsonValue) -> str:
    """One line of a server check's `summary_json` (`dev/servers`): the open issues, `kind subject value` each."""
    if not isinstance(raw, Mapping):
        return ""
    issues = raw.get("issues")
    parts: list[str] = []
    for issue in issues if isinstance(issues, list) else []:
        if isinstance(issue, Mapping):
            fields = (issue.get("kind"), issue.get("subject"), issue.get("value"))
            parts.append(" ".join(str(item) for item in fields if isinstance(item, str | int | float) and item != ""))
    return "; ".join(part for part in parts if part)
