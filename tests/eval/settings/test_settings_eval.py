"""Settings eval, dev bot (UC-09; SPEC §3.2.15; REQUIREMENTS §4 UC-09 fixture). One runner for `cases.yaml`.

Each case is one owner message, run through `settings.service.handle_request` on its own dev DB (a real SQLite
file), with the dev bot's asker (`gate.ask_setting_change`) and `FakeAssistant` as the `settings` model: either the
structured answer the case gives, or the real model's answer replayed from `tests/fixtures/llm/settings/`.

- `test_case` checks one case exactly: the rows the request stored (key, value, reason) and nothing else, the next
  snapshot, and what the owner was told: one restated line, one clarifying question, one refusal naming the options,
  or exactly one question for a change that weakens a safeguard. That change is then answered "ha" through
  `gate.answer_decision`, as the owner answers it, and must apply exactly once.
- `test_the_suite_meets_the_thresholds` replays every case and measures `thresholds.toml` [settings].
- `test_every_recorded_case_has_exactly_one_fixture`: a fixture no case asks for is stale.
- `test_live_settings_model` (`-m live`) asks the real `ClaudeAssistant` (`settings` kind on the fast tier) every
  `recorded` case and checks the same expectations; `--record-llm` rewrites the fixtures from those answers.
"""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import pytest

from tests.eval.casefile import (
    CaseFileError,
    as_bool,
    as_list,
    as_optional_str,
    as_str,
    fields,
    load_cases,
    thresholds,
)
from tests.eval.recorder import (
    FAST_TIER,
    Recorder,
    as_assistant,
    drop_fixtures,
    fixture_dir,
    kill_leftover_children,
    live_claude,
    recorded_cases,
)
from tests.factories import (
    Fakes,
    add_setting_row,
    gate_asker,
    list_replies,
    make_dev_deps,
    make_owner_origin,
    make_paths,
    open_test_db,
)
from tests.fakes.assistant import load_fixtures
from tests.fakes.claude_client import LIVE_CLI, scrub_live_env
from yordamchi.core import strings as S
from yordamchi.core.errors import DomainError, SettingInvalid, SettingPersonalOnly
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate_service
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.service import AnswerKind, DecisionReply
from yordamchi.ports.assistant import Assistant
from yordamchi.shared.settings import defaults, domain, service, store
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.registry import AnySetting, Group, SettingValue
from yordamchi.shared.settings.service import OutcomeKind, SettingsOutcome

CASES_FILE: Final = Path(__file__).with_name("cases.yaml")
FIXTURES: Final = fixture_dir("settings")
THRESHOLDS: Final = thresholds("settings")
LANGS: Final = frozenset({"uz", "ru", "en", "mixed"})
CHANGE_KINDS: Final = frozenset({"applied", "reverted", "asked"})
FLAG_KINDS: Final[Mapping[str, OutcomeKind]] = {
    "unclear": OutcomeKind.UNCLEAR,
    "needs_code": OutcomeKind.NEEDS_CODE,
    "shown": OutcomeKind.SHOWN,
    "unchanged": OutcomeKind.UNCHANGED,
}
KINDS: Final = CHANGE_KINDS | {"refused", "clarify"} | set(FLAG_KINDS)
PERSONAL_ONLY: Final = "personal_only"  # a refusal by scope; every other refusal reason is a `SettingInvalid` reason
REQUEST_UPDATE_ID: Final = 7001  # the owner's message; "ha" is the next update
DEV_GROUPS: Final = tuple(
    group for group in Group if any(s.group is group and domain.in_scope(s, BotName.DEV) for s in REGISTRY)
)


# --- the case file --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Expect:
    kind: str
    changes: tuple[tuple[str, JsonValue], ...] = ()  # applied, reverted, asked: the exact (key, value) changes
    reason: Literal["change", "revert"] = "change"  # of the rows a change stores
    refused_key: str = ""
    refused_reason: str = ""
    clarify: tuple[str, ...] = ()  # the change lines the clarifying question names, in order


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    group: Group
    lang: str
    text: str
    before: tuple[tuple[str, JsonValue], ...]  # stored versions before the message, oldest first
    answer: dict[str, JsonValue] | None  # the model's structured answer; None: replayed from the recording
    expect: Expect
    known_bug: str | None = None  # fails today on this reported bug: a strict xfail, outside the thresholds

    @property
    def recorded(self) -> bool:
        return self.answer is None


def _json(raw: object, where: str) -> JsonValue:
    """A YAML value as JSON (no dates, no non-string keys): what a model answer or a stored value can hold."""
    if raw is None or isinstance(raw, (str, bool, int, float)):
        return raw
    if isinstance(raw, list):
        return [_json(item, where) for item in raw]
    if isinstance(raw, dict) and all(isinstance(key, str) for key in raw):
        return {str(key): _json(item, where) for key, item in raw.items()}
    msg = f"{where}: {type(raw).__name__} is not a JSON value"
    raise CaseFileError(msg)


def _registered(key: str, where: str) -> AnySetting:
    setting = REGISTRY.get(key)
    if setting is None:
        msg = f"{where}: {key!r} is not a declared setting"
        raise CaseFileError(msg)
    return setting


def _pairs(raw: object, where: str) -> tuple[tuple[str, JsonValue], ...]:
    """A non-empty `{key: value}` mapping of declared settings, in its written order."""
    if not isinstance(raw, dict) or not raw:
        msg = f"{where}: expected a non-empty {{key: value}} mapping"
        raise CaseFileError(msg)
    pairs = tuple((str(key), _json(value, f"{where} {key}")) for key, value in raw.items())
    for key, _ in pairs:
        _registered(key, where)
    return pairs


def _answer(raw: object, where: str) -> dict[str, JsonValue] | None:
    """The `settings` model's answer, in `domain.INTERPRETATION_SCHEMA`. Its keys are not checked against the
    registry: a model may guess a key that does not exist, and that is a case too."""
    if raw == "recorded":
        return None
    data = fields(raw, where, required={"outcome"}, optional={"changes", "keys"})
    outcome = as_str(data["outcome"], f"{where} outcome")
    if outcome not in {intent.value for intent in domain.Intent}:
        msg = f"{where}: outcome {outcome!r} is not one of the schema's"
        raise CaseFileError(msg)
    raw_changes = data.get("changes", {})
    if not isinstance(raw_changes, dict):
        msg = f"{where} changes: expected a {{key: value}} mapping"
        raise CaseFileError(msg)
    changes: list[JsonValue] = [
        {"key": str(key), "value": _json(value, f"{where} changes")} for key, value in raw_changes.items()
    ]
    keys: list[JsonValue] = [as_str(key, f"{where} keys") for key in as_list(data.get("keys", []), f"{where} keys")]
    return {"outcome": outcome, "changes": changes, "keys": keys}


def _expect(raw: object, where: str, group: Group) -> Expect:
    data = fields(raw, where, optional=(*KINDS, "reason"))
    chosen = [kind for kind in KINDS if kind in data]
    if len(chosen) != 1:
        msg = f"{where}: give exactly one of {sorted(KINDS)}, got {sorted(chosen)}"
        raise CaseFileError(msg)
    [kind] = chosen
    reason = as_str(data.get("reason", "change"), f"{where} reason")
    if reason not in {"change", "revert"} or ("reason" in data and kind != "asked"):
        msg = f"{where}: `reason: revert` belongs to an `asked` revert only"
        raise CaseFileError(msg)
    if kind in CHANGE_KINDS:
        changes = _pairs(data[kind], f"{where} {kind}")
        for key, _ in changes:
            if REGISTRY[key].group is not group:
                msg = f"{where}: {key} is in group {REGISTRY[key].group.value}, the case says {group.value}"
                raise CaseFileError(msg)
        revert: Literal["change", "revert"] = "revert" if kind == "reverted" or reason == "revert" else "change"
        return Expect(kind, changes=changes, reason=revert)
    if kind == "refused":
        refused = fields(data[kind], f"{where} refused", required={"key", "reason"})
        key = as_str(refused["key"], f"{where} refused key")
        _registered(key, f"{where} refused")
        return Expect(kind, refused_key=key, refused_reason=as_str(refused["reason"], f"{where} refused reason"))
    if kind == "clarify":
        lines = tuple(as_str(line, f"{where} clarify") for line in as_list(data[kind], f"{where} clarify"))
        if not lines:
            msg = f"{where}: a clarifying question names at least one option"
            raise CaseFileError(msg)
        return Expect(kind, clarify=lines)
    if not as_bool(data[kind], f"{where} {kind}"):
        msg = f"{where}: write `{kind}: true`"
        raise CaseFileError(msg)
    return Expect(kind)


def _case(raw: Mapping[str, object]) -> Case:
    case_id = as_str(raw["id"], "case id")
    data = fields(
        raw, case_id, required={"id", "group", "lang", "text", "model", "expect"}, optional={"before", "known_bug"}
    )
    group = Group(as_str(data["group"], f"{case_id} group"))
    lang = as_str(data["lang"], f"{case_id} lang")
    if lang not in LANGS:
        msg = f"{case_id}: lang {lang!r} is not one of {sorted(LANGS)}"
        raise CaseFileError(msg)
    before = _pairs(data["before"], f"{case_id} before") if "before" in data else ()
    return Case(
        id=case_id,
        group=group,
        lang=lang,
        text=as_str(data["text"], f"{case_id} text"),
        before=before,
        answer=_answer(data["model"], f"{case_id} model"),
        expect=_expect(data["expect"], f"{case_id} expect", group),
        known_bug=as_optional_str(data.get("known_bug"), f"{case_id} known_bug"),
    )


CASES: Final = tuple(_case(raw) for raw in load_cases(CASES_FILE))
RECORDED: Final = tuple(case for case in CASES if case.recorded)
MEASURED: Final = tuple(case for case in CASES if case.known_bug is None)


def _param(case: Case) -> object:
    """A case as a test parameter; a known bug is a strict xfail on its failing assertion only (any other error,
    such as a stale recording, stays red)."""
    if case.known_bug is None:
        return pytest.param(case, id=case.id)
    marks = pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"BUG: {case.known_bug}")
    return pytest.param(case, id=case.id, marks=marks)


# --- one case -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Observed:
    """What one owner message did, read back from the DB and the fakes."""

    outcome: SettingsOutcome | None
    error: DomainError | None
    asked_rows: tuple[tuple[str, JsonValue, str], ...]  # stored before any "ha": must be () for a question
    rows: tuple[tuple[str, JsonValue, str], ...]  # (key, value, reason) the request stored in the end
    questions: tuple[str, ...]  # the settings questions sent before "ha" (the gate sends them itself)
    decisions: tuple[tuple[int, str], ...]  # the settings_weaken decisions, (id, status), in the end
    approval: gate_service.DecisionAnswer | None  # the answer to the owner's "ha", when a question was asked
    told: tuple[str, ...]  # what the gate sent after "ha"
    replies: tuple[tuple[str, JsonValue], ...]  # (op, text) the settings path queued for the owner
    before: Mapping[str, SettingValue]
    after: Mapping[str, SettingValue]
    prompts: tuple[str, ...]  # the owner text each `settings` ask carried

    @property
    def kind(self) -> str:
        if self.error is not None:
            return "refused"
        if self.outcome is None:
            return "none"
        if self.outcome.kind is OutcomeKind.APPLIED:
            return "reverted" if self.rows and all(row[2] == "revert" for row in self.rows) else "applied"
        names = {outcome: name for name, outcome in FLAG_KINDS.items()}
        return names.get(self.outcome.kind, self.outcome.kind.value)

    @property
    def moved(self) -> dict[str, SettingValue]:
        """The keys whose value differs from before the message, with their new value."""
        return {key: value for key, value in self.after.items() if self.before.get(key) != value}


async def _set_up(case: Case, deps: DevDeps) -> None:
    """The earlier changes, stored as versions. A recorded case runs its model on the fast tier, so that is
    stored first: the recording and the replay ask with the same settings table."""
    earlier = ((defaults.MODELS_SETTINGS.key, FAST_TIER),) if case.recorded else ()
    for key, value in (*earlier, *case.before):
        await add_setting_row(deps.db, key, value, at=deps.clock.now())


async def _rows_since(deps: DevDeps, row_id: int) -> tuple[tuple[str, JsonValue, str], ...]:
    async with deps.db.read() as s:
        latest = await store.list_latest_versions(s)
        keys = sorted({row.key for row in latest})
        rows = [row for key in keys for row in await store.list_setting_history(s, key) if row.id > row_id]
    return tuple((row.key, row.value_json, row.reason) for row in sorted(rows, key=lambda row: row.id))


async def _max_row_id(deps: DevDeps) -> int:
    async with deps.db.read() as s:
        return await store.get_max_setting_version_id(s)


async def _decisions(deps: DevDeps) -> tuple[tuple[int, str], ...]:
    async with deps.db.read() as s:
        rows = await gate_store.list_recent_decisions(s, limit=50)
    return tuple(sorted((row.id, row.status) for row in rows if row.kind == gate_service.SETTINGS_KIND))


def _sent(fakes: Fakes) -> tuple[str, ...]:
    return tuple(str(request.payload["text"]) for request in fakes.messenger.sent())


def _prompts(case: Case, deps: DevDeps, fakes: Fakes) -> tuple[str, ...]:
    """The owner text of every `settings` ask this case made."""
    if isinstance(deps.assistant, Recorder):
        return tuple(request.prompt for asked_by, request, _ in deps.assistant.asked if asked_by == case.id)
    return tuple(request.prompt for request in fakes.assistant.requests if request.kind == "settings")


async def run_case(case: Case, deps: DevDeps, fakes: Fakes) -> Observed:
    """Say `case.text` to the dev bot; answer "ha" to the one question if it asks one."""
    if case.answer is not None:
        fakes.assistant.reply_with("settings", case.answer)
    elif deps.assistant is fakes.assistant:
        fakes.assistant.fixtures = load_fixtures(FIXTURES)
    await _set_up(case, deps)
    start = await _max_row_id(deps)
    before = (await service.snapshot(deps)).values
    outcome: SettingsOutcome | None = None
    error: DomainError | None = None
    try:
        outcome = await service.handle_request(
            deps, make_owner_origin(update_id=REQUEST_UPDATE_ID), case.text, ask=gate_asker(deps)
        )
    except DomainError as refused:  # what `guarded()` turns into the owner's message
        error = refused
    asked_rows = await _rows_since(deps, start)
    questions = _sent(fakes)
    approval = None
    if outcome is not None and outcome.kind is OutcomeKind.ASKED:
        approval = await gate_service.answer_decision(
            deps, make_owner_origin(update_id=REQUEST_UPDATE_ID + 1), DecisionReply("ha")
        )
    replies = tuple(
        (row.op, row.payload_json.get("text") if isinstance(row.payload_json, dict) else None)
        for row in await list_replies(deps.db)
    )
    return Observed(
        outcome=outcome,
        error=error,
        asked_rows=asked_rows,
        rows=await _rows_since(deps, start),
        questions=questions,
        decisions=await _decisions(deps),
        approval=approval,
        told=_sent(fakes)[len(questions) :],
        replies=replies,
        before=before,
        after=(await service.snapshot(deps)).values,
        prompts=_prompts(case, deps, fakes),
    )


# --- reading an observation -----------------------------------------------------------------------------------------
def _value(key: str, raw: JsonValue) -> SettingValue:
    return domain.validate(REGISTRY[key], raw)


def _restated(case: Case, observed: Observed) -> str:
    return "; ".join(
        domain.change_line(REGISTRY[key], observed.before[key], _value(key, raw)) for key, raw in case.expect.changes
    )


def _change_failures(case: Case, observed: Observed) -> list[str]:
    expect = case.expect
    failures: list[str] = []
    wanted_rows = sorted((key, domain.json_of(_value(key, raw)), expect.reason) for key, raw in expect.changes)
    if sorted(observed.rows) != wanted_rows:
        failures.append(f"stored {list(observed.rows)}, expected exactly {wanted_rows}")
    wanted = {key: _value(key, raw) for key, raw in expect.changes}
    if observed.moved != wanted:
        failures.append(f"the next snapshot moved {observed.moved}, expected exactly {wanted}")
    restated = _restated(case, observed)
    done = S.SETTING_REVERTED(change=restated) if expect.reason == "revert" else S.SETTING_CHANGED(change=restated)
    if expect.kind == "asked":
        if observed.asked_rows:
            failures.append(f"stored {list(observed.asked_rows)} before the owner said 'ha'")
        decision_id = observed.decisions[0][0] if observed.decisions else 0
        question = S.SETTING_WEAKEN_QUESTION(decision_id=decision_id, change=restated)
        if observed.questions != (question,):
            failures.append(f"asked {list(observed.questions)}, expected exactly one question: {question!r}")
        if [status for _, status in observed.decisions] != ["consumed"]:
            failures.append(f"settings decisions {list(observed.decisions)}: expected one, consumed by the 'ha'")
        if observed.approval is None or observed.approval.kind is not AnswerKind.APPROVED:
            failures.append(f"'ha' was answered {observed.approval}")
        if observed.told != (done,) or observed.replies:
            failures.append(f"after 'ha' told {list(observed.told)} and queued {list(observed.replies)}: want {done!r}")
        return failures
    if observed.questions or observed.decisions:
        failures.append(f"asked {list(observed.questions)} ({list(observed.decisions)}) for a change that weakens none")
    if observed.replies != (("send_text", done),):
        failures.append(f"replied {list(observed.replies)}, expected the one restated line {done!r}")
    return failures


def _refusal_failures(case: Case, observed: Observed) -> list[str]:
    expect, error = case.expect, observed.error
    if error is None:
        return [f"nothing was refused (outcome {observed.outcome})"]
    failures: list[str] = []
    message = S.ERRORS[error.code](**error.params)
    if error.params.get("key") != expect.refused_key:
        failures.append(f"refused key {error.params.get('key')!r}, expected {expect.refused_key!r}")
    if expect.refused_reason == PERSONAL_ONLY:
        if not isinstance(error, SettingPersonalOnly) or not message.endswith("buni shaxsiy botga ayting."):
            failures.append(f"refused with {type(error).__name__}: {message!r}, expected the personal-bot pointer")
    elif not isinstance(error, SettingInvalid) or error.reason != expect.refused_reason:
        failures.append(f"refused with {type(error).__name__} {getattr(error, 'reason', '')!r}: {message!r}")
    else:
        check = REGISTRY[expect.refused_key].check
        if check is not None and check.options() not in message:
            failures.append(f"the refusal {message!r} does not name the valid options {check.options()!r}")
        if "Hech narsa o'zgarmadi" not in message:
            failures.append(f"the refusal {message!r} does not say nothing changed")
    return failures


def _quiet_failures(observed: Observed) -> list[str]:
    """Anything but a change: nothing is stored and nothing is asked."""
    failures: list[str] = []
    if observed.rows or observed.moved:
        failures.append(f"stored {list(observed.rows)}, the snapshot moved {observed.moved}")
    if observed.questions or observed.decisions:
        failures.append(f"asked {list(observed.questions)} ({list(observed.decisions)})")
    return failures


def failures_of(case: Case, observed: Observed) -> list[str]:
    """Every way `observed` differs from the case's expectation (empty when it matches)."""
    expect = case.expect
    failures: list[str] = []
    if observed.prompts != (case.text,):
        failures.append(f"the settings model was asked {list(observed.prompts)}, expected once with the owner's text")
    if observed.kind != expect.kind:
        detail = f"{type(observed.error).__name__}" if observed.error is not None else f"{observed.outcome}"
        return [*failures, f"outcome {observed.kind!r} ({detail}), expected {expect.kind!r}"]
    if expect.kind in CHANGE_KINDS:
        return failures + _change_failures(case, observed)
    failures += _quiet_failures(observed)
    if expect.kind == "refused":
        failures += _refusal_failures(case, observed)
        if observed.replies:
            failures.append(f"queued {list(observed.replies)} with the refusal (guarded() sends the refusal)")
    elif expect.kind == "clarify":
        options = "; ".join(
            S.SETTINGS_CLARIFY_OPTION(number=n, change=line) for n, line in enumerate(expect.clarify, 1)
        )
        wanted = (("send_text", S.SETTINGS_CLARIFY(options=options)),)
        if observed.replies != wanted:
            failures.append(f"replied {list(observed.replies)}, expected one clarifying question {wanted}")
    elif expect.kind == "shown":
        if [op for op, _ in observed.replies] != ["send_text", "send_document"]:
            failures.append(f"showed {list(observed.replies)}, expected the summary and the full list as a file")
    else:
        texts = {"unclear": S.SETTINGS_UNCLEAR(), "needs_code": S.SETTINGS_NEEDS_CODE()}
        wanted_text = texts.get(expect.kind, S.SETTINGS_UNCHANGED())
        if observed.replies != (("send_text", wanted_text),):
            failures.append(f"replied {list(observed.replies)}, expected exactly {wanted_text!r}")
    return failures


# --- the runner -----------------------------------------------------------------------------------------------------
def test_the_case_file_covers_every_group_and_language() -> None:
    assert {case.group for case in CASES} >= set(DEV_GROUPS)
    assert {case.lang for case in CASES} == LANGS
    assert {case.expect.kind for case in CASES} == KINDS
    assert RECORDED, "a subset replays the real model's answers"


@pytest.mark.parametrize("case", [_param(case) for case in CASES])
async def test_case(case: Case, dev_deps: DevDeps, fakes: Fakes) -> None:
    observed = await run_case(case, dev_deps, fakes)
    assert failures_of(case, observed) == []


async def _run_in(root: Path, case: Case, assistant: Assistant | None = None) -> Observed:
    """One case on its own DB and fakes under `root`, so every case starts from nothing."""
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    deps = make_dev_deps(db=db, paths=paths, fakes=fakes)
    if assistant is not None:
        deps = dataclasses.replace(deps, assistant=assistant)
    try:
        return await run_case(case, deps, fakes)
    finally:
        await db.dispose()


async def test_the_suite_meets_the_thresholds(tmp_path: Path) -> None:
    """[settings] over every case but the known bugs (each is a strict xfail in `test_case`, and listed in the
    report); a known bug may not hide a whole group, a weakening kind or the out-of-range subset."""
    assert len(MEASURED) >= len(CASES) - 5, "more known bugs than T33 reported: fix them, do not grow the list"
    results = {case.id: await _run_in(tmp_path / case.id, case) for case in MEASURED}
    matched = {case.id for case in MEASURED if failures_of(case, results[case.id]) == []}
    assert matched == set(results), sorted(set(results) - matched)

    for group in DEV_GROUPS:
        changed = [case.id for case in MEASURED if case.group is group and case.expect.kind in CHANGE_KINDS]
        assert len(changed) >= THRESHOLDS["changes_per_group_min"], f"group {group.value}: {changed}"

    weakening = [case for case in MEASURED if case.expect.kind == "asked"]
    assert weakening, "no case weakens a safeguard: its threshold would pass on nothing"
    for case in weakening:
        assert len(results[case.id].questions) == THRESHOLDS["weakening_questions_per_change"], case.id

    out_of_range = [case for case in MEASURED if case.expect.refused_reason == "out_of_range"]
    assert out_of_range, "no case is out of range: its threshold would pass on nothing"
    stored = sum(len(results[case.id].rows) for case in out_of_range)
    assert stored <= THRESHOLDS["out_of_range_stored_max"]
    assert all(results[case.id].error is not None for case in out_of_range)


def test_every_recorded_case_has_exactly_one_fixture() -> None:
    assert recorded_cases(FIXTURES) == sorted(case.id for case in RECORDED)


@pytest.mark.live
@pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
async def test_live_settings_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_llm: bool) -> None:
    """The real `settings` model on every recorded case; `--record-llm` writes its answers as the fixtures. A known
    bug must still fail here, and nothing else may."""
    scrub_live_env(monkeypatch)
    if record_llm:
        drop_fixtures(FIXTURES, [case.id for case in RECORDED])
    failures: dict[str, list[str]] = {}
    async with live_claude(tmp_path / "sessions") as claude:
        status = await claude.probe(timeout=30)
        source = f"recorded live by test_live_settings_model --record-llm (claude {status.version}, {FAST_TIER})"
        recorder = Recorder(inner=claude, directory=FIXTURES, write=record_llm, source=source)
        for case in RECORDED:
            recorder.case = case.id
            problems = failures_of(case, await _run_in(tmp_path / case.id, case, as_assistant(recorder)))
            if case.known_bug is not None and not problems:
                failures[case.id] = [f"passes now: drop its known_bug ({case.known_bug})"]
            elif case.known_bug is None and problems:
                failures[case.id] = problems
    assert await kill_leftover_children() == []  # every claude child was closed with the adapter
    assert failures == {}
