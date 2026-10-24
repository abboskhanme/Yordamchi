"""Routing eval, dev bot (UC-10; SPEC §3.2.6, §6.1; REQUIREMENTS §4 UC-10). One runner for `cases.yaml`.

Each case replays through the real `dev/routing` service, on its own SQLite file in `tmp_path` and on virtual time:
- the registry is `registry.json` (the real scan of the owner's ~/Documents/Projects, snapshotted) plus the case
  file's aliases, as `projects` rows and the `projects.aliases` setting;
- `recent` is what the router hands routing: `spoken` as earlier `route_decisions` rows, `reported` and `running` as
  `RouteFacts`;
- the message is one closed burst (a text, or a voice note with its transcript). Then the router's order (SPEC §6.1):
  pre-parse (an interrupt word, an answer to an open clarification, "bu X uchun edi"), else `route_burst`, which reads
  names and context and asks the routing LLM.

The LLM's answer replays from `tests/fixtures/llm/routing/<fixture_key(request)>.json` (CONVENTIONS §11): a changed
prompt, registry or case is a stale key and fails loudly. `uv run pytest -m live --record-llm tests/eval/routing`
rescans the registry and asks the real `ClaudeAssistant` (routing kind, `models.routing`, effort low) once per case.

- `test_case`: what every case holds, right or wrong: one routing ask; a first reply that names the target, or a
  question that names its ≤ 3 candidates; an open clarification exactly when it asks.
- `test_the_suite_meets_the_thresholds`: every case graded, UC-10 measured against `thresholds.toml` [routing], every
  recording owned by exactly one case. The per-bucket report and each miss go to the terminal summary.
- `test_live_record_pass` (live, opt-in): the same grading on the live answers, plus their latency.
"""

import json
import math
import time
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
import yaml

from tests.eval.casefile import CaseFileError, as_bool, as_int, as_list, as_str, fields, thresholds
from tests.eval.routing import registry
from tests.eval.routing.report import REPORT
from tests.factories import (
    ROUTING_FIXTURES,
    Fakes,
    add_setting_row,
    make_closed_burst,
    make_default_snapshot,
    make_dev_deps,
    make_part,
    make_paths,
    make_running_turn,
    make_voice_part,
    open_test_db,
)
from tests.fakes.assistant import fixture_key, load_fixtures
from tests.fakes.claude_client import LIVE_CLI, LIVE_SEARCH_PATH, scrub_live_env
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.routing import domain, service
from yordamchi.dev.routing import store as routing_store
from yordamchi.dev.routing.domain import Activity, ActivityKind, ClarificationStatus, Intent, Outcome, RouteFacts
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst.domain import CloseReason, TurnInput
from yordamchi.shared.settings import defaults

CASES_FILE: Final = Path(__file__).with_name("cases.yaml")
THRESHOLDS: Final = thresholds("routing")
LATENCY: Final = thresholds("latency")
BUCKETS: Final = {"explicit": "clear", "content": "clear", "context": "context", "ambiguous": "ambiguous"}
INTENTS: Final = {intent.value: intent for intent in domain.LLM_INTENTS}
ROUTED: Final = frozenset({Outcome.ROUTE.value, Outcome.MERGE.value})
ASKED: Final = Outcome.ASK.value
LIVE_SOURCE: Final = "live"  # a recording's `source` starts with this; T20's authored answers did not
LIVE_ASK_TIMEOUT_S: Final = 60.0  # a recording waits for the answer; `timeouts.routing_s` is reported against it
LIVE_CLOSE_TIMEOUT_S: Final = 15.0


# --- the case file --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Recent:
    project: str
    minutes_ago: int
    kind: ActivityKind


@dataclass(frozen=True, slots=True)
class Expect:
    project: str | None = None  # routed (or merged) there, no question
    ask: tuple[str, ...] = ()  # one question naming at least these
    any_question: bool = False  # one question naming at least two candidates, whichever
    no_project: bool = False  # nothing routed: report_all, server_status, new_project

    @property
    def projects(self) -> tuple[str, ...]:
        return (self.project,) if self.project is not None else self.ask


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    category: str
    text: str
    expect: Expect
    voice: bool = False
    recent: tuple[Recent, ...] = ()
    intent: Intent | None = None

    @property
    def bucket(self) -> str:
        return BUCKETS[self.category]


@dataclass(frozen=True, slots=True)
class CaseFile:
    projects: tuple[str, ...]
    aliases: Mapping[str, str]
    cases: tuple[Case, ...]


def _known(project: str, projects: Sequence[str], where: str) -> str:
    if project not in projects:
        msg = f"{where}: {project!r} is not a registered project"
        raise CaseFileError(msg)
    return project


def _recent(raw: object, where: str, projects: Sequence[str]) -> Recent:
    row = fields(raw, where, required={"project", "minutes_ago", "kind"})
    kind = as_str(row["kind"], f"{where} kind")
    if kind not in {item.value for item in ActivityKind}:
        msg = f"{where}: unknown kind {kind!r}"
        raise CaseFileError(msg)
    minutes = as_int(row["minutes_ago"], f"{where} minutes_ago")
    if minutes < 0:
        msg = f"{where}: minutes_ago is negative"
        raise CaseFileError(msg)
    return Recent(_known(as_str(row["project"], f"{where} project"), projects, where), minutes, ActivityKind(kind))


def _expect(raw: object, where: str, projects: Sequence[str]) -> Expect:
    row = fields(raw, where, optional={"project", "ask", "asks", "no_project"})
    if len(row) != 1:
        msg = f"{where}: expect holds exactly one of project, ask, asks, no_project"
        raise CaseFileError(msg)
    if "project" in row:
        return Expect(project=_known(as_str(row["project"], f"{where} project"), projects, where))
    if "ask" in row:
        ask = tuple(_known(as_str(item, f"{where} ask"), projects, where) for item in as_list(row["ask"], where))
        if not 1 <= len(ask) <= domain.MAX_CANDIDATES or len(set(ask)) != len(ask):
            msg = f"{where}: ask names 1 to {domain.MAX_CANDIDATES} distinct projects"
            raise CaseFileError(msg)
        return Expect(ask=ask)
    if "asks" in row:
        if not as_bool(row["asks"], f"{where} asks"):
            msg = f"{where}: asks is only ever true"
            raise CaseFileError(msg)
        return Expect(any_question=True)
    if not as_bool(row["no_project"], f"{where} no_project"):
        msg = f"{where}: no_project is only ever true"
        raise CaseFileError(msg)
    return Expect(no_project=True)


def _case(raw: object, index: int, projects: Sequence[str]) -> Case:
    where = f"{CASES_FILE.name}: case #{index}"
    row = fields(raw, where, required={"id", "category", "text", "expect"}, optional={"voice", "recent", "intent"})
    where = f"{CASES_FILE.name}: {as_str(row['id'], f'{where} id')}"
    category = as_str(row["category"], f"{where} category")
    if category not in BUCKETS:
        msg = f"{where}: unknown category {category!r}"
        raise CaseFileError(msg)
    text = as_str(row["text"], f"{where} text")
    if not text.strip():
        msg = f"{where}: empty text"
        raise CaseFileError(msg)
    intent = row.get("intent")
    if intent is not None and as_str(intent, f"{where} intent") not in INTENTS:
        msg = f"{where}: unknown intent {intent!r}"
        raise CaseFileError(msg)
    recent = as_list(row.get("recent", []), f"{where} recent")
    return Case(
        id=as_str(row["id"], f"{where} id"),
        category=category,
        text=text,
        expect=_expect(row["expect"], f"{where} expect", projects),
        voice=as_bool(row.get("voice", False), f"{where} voice"),
        recent=tuple(_recent(item, f"{where} recent[{n}]", projects) for n, item in enumerate(recent)),
        intent=INTENTS[str(intent)] if intent is not None else None,
    )


def load_case_file(path: Path = CASES_FILE) -> CaseFile:
    document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    top = fields(document, str(path), required={"projects", "aliases", "cases"})
    projects = tuple(as_str(item, f"{path}: projects") for item in as_list(top["projects"], f"{path}: projects"))
    raw_aliases = top["aliases"]
    if not isinstance(raw_aliases, dict):
        msg = f"{path}: aliases is not a mapping"
        raise CaseFileError(msg)
    aliases = {
        as_str(alias, f"{path}: alias"): _known(as_str(target, f"{path}: aliases"), projects, f"{path}: aliases")
        for alias, target in raw_aliases.items()
    }
    cases = tuple(_case(raw, index, projects) for index, raw in enumerate(as_list(top["cases"], f"{path}: cases")))
    repeated = [case_id for case_id, count in Counter(case.id for case in cases).items() if count > 1]
    if repeated:
        msg = f"{path}: case ids used twice: {repeated}"
        raise CaseFileError(msg)
    return CaseFile(projects=projects, aliases=aliases, cases=cases)


CASE_FILE: Final = load_case_file()
CASES: Final = CASE_FILE.cases
SNAPSHOT: Final = registry.load()


# --- one case -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Seen:
    """What the router did with one case's burst."""

    outcome: str  # an `Outcome` value; or `interrupt` / `clarification` / `correction` when pre-parse took it
    project: str | None = None
    candidates: tuple[str, ...] = ()
    intent: Intent | None = None
    reason: str = ""
    reply: str | None = None  # the owner's first reply: "→ X", the voice echo, or the question
    open_clarification: bool = False
    asks: tuple[AssistantRequest, ...] = ()  # what routing asked the LLM

    def describe(self) -> str:
        intent = self.intent.value if self.intent else "-"
        if self.outcome in ROUTED:
            return f"{self.outcome} {self.project} ({intent}, {self.reason})"
        if self.outcome == ASKED:
            return f"ask {list(self.candidates)} ({intent}, {self.reason})"
        return f"{self.outcome} ({intent})"


class _Asks:
    """Every routing ask of one case, whatever assistant answers it."""

    def __init__(self, inner: Assistant) -> None:
        self.inner = inner
        self.requests: list[AssistantRequest] = []

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return await self.inner.probe(timeout=timeout)

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        await self.inner.warm(profile, timeout=timeout)

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        self.requests.append(request)
        return await self.inner.ask(request, timeout=timeout)

    def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        return self.inner.stream(request, idle_timeout=idle_timeout)

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        await self.inner.reset(kind, conversation=conversation, timeout=timeout)


async def _world(case: Case, deps: DevDeps, snapshot: registry.Snapshot) -> tuple[TurnInput, RouteFacts]:
    """The registry, the aliases and the activity before the message, then the message as one closed burst."""
    now = deps.clock.now()
    if CASE_FILE.aliases:
        await add_setting_row(deps.db, defaults.PROJECTS_ALIASES.key, dict(CASE_FILE.aliases), at=now)
    reported: list[Activity] = []
    running: list[burst_domain.RunningTurn] = []
    async with deps.db.uow() as s:
        await registry.add_rows(s, snapshot, at=now)
        for number, recent in enumerate(case.recent, start=1):
            at = now - timedelta(minutes=recent.minutes_ago)
            if recent.kind is ActivityKind.RUNNING:
                running.append(make_running_turn(recent.project, last_part_at=at))
            elif recent.kind is ActivityKind.REPORTED:
                reported.append(Activity(recent.project, at, ActivityKind.REPORTED))
            else:
                await routing_store.add_route_decision(
                    s,
                    burst_id=None,
                    at=at,
                    method=domain.Method.EXPLICIT.value,
                    intent=Intent.INSTRUCT.value,
                    project_id=recent.project,
                    candidates=domain.candidates_json([domain.Candidate(recent.project, 1.0)]),
                    confidence_milli=1000 - number,
                    asked=False,
                    model=None,
                    latency_ms=0,
                )
        part = (
            make_voice_part(1, at_s=0, transcript=case.text) if case.voice else make_part(1, text=case.text, start=now)
        )
        interrupt = burst_domain.interrupt_of_part(part) is not None
        item = await make_closed_burst(s, part, reason=CloseReason.INTERRUPT if interrupt else CloseReason.QUIET)
    return item, RouteFacts(reported=tuple(reported), running=tuple(running))


async def run_case(case: Case, deps: DevDeps, snapshot: registry.Snapshot = SNAPSHOT) -> Seen:
    """One case through the router's order (SPEC §6.1): interrupt, clarification answer, correction, routing."""
    asks = _Asks(deps.assistant)
    deps = replace(deps, assistant=asks)
    item, facts = await _world(case, deps, snapshot)
    if item.interrupt is not None:
        return Seen("interrupt")
    if (answered := await service.answer_clarification(deps, item, facts)) is not None:
        return Seen("clarification", project=answered.project_id)
    if (corrected := await service.correct_route(deps, item, facts)) is not None:
        return Seen("correction", project=corrected.to_project, intent=Intent.WRONG_TARGET)
    routed = await service.route_burst(deps, item, facts)
    if routed is None:
        return Seen("already_handled")
    async with deps.db.read() as s:
        questions = await routing_store.list_clarifications_with_status(s, ClarificationStatus.OPEN.value)
    decision = routed.decision
    return Seen(
        outcome=decision.outcome.value,
        project=decision.project_id,
        candidates=decision.candidate_ids,
        intent=decision.intent,
        reason=decision.reason,
        reply=routed.reply,
        open_clarification=bool(questions),
        asks=tuple(asks.requests),
    )


def invariant_failures(seen: Seen) -> list[str]:
    """What holds for every routed case, whether the route is right or not (UC-10 AC: the first reply names the
    target; one question naming ≤ 3 candidates)."""
    if seen.outcome not in {*ROUTED, ASKED, Outcome.NO_PROJECT.value}:
        return []  # pre-parse took it: graded as a miss, there is no route to check
    failures: list[str] = []
    if [request.kind for request in seen.asks] != ["routing"]:
        failures.append(f"expected one routing ask, got {[request.kind for request in seen.asks]}")
    if seen.outcome in ROUTED and (seen.reply is None or str(seen.project) not in seen.reply):
        failures.append(f"the first reply {seen.reply!r} does not name {seen.project}")
    if seen.outcome == ASKED:
        if len(seen.candidates) > domain.MAX_CANDIDATES:
            failures.append(f"the question names {len(seen.candidates)} candidates")
        if seen.reply is None or any(project not in seen.reply for project in seen.candidates):
            failures.append(f"the question {seen.reply!r} does not name every candidate {list(seen.candidates)}")
    if seen.open_clarification is not (seen.outcome == ASKED):
        failures.append(f"an open clarification is {seen.open_clarification} after {seen.outcome}")
    return failures


# --- grading --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Grade:
    correct: bool
    silent_wrong: bool  # routed without a question, but not to the expected project (or not at all expected)
    asked: bool
    on_target: bool = False  # routed to the expected project, whatever the intent


def grade(case: Case, seen: Seen) -> Grade:
    routed, asked = seen.outcome in ROUTED, seen.outcome == ASKED
    intent_ok = case.intent is None or seen.intent is case.intent
    expect = case.expect
    if expect.project is not None:
        on_target = routed and seen.project == expect.project
        return Grade(on_target and intent_ok, routed and not on_target, asked, on_target)
    if expect.ask:
        return Grade(asked and set(expect.ask) <= set(seen.candidates), routed, asked)
    if expect.any_question:
        return Grade(asked and len(seen.candidates) >= 2, routed, asked)
    return Grade(seen.outcome == Outcome.NO_PROJECT.value and intent_ok, routed, asked)


@dataclass(frozen=True, slots=True)
class Bucket:
    total: int = 0
    correct: int = 0
    asked: int = 0  # not correct, but a question
    wrong: int = 0  # not correct, no question, and not the expected project (UC-10: "asked, never wrong")

    def add(self, mark: Grade) -> "Bucket":
        return Bucket(
            self.total + 1,
            self.correct + mark.correct,
            self.asked + (not mark.correct and mark.asked),
            self.wrong + (not mark.correct and not mark.asked and not mark.on_target),
        )

    def meets(self, minimum: float) -> bool:
        """`correct / total ≥ minimum`, in thousandths as the thresholds are written (no float rounding)."""
        return self.total > 0 and self.correct * 1000 >= round(minimum * 1000) * self.total

    def ratio(self) -> str:
        return f"{self.correct}/{self.total} = {100 * self.correct / self.total:.1f} %" if self.total else "0/0"


@dataclass(frozen=True, slots=True)
class Results:
    seen: Mapping[str, Seen]
    marks: Mapping[str, Grade]
    buckets: Mapping[str, Bucket]

    @property
    def silent_wrong(self) -> tuple[Case, ...]:
        return tuple(case for case in CASES if self.marks[case.id].silent_wrong)

    @property
    def misses(self) -> tuple[Case, ...]:
        return tuple(case for case in CASES if not self.marks[case.id].correct)


def evaluate(seen: Mapping[str, Seen]) -> Results:
    marks = {case.id: grade(case, seen[case.id]) for case in CASES}
    buckets: dict[str, Bucket] = {}
    for case in CASES:
        buckets[case.bucket] = buckets.get(case.bucket, Bucket()).add(marks[case.id])
    return Results(seen, marks, buckets)


def projects_covered() -> set[str]:
    return {project for case in CASES for project in case.expect.projects}


def threshold_failures(results: Results) -> list[str]:
    """UC-10 against `thresholds.toml` [routing]."""
    clear, context, ambiguous = (results.buckets.get(name, Bucket()) for name in ("clear", "context", "ambiguous"))
    checks = [
        (len(CASES) >= THRESHOLDS["cases_min"], f"{len(CASES)} cases"),
        (clear.total >= THRESHOLDS["explicit_cases_min"], f"{clear.total} explicit or content-clear cases"),
        (context.total >= THRESHOLDS["context_cases_min"], f"{context.total} context cases"),
        (ambiguous.total >= THRESHOLDS["ambiguous_cases_min"], f"{ambiguous.total} ambiguous cases"),
        (len(projects_covered()) >= THRESHOLDS["projects_min"], f"{len(projects_covered())} projects"),
        (clear.meets(THRESHOLDS["explicit_correct_min"]), f"clear routed right {clear.ratio()}"),
        (context.meets(THRESHOLDS["context_correct_min"]), f"context routed right {context.ratio()}"),
        (context.wrong <= THRESHOLDS["context_wrong_max"], f"context wrong {context.wrong}"),
        (ambiguous.meets(THRESHOLDS["ambiguous_question_min"]), f"ambiguous asked right {ambiguous.ratio()}"),
        (len(results.silent_wrong) <= THRESHOLDS["silent_wrong_max"], f"silent wrong {len(results.silent_wrong)}"),
    ]
    return [what for ok, what in checks if not ok]


def _expected(case: Case) -> str:
    if case.expect.project is not None:
        return case.expect.project + (f" ({case.intent.value})" if case.intent else "")
    if case.expect.ask:
        return f"ask {list(case.expect.ask)}"
    if case.expect.any_question:
        return "a question naming two or more"
    return f"no project ({case.intent.value})" if case.intent else "no project"


def report_lines(results: Results, source: str) -> list[str]:
    clear, context, ambiguous = (results.buckets.get(name, Bucket()) for name in ("clear", "context", "ambiguous"))
    t = THRESHOLDS
    lines = [
        f"{len(CASES)} cases over {len(projects_covered())} projects; model answers: {source}",
        f"  explicit + content-clear  routed right {clear.ratio()}  (min {t['explicit_correct_min']:.0%})",
        (
            f"  context follow-ups        routed right {context.ratio()}  (min {t['context_correct_min']:.0%}),"
            f" asked {context.asked}, wrong {context.wrong} (max {t['context_wrong_max']:.0f})"
        ),
        f"  genuinely ambiguous       asked right  {ambiguous.ratio()}  (min {t['ambiguous_question_min']:.0%})",
        f"  silent wrong routes       {len(results.silent_wrong)}  (max {t['silent_wrong_max']:.0f})",
    ]
    for case in results.misses:
        flag = "SILENT WRONG " if results.marks[case.id].silent_wrong else ""
        lines.append(
            f"  miss {flag}{case.id} [{case.category}] {case.text!r}: expected {_expected(case)}, "
            f"got {results.seen[case.id].describe()}"
        )
    return lines


# --- replay ---------------------------------------------------------------------------------------------------------
def _recordings(directory: Path = ROUTING_FIXTURES) -> dict[str, dict[str, object]]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))}


def _is_live(data: Mapping[str, object]) -> bool:
    return str(data.get("source", "")).startswith(LIVE_SOURCE)


FIXTURES: Final = load_fixtures(ROUTING_FIXTURES)


async def _replay_in(
    root: Path, case: Case, *, assistant: Assistant | None = None, snapshot: registry.Snapshot = SNAPSHOT
) -> Seen:
    """One case on its own database and fakes under `root`, so every case starts from nothing. The routing LLM is
    the recordings, or `assistant`."""
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    fakes.assistant.fixtures = FIXTURES
    try:
        deps = make_dev_deps(db=db, paths=paths, fakes=fakes)
        return await run_case(case, deps if assistant is None else replace(deps, assistant=assistant), snapshot)
    finally:
        await db.dispose()


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_case(case: Case, tmp_path: Path) -> None:
    seen = await _replay_in(tmp_path, case)

    assert invariant_failures(seen) == []


def test_the_case_file_is_written_against_the_snapshotted_registry() -> None:
    assert SNAPSHOT.ids == CASE_FILE.projects
    exact_only = {entry.id for entry in SNAPSHOT.entries if entry.is_worktree or entry.is_archive_name}
    assert exact_only == {"HRMentor-wt-agent-api", "BonviCall Clone Archive"}
    assert projects_covered() <= set(SNAPSHOT.ids)


async def test_the_suite_meets_the_thresholds(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    seen = {case.id: await _replay_in(tmp_path / case.id, case) for case in CASES}
    results = evaluate(seen)
    lines = report_lines(results, "replayed from tests/fixtures/llm/routing")
    request.config.stash[REPORT] = lines

    owners = Counter(fixture_key(ask) for item in seen.values() for ask in item.asks)
    assert [key for key, count in owners.items() if count > 1] == []  # each recording answers one case
    recordings = _recordings()
    assert set(recordings) == set(owners), "a recording no case asks for is stale: re-record"
    asked_by = {fixture_key(ask): case_id for case_id, item in seen.items() for ask in item.asks}
    assert {key: data.get("case") for key, data in recordings.items()} == asked_by
    assert all(_is_live(data) for data in recordings.values())
    assert threshold_failures(results) == [], "\n".join(lines)


# --- the live record pass -------------------------------------------------------------------------------------------
class Recorder:
    """The real assistant, recorded. Each new ask goes live once, waits for the answer however long it takes (its wall
    time is kept and reported against `timeouts.routing_s`), and is written to `tests/fixtures/llm/routing/` at once.
    An ask already recorded live replays, so an interrupted pass resumes where it stopped."""

    def __init__(self, live: Assistant, *, source: str, directory: Path = ROUTING_FIXTURES) -> None:
        self.live = live
        self.source = source
        self.directory = directory
        self.case_id = ""
        self.used: dict[str, str] = {}  # recording key → the case that asked it
        self.latency_ms: dict[str, int] = {}  # case → wall time of its live ask
        self.recorded = {key: data for key, data in _recordings(directory).items() if _is_live(data)}
        self.kept = {key: reply for key, reply in load_fixtures(directory).items() if key in self.recorded}

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return await self.live.probe(timeout=timeout)

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        await self.live.warm(profile, timeout=timeout)

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        key = fixture_key(request)
        self.used[key] = self.case_id
        if (kept := self.kept.get(key)) is not None:
            latency = self.recorded.get(key, {}).get("latency_ms")
            if isinstance(latency, int):
                self.latency_ms[self.case_id] = latency
            return kept
        started = time.monotonic()
        reply = await self.live.ask(request, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))
        latency_ms = round((time.monotonic() - started) * 1000)
        record: dict[str, JsonValue] = {
            "case": self.case_id,
            "source": self.source,
            "model": reply.model,
            "provider": reply.provider,
            "duration_ms": reply.duration_ms,
            "latency_ms": latency_ms,
            "structured": reply.structured,
            "text": reply.text,
        }
        path = self.directory / f"{key}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.recorded[key] = dict(record)
        self.kept[key] = reply
        self.latency_ms[self.case_id] = latency_ms
        return reply

    def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        return self.live.stream(request, idle_timeout=idle_timeout)

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        await self.live.reset(kind, conversation=conversation, timeout=timeout)

    def remove_stale(self) -> list[str]:
        """After a full pass: the recordings no case asked for (an old prompt or input, T20's authored answers)."""
        stale = sorted(path.stem for path in self.directory.glob("*.json") if path.stem not in self.used)
        for key in stale:
            (self.directory / f"{key}.json").unlink()
        return stale


def latency_lines(latency_ms: Mapping[str, int], cfg: domain.RouteSettings) -> list[str]:
    values = sorted(latency_ms.values())
    if not values:
        return ["  latency: no live ask"]

    def percentile(share: float) -> int:
        return values[min(len(values) - 1, math.ceil(share * len(values)) - 1)]

    budget_s = LATENCY["routing_s"]
    over_timeout = sum(1 for value in values if value > cfg.timeout_s * 1000)
    over_budget = sum(1 for value in values if value > budget_s * 1000)
    return [
        f"  latency of {len(values)} live asks: p50 {percentile(0.5)} ms, p90 {percentile(0.9)} ms, max {values[-1]}",
        f"  over timeouts.routing_s = {cfg.timeout_s} s (production falls back): {over_timeout}",
        f"  over the {budget_s} s decision budget (UC-10): {over_budget}",
    ]


async def _warm(root: Path, assistant: Assistant) -> None:
    """Warm the routing client with the profile every case asks with (the same settings version), so no case is the
    cold start."""
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    try:
        deps = replace(make_dev_deps(db=db, paths=paths, fakes=fakes), assistant=assistant)
        if CASE_FILE.aliases:
            await add_setting_row(db, defaults.PROJECTS_ALIASES.key, dict(CASE_FILE.aliases), at=fakes.clock.now())
        await service.warm_routing(deps)
    finally:
        await db.dispose()


@pytest.mark.live
@pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
async def test_live_record_pass(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rescan the owner's folders, ask the real routing model once per case, write the recordings, grade them."""
    if not request.config.getoption("--record-llm"):
        pytest.skip("records only with --record-llm: uv run pytest -m live --record-llm tests/eval/routing")
    scrub_live_env(monkeypatch)
    snapshot = await registry.scan(registry.owner_root(), tmp_path / "scan")
    assert snapshot.ids == CASE_FILE.projects, "the folders changed: update cases.yaml and OWNER_PROJECT_NAMES first"
    registry.dump(snapshot)
    cfg = domain.route_settings(make_default_snapshot())
    live = ClaudeAssistant(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH, sessions_dir=tmp_path / "sessions")
    try:
        status = await live.probe(timeout=LIVE_ASK_TIMEOUT_S)
        source = f"{LIVE_SOURCE}: claude {status.version}, models.routing={cfg.model}, effort {domain.ROUTING_EFFORT}"
        recorder = Recorder(live, source=source)
        await _warm(tmp_path / "warm", recorder)
        seen: dict[str, Seen] = {}
        for case in CASES:
            recorder.case_id = case.id
            seen[case.id] = await _replay_in(tmp_path / case.id, case, assistant=recorder, snapshot=snapshot)
    finally:
        await live.close(timeout=LIVE_CLOSE_TIMEOUT_S)
    missing = [case.id for case in CASES if case.id not in recorder.latency_ms]
    assert missing == [], "these asks got no live answer; run the pass again (the recorded asks are kept)"
    stale = recorder.remove_stale()
    results = evaluate(seen)
    lines = [*report_lines(results, source), *latency_lines(recorder.latency_ms, cfg)]
    lines.append(f"  removed {len(stale)} recordings no case asks for")
    request.config.stash[REPORT] = lines
    assert [failure for item in seen.values() for failure in invariant_failures(item)] == []
    assert threshold_failures(results) == [], "\n".join(lines)
