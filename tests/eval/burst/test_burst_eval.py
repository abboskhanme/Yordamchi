"""Burst eval, dev bot (UC-02, UC-03; SPEC §4.4, §3.2.5; REQUIREMENTS §4.1). One runner for `cases.yaml`.

Each case replays through the real intake and the real burst service, on a real SQLite file in `tmp_path` and on
virtual time (FakeClock), the way the burst loop (`burst.service.run_bursts`) runs it:
- a part is accepted by `inbox.accept_update` at its `t`, sent by the owner at its `sent_t` (Telegram's date, if
  given), then a burst pass attaches it;
- a voice part's STT result comes back through `burst.service.record_transcript` at its `transcript_t`;
- between events the clock moves to the earliest `closes_at`, where the timer pass (`close_due_bursts`) runs.
At one instant, parts arrive first (in `update_id` order), then transcripts, then the timer pass runs, all in one
pass: a part received exactly at `closes_at` still belongs to its burst (`Window.admits`).

A follow-up (`expect.follow_up`) is decided by `burst.domain.decide_dev_follow_up`, with the first closed burst as the
running dev turn (as `dev/turns` will hand it over, T24) and the case's route hint standing in for `dev/routing`
(T20).

- `test_case` checks one case exactly, plus what every case must hold: no message to the owner is queued, every
  update is attached, every voice part asked for its STT job, and the label rules (lone media, complete text,
  interrupts).
- `test_the_suite_meets_the_thresholds` replays every case and measures the UC-02 numbers of `thresholds.toml`
  [burst]. "Asks nothing" is measured at the burst stage (the outbox holds no message for the owner); routing's
  clarifying questions join it once T20/T24 are wired, and the "no question needed" subset is graded by the owner
  (T45).
"""

import dataclasses
import itertools
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from tests.eval.casefile import (
    CaseFileError,
    as_bool,
    as_float,
    as_int,
    as_list,
    as_optional_str,
    as_str,
    fields,
    load_cases,
    thresholds,
)
from tests.factories import Fakes, make_dev_deps, make_paths, make_update, open_test_db, origin_of
from tests.fakes.clock import FakeClock
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.burst import domain, service
from yordamchi.shared.burst.domain import (
    BurstEvents,
    CloseReason,
    FollowUpDecision,
    FollowUpReason,
    RouteHint,
    RunningTurn,
    TurnInput,
)
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import Attachment, IncomingUpdate, UpdateKind, UpdateStatus
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.domain import COUNTED_OPS
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

CASES_FILE: Final = Path(__file__).with_name("cases.yaml")
THRESHOLDS: Final = thresholds("burst")
LABELS: Final = frozenset(
    {"interpretable", "no_question_needed", "unrelated", "interrupt", "lone_media", "complete_text"}
)
KINDS: Final = frozenset(
    {
        UpdateKind.TEXT,
        UpdateKind.PHOTO,
        UpdateKind.VIDEO,
        UpdateKind.DOCUMENT,
        UpdateKind.AUDIO,
        UpdateKind.VOICE,
        UpdateKind.LINK,
        UpdateKind.EDITED,
        UpdateKind.OTHER,
    }
)
_MIME: Final = {
    UpdateKind.PHOTO: "image/jpeg",
    UpdateKind.VIDEO: "video/mp4",
    UpdateKind.DOCUMENT: "application/pdf",
    UpdateKind.AUDIO: "audio/mpeg",
    UpdateKind.VOICE: "audio/ogg",
}
_OWNER_MESSAGE_OPS: Final = frozenset(op.value for op in COUNTED_OPS)  # anything that shows the owner words
MAX_PASSES: Final = 1_000  # a guard against a replay that never settles, not a limit any case comes near


# --- the case file --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Part:
    t: float
    kind: UpdateKind
    sent_t: float | None = None
    text: str | None = None
    caption: str | None = None
    media_group: str | None = None
    duration_s: int | None = None
    transcript_t: float | None = None
    transcript: str | None = None
    transcript_ok: bool = True
    message_id: int | None = None
    reply_to: int | None = None


@dataclass(frozen=True, slots=True)
class ExpectedBurst:
    parts: tuple[int, ...]
    closes_t: float
    reason: CloseReason
    texts: tuple[str | None, ...] | None = None
    kinds: tuple[UpdateKind, ...] | None = None


@dataclass(frozen=True, slots=True)
class ExpectedFollowUp:
    running_project: str
    route: RouteHint
    merge: bool
    reason: FollowUpReason


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    labels: frozenset[str]
    parts: tuple[Part, ...]
    bursts: tuple[ExpectedBurst, ...]
    follow_up: ExpectedFollowUp | None


def _optional_float(raw: object, where: str) -> float | None:
    return None if raw is None else as_float(raw, where)


def _optional_int(raw: object, where: str) -> int | None:
    return None if raw is None else as_int(raw, where)


def _part(raw: object, where: str) -> Part:
    data = fields(
        raw,
        where,
        required={"t", "kind"},
        optional={
            "sent_t",
            "text",
            "caption",
            "media_group",
            "duration_s",
            "transcript_t",
            "transcript",
            "transcript_ok",
            "message_id",
            "reply_to",
        },
    )
    kind = UpdateKind(as_str(data["kind"], f"{where} kind"))
    if kind not in KINDS:
        msg = f"{where}: kind {kind.value!r} is not a burst part"
        raise CaseFileError(msg)
    part = Part(
        t=as_float(data["t"], f"{where} t"),
        kind=kind,
        sent_t=_optional_float(data.get("sent_t"), f"{where} sent_t"),
        text=as_optional_str(data.get("text"), f"{where} text"),
        caption=as_optional_str(data.get("caption"), f"{where} caption"),
        media_group=as_optional_str(data.get("media_group"), f"{where} media_group"),
        duration_s=_optional_int(data.get("duration_s"), f"{where} duration_s"),
        transcript_t=_optional_float(data.get("transcript_t"), f"{where} transcript_t"),
        transcript=as_optional_str(data.get("transcript"), f"{where} transcript"),
        transcript_ok=as_bool(data.get("transcript_ok", True), f"{where} transcript_ok"),
        message_id=_optional_int(data.get("message_id"), f"{where} message_id"),
        reply_to=_optional_int(data.get("reply_to"), f"{where} reply_to"),
    )
    if part.sent_t is not None and part.sent_t > part.t:
        msg = f"{where}: the owner sends a message before the bot receives it (sent_t <= t)"
        raise CaseFileError(msg)
    is_voice = kind is UpdateKind.VOICE
    if is_voice != (part.duration_s is not None):
        msg = f"{where}: duration_s is required on a voice part and only there"
        raise CaseFileError(msg)
    if not is_voice and (part.transcript_t is not None or part.transcript is not None):
        msg = f"{where}: only a voice part has a transcript"
        raise CaseFileError(msg)
    if (part.transcript_t is None) != (part.transcript is None):
        msg = f"{where}: transcript_t and transcript come together"
        raise CaseFileError(msg)
    if part.transcript_t is not None and part.transcript_t < part.t:
        msg = f"{where}: a transcript cannot come back before its voice note arrived"
        raise CaseFileError(msg)
    if kind in {UpdateKind.TEXT, UpdateKind.LINK} and part.text is None:
        msg = f"{where}: a {kind.value} part needs its text"
        raise CaseFileError(msg)
    if kind is UpdateKind.EDITED and (part.message_id is None or (part.text is None and part.caption is None)):
        msg = f"{where}: an edit names the message_id it edits and carries the new text or caption"
        raise CaseFileError(msg)
    return part


def _expected_burst(raw: object, where: str, part_count: int) -> ExpectedBurst:
    data = fields(raw, where, required={"parts", "closes_t", "reason"}, optional={"texts", "kinds"})
    parts = tuple(as_int(item, f"{where} parts") for item in as_list(data["parts"], f"{where} parts"))
    if not parts or any(index < 0 or index >= part_count for index in parts):
        msg = f"{where}: parts must index this case's parts"
        raise CaseFileError(msg)
    texts: tuple[str | None, ...] | None = None
    if "texts" in data:
        texts = tuple(as_optional_str(item, f"{where} texts") for item in as_list(data["texts"], f"{where} texts"))
    kinds: tuple[UpdateKind, ...] | None = None
    if "kinds" in data:
        kinds = tuple(UpdateKind(as_str(item, f"{where} kinds")) for item in as_list(data["kinds"], f"{where} kinds"))
    for name, values in (("texts", texts), ("kinds", kinds)):
        if values is not None and len(values) != len(parts):
            msg = f"{where}: {name} must have one entry per part"
            raise CaseFileError(msg)
    return ExpectedBurst(
        parts=parts,
        closes_t=as_float(data["closes_t"], f"{where} closes_t"),
        reason=CloseReason(as_str(data["reason"], f"{where} reason")),
        texts=texts,
        kinds=kinds,
    )


def _follow_up(raw: object, where: str) -> ExpectedFollowUp:
    data = fields(raw, where, required={"running_project", "route", "merge", "reason"})
    route = fields(data["route"], f"{where} route", required={"project", "confidence"})
    confidence = as_float(route["confidence"], f"{where} route confidence")
    if not 0.0 <= confidence <= 1.0:
        msg = f"{where}: a confidence is between 0 and 1"
        raise CaseFileError(msg)
    return ExpectedFollowUp(
        running_project=as_str(data["running_project"], f"{where} running_project"),
        route=RouteHint(project_id=as_optional_str(route["project"], f"{where} route project"), confidence=confidence),
        merge=as_bool(data["merge"], f"{where} merge"),
        reason=FollowUpReason(as_str(data["reason"], f"{where} reason")),
    )


def _case(raw: Mapping[str, object]) -> Case:
    case_id = as_str(raw["id"], "case id")
    data = fields(raw, case_id, required={"id", "parts", "expect"}, optional={"labels"})
    labels = frozenset(as_str(item, f"{case_id} labels") for item in as_list(data.get("labels", []), case_id))
    if labels - LABELS:
        msg = f"{case_id}: unknown labels {sorted(labels - LABELS)}"
        raise CaseFileError(msg)
    raw_parts = as_list(data["parts"], f"{case_id} parts")
    parts = tuple(_part(item, f"{case_id} part #{index}") for index, item in enumerate(raw_parts))
    if not parts or any(later.t < earlier.t for earlier, later in itertools.pairwise(parts)):
        msg = f"{case_id}: parts are listed in arrival order (update ids increase with time)"
        raise CaseFileError(msg)
    expect = fields(data["expect"], f"{case_id} expect", required={"bursts"}, optional={"follow_up"})
    raw_bursts = as_list(expect["bursts"], f"{case_id} bursts")
    bursts = tuple(
        _expected_burst(item, f"{case_id} burst #{index}", len(parts)) for index, item in enumerate(raw_bursts)
    )
    follow_up = None if expect.get("follow_up") is None else _follow_up(expect["follow_up"], f"{case_id} follow_up")
    if follow_up is not None and len(bursts) != 2:
        msg = f"{case_id}: a follow-up case closes exactly two bursts (the running turn, then the follow-up)"
        raise CaseFileError(msg)
    return Case(id=case_id, labels=labels, parts=parts, bursts=bursts, follow_up=follow_up)


CASES: Final = tuple(_case(raw) for raw in load_cases(CASES_FILE))


# --- the replay -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, order=True)
class _Event:
    at: float
    order: int  # 0: the part arrives, 1: its transcript comes back
    index: int


@dataclass(frozen=True, slots=True)
class Replay:
    start: datetime
    closed: tuple[TurnInput, ...]  # in close order; at one instant, interrupts first (as `notify` gets them)
    transcribe: frozenset[int]  # update ids whose STT job the burst asked for
    unattached: int  # inbox rows still `received` at the end
    owner_messages: int  # outbox rows that would show the owner text: a question, an answer, an edit
    follow_up: FollowUpDecision | None

    def seconds(self, moment: datetime) -> float:
        return round((moment - self.start).total_seconds(), 3)


def _update(index: int, part: Part, start: datetime) -> IncomingUpdate:
    """Part `index` as the handler builds it: update ids 1, 2, … in arrival order; `sent_t` counts from `start`."""
    update_id = index + 1
    attachment = None
    if part.kind in _MIME:
        attachment = Attachment(
            file_id=f"file-{update_id}", mime=_MIME[part.kind], size_bytes=2048, duration_s=part.duration_s
        )
    update = make_update(
        update_id,
        text=part.text,
        kind=part.kind,
        message_id=part.message_id,
        caption=part.caption,
        attachment=attachment,
    )
    sent_at = None if part.sent_t is None else start + timedelta(seconds=part.sent_t)
    return dataclasses.replace(
        update, media_group_id=part.media_group, reply_to_message_id=part.reply_to, sent_at=sent_at
    )


def _events(case: Case) -> deque[_Event]:
    events = [_Event(part.t, 0, index) for index, part in enumerate(case.parts)]
    events += [
        _Event(part.transcript_t, 1, index) for index, part in enumerate(case.parts) if part.transcript_t is not None
    ]
    return deque(sorted(events))


async def _advance_to(clock: FakeClock, moment: datetime) -> None:
    delta = (moment - clock.now()).total_seconds()
    if delta > 0:
        await clock.advance(delta)
    assert clock.now() >= moment


async def _apply(case: Case, event: _Event, deps: DevDeps, start: datetime) -> BurstEvents:
    part = case.parts[event.index]
    if event.order == 0:
        update = _update(event.index, part, start)
        await inbox.accept_update(deps, origin_of(update), update)
        return await service.attach_received_updates(deps)
    assert part.transcript is not None
    return await service.record_transcript(deps, update_id=event.index + 1, text=part.transcript, ok=part.transcript_ok)


async def _decide(case: Case, deps: DevDeps, closed: tuple[TurnInput, ...]) -> FollowUpDecision | None:
    if case.follow_up is None or len(closed) != 2:
        return None
    snap = await settings.snapshot(deps)
    running, follow_up = closed
    turn = RunningTurn(
        last_part_at=running.last_part_at,
        project_id=case.follow_up.running_project,
        message_ids=running.message_ids,
        media_group_ids=running.media_group_ids,
    )
    return domain.decide_dev_follow_up(
        follow_up,
        turn,
        route=case.follow_up.route,
        settings=domain.window_settings(snap),
        min_confidence=snap[defaults.BURST_MERGE_MIN_CONFIDENCE],
    )


async def replay(case: Case, deps: DevDeps, clock: FakeClock) -> Replay:
    start = clock.now()
    queue = _events(case)
    closed: list[TurnInput] = []
    transcribe: set[int] = set()
    for _ in range(MAX_PASSES):
        due = await service.next_closes_at(deps)
        upcoming = start + timedelta(seconds=queue[0].at) if queue else None
        if upcoming is None and due is None:
            break
        if upcoming is not None and (due is None or upcoming <= due):
            await _advance_to(clock, upcoming)
            instant = queue[0].at
            events = BurstEvents()
            while queue and queue[0].at == instant:
                events += await _apply(case, queue.popleft(), deps, start)
            events += await service.close_due_bursts(deps)
        else:
            assert due is not None
            await _advance_to(clock, due)
            events = await service.close_due_bursts(deps)
        closed.extend(events.closed)
        transcribe.update(part.update_id for part in events.transcribe)
    else:
        msg = f"{case.id}: the replay did not settle in {MAX_PASSES} passes"
        raise AssertionError(msg)
    async with deps.db.read() as s:
        updates = await inbox_store.list_inbound_updates(s, deps.bot.value)
        queued = await outbox_store.list_outbox_messages(s, deps.bot.value)
    return Replay(
        start=start,
        closed=tuple(closed),
        transcribe=frozenset(transcribe),
        unattached=sum(1 for row in updates if row.status == UpdateStatus.RECEIVED.value),
        owner_messages=sum(1 for row in queued if row.op in _OWNER_MESSAGE_OPS),
        follow_up=await _decide(case, deps, tuple(closed)),
    )


# --- reading a replay -----------------------------------------------------------------------------------------------
def _observed(result: Replay) -> list[dict[str, object]]:
    return [
        {
            "parts": [part.update_id - 1 for part in item.parts],
            "closes_t": result.seconds(item.closed_at),
            "reason": item.close_reason.value,
            "texts": [part.text for part in item.parts],
            "kinds": [part.kind.value for part in item.parts],
        }
        for item in result.closed
    ]


def _expected(case: Case, observed: list[dict[str, object]]) -> list[dict[str, object]]:
    """The case's bursts in the observed shape. `texts` and `kinds` are checked only where the case gives them."""
    expected: list[dict[str, object]] = []
    for index, burst in enumerate(case.bursts):
        seen = observed[index] if index < len(observed) else {}
        expected.append(
            {
                "parts": list(burst.parts),
                "closes_t": round(burst.closes_t, 3),
                "reason": burst.reason.value,
                "texts": list(burst.texts) if burst.texts is not None else seen.get("texts"),
                "kinds": [kind.value for kind in burst.kinds] if burst.kinds is not None else seen.get("kinds"),
            }
        )
    return expected


def _arrived_t(case: Case, update_id: int) -> float:
    """When a part became known: its arrival, or for a voice note the instant its transcript came back."""
    part = case.parts[update_id - 1]
    return part.transcript_t if part.transcript_t is not None else part.t


def _wait_after_last_part(item: TurnInput) -> float:
    return round((item.closed_at - item.last_part_at).total_seconds(), 3)


def _label_failures(case: Case, result: Replay) -> list[str]:
    """The UC-02/UC-03 rules a label promises, as messages (empty when they hold)."""
    failures: list[str] = []
    if "lone_media" in case.labels:
        lone_max = THRESHOLDS["lone_photo_proceed_max_s"]
        if len(result.closed) != 1 or not result.closed[0].is_media_only:
            failures.append("lone media: expected one burst of bare media only")
        elif (wait := _wait_after_last_part(result.closed[0])) > lone_max:
            failures.append(f"lone media: proceeds {wait} s after the last part, more than {lone_max} s")
    if "complete_text" in case.labels:
        delay_max = THRESHOLDS["complete_text_added_delay_max_s"]
        texts = [part for item in result.closed for part in item.parts]
        if len(result.closed) != 1 or len(texts) != 1 or not domain.is_complete_text(texts[0].text or ""):
            failures.append("complete text: expected one burst holding one complete text")
        elif (delay := _wait_after_last_part(result.closed[0])) > delay_max:
            failures.append(f"complete text: the window added {delay} s, more than {delay_max} s")
    if "interrupt" in case.labels:
        stops = [item for item in result.closed if item.close_reason is CloseReason.INTERRUPT]
        if not stops:
            failures.append("interrupt: no burst closed as an interrupt")
        for item in stops:
            [part] = item.parts
            if item.interrupt is None:
                failures.append(f"interrupt: burst of part {part.update_id - 1} is no interrupt word")
            if result.seconds(item.closed_at) != round(_arrived_t(case, part.update_id), 3):
                failures.append(f"interrupt: part {part.update_id - 1} did not close the instant it arrived")
        if result.follow_up is not None and result.follow_up.merge:
            failures.append("interrupt: merged into the running turn")
    return failures


# --- the runner -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_case(case: Case, dev_deps: DevDeps, fakes: Fakes) -> None:
    result = await replay(case, dev_deps, fakes.clock)

    observed = _observed(result)
    assert observed == _expected(case, observed)
    if case.follow_up is not None:
        assert result.follow_up == FollowUpDecision(merge=case.follow_up.merge, reason=case.follow_up.reason)
    voice = {index + 1 for index, part in enumerate(case.parts) if part.kind is UpdateKind.VOICE}
    assert result.transcribe == voice  # each voice part started its STT job
    assert result.unattached == 0  # nothing is left behind in the inbox
    assert result.owner_messages == 0  # the burst never talks to the owner: a lone photo proceeds, no question
    assert _label_failures(case, result) == []


async def _replay_in(root: Path, case: Case) -> Replay:
    """One case on its own DB and fakes under `root`, so every case starts from nothing."""
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    try:
        return await replay(case, make_dev_deps(db=db, paths=paths, fakes=fakes), fakes.clock)
    finally:
        await db.dispose()


async def test_the_suite_meets_the_thresholds(tmp_path: Path) -> None:
    assert len(CASES) >= THRESHOLDS["dev_cases_min"]
    results = {case.id: await _replay_in(tmp_path / case.id, case) for case in CASES}

    def subset(label: str) -> list[Case]:
        chosen = [case for case in CASES if label in case.labels]
        assert chosen, f"no case is labelled {label!r}: its threshold would pass on nothing"
        return chosen

    interpretable = subset("interpretable")
    asked_nothing = sum(1 for case in interpretable if results[case.id].owner_messages == 0)
    assert asked_nothing / len(interpretable) >= THRESHOLDS["interpretable_no_question_min"]

    unrelated = subset("unrelated")
    assert all(results[case.id].follow_up is not None for case in unrelated)
    merges = sum(1 for case in unrelated if (decision := results[case.id].follow_up) is not None and decision.merge)
    assert merges <= THRESHOLDS["unrelated_merges_max"]

    per_turn_max = THRESHOLDS["clarifying_questions_per_turn_max"]
    assert all(result.owner_messages <= per_turn_max * len(result.closed) for result in results.values())

    for label in ("complete_text", "lone_media", "interrupt"):
        for case in subset(label):
            assert _label_failures(case, results[case.id]) == [], case.id
