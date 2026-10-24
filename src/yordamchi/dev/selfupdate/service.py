"""Self-update by chat, the dev bot's side (SPEC §3.2.12 bot steps 1–3; UC-19, Q5, R-30…R-33). T57's supervisor does
the rest: build, test, drain, switch, probation, merge or rollback.

- **Request** (`request_self_update`): the working copy `~/Documents/Projects/Yordamchi` must be clean and on `main`,
  and no other self-update may be on its way. Yordamchi cuts `selfupdate/<n>-<slug>` from `main` and queues one
  `selfupdate` turn (`prompts/dev/selfupdate_turn.md`) under the SELF_UPDATE gate policy: FULL, except that pushing
  or merging `main` is forbidden (`dev/gate/domain.py`). Nothing ever runs from the working copy.
- **The turn ends** (`advance_self_updates`, run every `FOLLOW_EVERY_S` and on `turns`' turn-end hook): the files the
  branch changed against `main` are matched against `.protected-core` **of the running release** (fail closed: an
  unreadable file protects everything), and `defaults.py` against its base for a changed safeguard setting.
  - `supervisor/**` together with anything else → refused (`S.SELFUPDATE_SPLIT_NEEDED`, R-32);
  - protected core or a safeguard setting → ONE question (`gate.ask_selfupdate_core`, Q5); "ha" goes on, "yo'q"
    refuses;
  - otherwise, and after a "ha": `updates/<id>/request.json` is written atomically → `S.SELFUPDATE_REQUESTED`.
- **Follow** the supervisor's `updates/<id>/status.json`: the row mirrors it, and its final outcome is told once
  (`reported_at`), in plain Uzbek: version, what changed, tests, health. A rollback the supervisor already alerted is
  not told twice. Run at start too, so an outcome reached while the bot was down (a rollback) is still told.
- **Supervisor self-switch watch** (`kind=supervisor`): the supervisor swaps `supervisor/current` and exits; if no
  heartbeat of the new release arrives within `selfupdate.supervisor_switch_s`, the dev bot points the link back at
  the previous release and restarts the supervisor (`kick_supervisor`, from `bots/dev/app.py`). The old supervisor
  then reports `rolled_back`.

Ports are awaited only outside units of work (A3). Logs carry ids, codes and counts, never request text.
"""

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError, SelfUpdateBusy, SelfUpdateDirty, SelfUpdateNotOnMain
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate.service import CoreAnswer
from yordamchi.dev.projects import service as projects
from yordamchi.dev.selfupdate import domain, store
from yordamchi.dev.selfupdate.domain import (
    FINAL,
    MAIN,
    OPEN,
    SELF_PROJECT,
    WITH_SUPERVISOR,
    ChangePlan,
    UpdateKind,
    UpdateStatus,
)
from yordamchi.dev.selfupdate.models import SelfUpdate
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import TERMINAL, DeliverySource, TurnKind, TurnRequest, TurnStatus
from yordamchi.dev.turns.service import TurnsState
from yordamchi.persistence.uow import Session
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

log = get_log(__name__)

FOLLOW_EVERY_S: Final = 10.0  # how often `advance_self_updates` runs (wiring, T58)
TURN_START_GRACE: Final = timedelta(minutes=10)  # a row whose turn never got written is failed after this
PROMPT: Final = "dev/selfupdate_turn"
FAIL_CLOSED_GLOBS: Final = ("**",)  # no readable `.protected-core`: every change asks
SWITCHED_BACK: Final = "supervisor_switched_back"

type SupervisorKick = Callable[[], Awaitable[bool]]
"""`launchctl kickstart -k gui/<uid>/uz.yordamchi.supervisor`, built by `bots/dev/app.py` (an adapter's call)."""


@dataclass(frozen=True, slots=True)
class Started:
    update_id: str
    branch: str
    turn_id: int


# --- files --------------------------------------------------------------------------------------------------------
def _write_private_file(path: Path, data: bytes) -> None:
    """Atomic 0600 write in a 0700 directory: the supervisor never reads half a request."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> JsonValue:
    try:
        return json_value.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _swap_symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(f".{link.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(os.path.relpath(target, link.parent))
    tmp.replace(link)


def _update_dir(deps: DevDeps, row_id: int) -> Path:
    return deps.paths.updates / domain.update_id(row_id)


# --- telling the owner --------------------------------------------------------------------------------------------
async def _tell(s: Session, deps: DevDeps, text: BotText, *, ref: str) -> None:
    snap = await settings.snapshot(deps)
    await outbox.enqueue_text(
        s,
        bot=deps.bot,
        chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
        text=text,
        kind=Kind.NOTICE,
        ref=ref,
        at=deps.clock.now(),
        max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        about=About(project_id=SELF_PROJECT),
    )


async def _audit(s: Session, deps: DevDeps, row_id: int, data: dict[str, JsonValue]) -> None:
    await audit.record_event(
        s,
        bot=deps.bot,
        at=deps.clock.now(),
        actor=AuditActor.SYSTEM,
        kind=AuditKind.SELF_UPDATE,
        project_id=SELF_PROJECT,
        ref_type="self_update",
        ref_id=domain.update_id(row_id),
        data=data,
    )


async def _close(
    deps: DevDeps, row: SelfUpdate, status: UpdateStatus, *, code: str, text: BotText | None, **values: object
) -> int:
    """The bot's own final outcome (refused or failed before any request): stored and told in one unit of work."""
    now = deps.clock.now()
    async with deps.db.uow() as s:
        moved = await store.move_self_update(
            s,
            row.id,
            from_statuses=[row.status],
            to_status=status.value,
            result_code=code,
            finished_at=now,
            reported_at=now,
            **values,
        )
        if moved:
            if text is not None:
                await _tell(s, deps, text, ref=f"selfupdate-{row.id}-{status.value}")
            await _audit(s, deps, row.id, {"status": status.value, "code": code})
    log.info("selfupdate_closed", update_id=domain.update_id(row.id), status=status.value, code=code)
    return int(moved)


# --- request ------------------------------------------------------------------------------------------------------
async def request_self_update(deps: DevDeps, state: TurnsState, *, text: str, burst_id: int | None = None) -> Started:
    """ "O'zingga X qo'sh" (UC-19): cut the branch and queue the turn. Raises `ProjectNotFound`, `SelfUpdateBusy`,
    `SelfUpdateDirty`, `SelfUpdateNotOnMain`, or the git adapter's `ExternalError`; the owner gets their message."""
    project = await projects.get_project(deps, SELF_PROJECT)
    root = Path(project.path)
    async with deps.db.read() as s:
        following = await store.list_self_updates_to_follow(s, [status.value for status in OPEN])
    open_rows = [row for row in following if UpdateStatus(row.status) in OPEN]
    busy = [row for row in open_rows if row.status != UpdateStatus.AWAITING_CORE.value]
    if busy:
        raise SelfUpdateBusy(project=SELF_PROJECT, update=domain.update_id(busy[0].id))
    status = await deps.git.status(root, timeout=domain.GIT_TIMEOUT_S)
    if status.changed:
        raise SelfUpdateDirty(project=SELF_PROJECT, count=status.changed)
    if status.branch != MAIN:
        raise SelfUpdateNotOnMain(project=SELF_PROJECT, branch=status.branch or "HEAD")
    now = deps.clock.now()
    async with deps.db.uow() as s:
        for stale in open_rows:  # an unanswered core question gives way to the newer request
            await store.move_self_update(
                s,
                stale.id,
                from_statuses=[UpdateStatus.AWAITING_CORE.value],
                to_status=UpdateStatus.REFUSED.value,
                result_code="superseded",
                finished_at=now,
                reported_at=now,
            )
        row = await store.add_self_update(
            s, status=UpdateStatus.WORKING.value, kind=UpdateKind.APP.value, branch="", base_commit=status.head, at=now
        )
        branch = domain.branch_name(row.id, text)
        await store.set_self_update(s, row.id, branch=branch)
    try:
        await deps.git.create_branch(root, branch, timeout=domain.GIT_TIMEOUT_S)
    except ExternalError as error:
        async with deps.db.uow() as s:
            await store.set_self_update(
                s,
                row.id,
                status=UpdateStatus.FAILED.value,
                result_code=error.code.value,
                finished_at=now,
                reported_at=now,
            )
        log.warning("selfupdate_branch_failed", update_id=domain.update_id(row.id), error_code=error.code.value)
        raise
    request = TurnRequest(
        project_id=SELF_PROJECT,
        text=prompts.render(PROMPT, branch=branch, request=text),
        kind=TurnKind.SELFUPDATE,
        burst_id=burst_id,
        source=DeliverySource.OWNER,
    )
    submitted = await turns.submit_turn(deps, state, request)
    async with deps.db.uow() as s:
        await store.set_self_update(s, row.id, turn_id=submitted.turn_id)
        await _tell(s, deps, S.SELFUPDATE_STARTED(project=SELF_PROJECT, branch=branch), ref=f"selfupdate-{row.id}")
        await _audit(s, deps, row.id, {"status": "working", "turn_id": submitted.turn_id, "branch": branch})
    log.info("selfupdate_started", update_id=domain.update_id(row.id), turn_id=submitted.turn_id)
    return Started(update_id=domain.update_id(row.id), branch=branch, turn_id=submitted.turn_id)


# --- follow -------------------------------------------------------------------------------------------------------
async def advance_self_updates(deps: DevDeps, *, kick_supervisor: SupervisorKick | None = None) -> int:
    """One pass over every self-update on its way; returns how many rows changed."""
    async with deps.db.read() as s:
        rows = await store.list_self_updates_to_follow(s, [status.value for status in OPEN])
    changed = 0
    for row in rows:
        status = UpdateStatus(row.status)
        if status is UpdateStatus.WORKING:
            changed += await _after_turn(deps, row)
        elif status is UpdateStatus.AWAITING_CORE:
            changed += await _after_answer(deps, row)
        elif status in WITH_SUPERVISOR or (status in FINAL and row.reported_at is None):
            changed += await _follow(deps, row, kick_supervisor)
    return changed


async def on_turn_ended(deps: DevDeps, project_id: str) -> None:
    """`turns`' turn-end hook (composed with the digest's in `bots/dev/app.py`): a Yordamchi turn ended."""
    if project_id == SELF_PROJECT:
        await advance_self_updates(deps)


async def _turn_summary(deps: DevDeps, turn_id: int) -> str:
    for summary in await turns.list_recent_turns(deps, SELF_PROJECT, limit=20):
        if summary.turn_id == turn_id:
            return redact.scrubbed(summary.natija or summary.title)[: domain.SUMMARY_MAX]
    return ""


async def _after_turn(deps: DevDeps, row: SelfUpdate) -> int:
    if row.turn_id is None:
        if deps.clock.now() - row.created_at > TURN_START_GRACE:
            return await _close(deps, row, UpdateStatus.FAILED, code="turn_missing", text=None)
        return 0
    turn = (await turns.turn_statuses(deps, [row.turn_id])).get(row.turn_id)
    if turn is not None and turn not in TERMINAL:
        return 0
    if turn is not TurnStatus.DONE:
        code = f"turn_{turn.value if turn is not None else 'missing'}"
        text = S.SELFUPDATE_TURN_FAILED(project=SELF_PROJECT, reason=domain.reason_text(code), branch=row.branch)
        return await _close(deps, row, UpdateStatus.FAILED, code=code, text=text)
    return await _evaluate(deps, row)


async def _protected_globs(deps: DevDeps) -> tuple[str, ...]:
    text = await asyncio.to_thread(_read_text, deps.paths.current_release / domain.PROTECTED_CORE)
    globs = domain.protected_globs(text) if text is not None else ()
    if not globs:
        log.warning("selfupdate_protected_core_unreadable")
    return globs or FAIL_CLOSED_GLOBS


async def _defaults_at(deps: DevDeps, root: Path, ref: str) -> str | None:
    files = await deps.git.tree_files(
        root, ref, domain.SETTINGS_DIR, max_bytes=domain.DEFAULTS_MAX_BYTES, timeout=domain.GIT_TIMEOUT_S
    )
    data = files.get(domain.DEFAULTS_NAME)
    return data.decode("utf-8", errors="replace") if data is not None else None


async def _evaluate(deps: DevDeps, row: SelfUpdate) -> int:
    """SPEC §3.2.12 bot step 2: what did the branch change, and does it ask the owner?"""
    project = await projects.get_project(deps, SELF_PROJECT)
    root = Path(project.path)
    try:
        branch = await deps.git.current_branch(root, timeout=domain.GIT_TIMEOUT_S)
        head = await deps.git.head(root, timeout=domain.GIT_TIMEOUT_S)
        if branch != row.branch:
            text = S.SELFUPDATE_TURN_FAILED(
                project=SELF_PROJECT, reason=domain.reason_text("branch_lost"), branch=row.branch
            )
            return await _close(deps, row, UpdateStatus.FAILED, code="branch_lost", text=text)
        if head is None or row.base_commit is None or head == row.base_commit:
            text = S.SELFUPDATE_NO_CHANGES(project=SELF_PROJECT)
            return await _close(deps, row, UpdateStatus.REFUSED, code="no_changes", text=text)
        changes = await deps.git.changed_files(root, row.base_commit, timeout=domain.GIT_TIMEOUT_S)
        before = await _defaults_at(deps, root, row.base_commit)
        after = await _defaults_at(deps, root, head)
    except ExternalError as error:
        log.warning("selfupdate_git_unreadable", update_id=domain.update_id(row.id), error_code=error.code.value)
        return 0  # tried again on the next pass
    files = [path for change in changes for path in (change.path, change.old_path) if path]
    plan = domain.plan_change(files, await _protected_globs(deps), defaults_before=before, defaults_after=after)
    if plan.split_needed:
        text = S.SELFUPDATE_SPLIT_NEEDED(project=SELF_PROJECT, branch=row.branch)
        return await _close(
            deps,
            row,
            UpdateStatus.REFUSED,
            code="split_needed",
            text=text,
            commit=head,
            changed_files_json=list(plan.files),
        )
    summary = await _turn_summary(deps, row.turn_id or 0)
    kind = plan.kind or UpdateKind.APP
    async with deps.db.uow() as s:
        await store.set_self_update(
            s,
            row.id,
            commit=head,
            kind=kind.value,
            changed_files_json=list(plan.files[: domain.CHANGED_FILES_KEPT]),
            touches_core=plan.asks_owner,
            summary=summary,
        )
    if not plan.asks_owner:
        return await _request(deps, row.id, plan=plan, kind=kind, commit=head, summary=summary, decision_id=None)
    decision_id = await gate.ask_selfupdate_core(
        deps,
        project_id=SELF_PROJECT,
        key=f"selfupdate:{domain.update_id(row.id)}:{head}",
        summary=summary or domain.changes_text(plan.files),
        files=domain.core_summary(plan),
    )
    async with deps.db.uow() as s:
        moved = await store.move_self_update(
            s,
            row.id,
            from_statuses=[UpdateStatus.WORKING.value],
            to_status=UpdateStatus.AWAITING_CORE.value,
            decision_id=decision_id,
        )
        if moved:
            await _audit(s, deps, row.id, {"status": "awaiting_core", "decision_id": decision_id})
    log.info("selfupdate_core_asked", update_id=domain.update_id(row.id), decision_id=decision_id)
    return int(moved)


async def _request(
    deps: DevDeps,
    row_id: int,
    *,
    plan: ChangePlan,
    kind: UpdateKind,
    commit: str,
    summary: str,
    decision_id: int | None,
) -> int:
    """SPEC §3.2.12 bot step 3: `updates/<id>/request.json` for the supervisor, then `requested`."""
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        row = await store.get_self_update(s, row_id)
    if row is None:
        return 0
    now = deps.clock.now()
    body = domain.request_json(
        update=domain.update_id(row_id),
        kind=kind,
        commit=commit,
        branch=row.branch,
        summary=summary,
        plan=plan,
        decision_id=decision_id,
        at=now,
        health_timeout_s=snap[defaults.SELFUPDATE_HEALTH_TIMEOUT_S],
        drain_max_min=snap[defaults.SELFUPDATE_DRAIN_MAX_MIN],
    )
    path = _update_dir(deps, row_id) / "request.json"
    await asyncio.to_thread(_write_private_file, path, json_value.dumps(body).encode())
    async with deps.db.uow() as s:
        moved = await store.move_self_update(
            s,
            row_id,
            from_statuses=[UpdateStatus.WORKING.value, UpdateStatus.AWAITING_CORE.value],
            to_status=UpdateStatus.REQUESTED.value,
            requested_at=now,
        )
        if moved:
            text = S.SELFUPDATE_REQUESTED(project=SELF_PROJECT, files=len(plan.files))
            await _tell(s, deps, text, ref=f"selfupdate-{row_id}-requested")
            await _audit(s, deps, row_id, {"status": "requested", "kind": kind.value, "core": plan.asks_owner})
    log.info("selfupdate_requested", update_id=domain.update_id(row_id), kind=kind.value, files=len(plan.files))
    return int(moved)


async def _after_answer(deps: DevDeps, row: SelfUpdate) -> int:
    if row.decision_id is None:
        return 0
    answer, newest = await gate.take_selfupdate_core(deps, row.decision_id)
    if newest != row.decision_id:
        async with deps.db.uow() as s:
            await store.set_self_update(s, row.id, decision_id=newest)
    if answer is CoreAnswer.PENDING:
        return 0
    if answer is CoreAnswer.APPROVED and row.commit is not None:
        files = tuple(item for item in _strings(row.changed_files_json))
        plan = ChangePlan(kind=UpdateKind(row.kind), files=files, core=files)
        return await _request(
            deps,
            row.id,
            plan=plan,
            kind=UpdateKind(row.kind),
            commit=row.commit,
            summary=row.summary or "",
            decision_id=newest,
        )
    # "yo'q": the gate already answered the owner. A question that is gone is told here.
    text = (
        None
        if answer is CoreAnswer.DECLINED
        else S.SELFUPDATE_REFUSED(project=SELF_PROJECT, reason=domain.reason_text("core_declined"))
    )
    return await _close(deps, row, UpdateStatus.REFUSED, code="core_declined", text=text)


def _strings(raw: JsonValue) -> list[str]:
    return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


async def _follow(deps: DevDeps, row: SelfUpdate, kick: SupervisorKick | None) -> int:
    raw = await asyncio.to_thread(_read_json, _update_dir(deps, row.id) / "status.json")
    outcome = domain.read_outcome(raw)
    if outcome is None:
        return 0
    kind = UpdateKind(row.kind)
    if kind is UpdateKind.SUPERVISOR and kick is not None and row.result_code != SWITCHED_BACK:
        await _watch_supervisor(deps, row, outcome, kick)
    now = deps.clock.now()
    values: dict[str, object] = {"version": outcome.version or row.version}
    count = outcome.tests.get("supervisor" if kind is UpdateKind.SUPERVISOR else "app")
    if count is not None:
        values["tests_count"] = count
    if not outcome.final:
        if outcome.status.value == row.status:
            return 0
        async with deps.db.uow() as s:
            await store.set_self_update(s, row.id, status=outcome.status.value, **values)
        return 1
    async with deps.db.uow() as s:
        await store.set_self_update(
            s, row.id, status=outcome.status.value, result_code=outcome.code or None, finished_at=now, **values
        )
        if not await store.mark_self_update_reported(s, row.id, at=now):
            return 1
        text = domain.outcome_text(
            outcome,
            kind=kind,
            branch=row.branch,
            summary=row.summary or "",
            files=_strings(row.changed_files_json),
        )
        if text is not None:
            await _tell(s, deps, text, ref=f"selfupdate-{row.id}-outcome")
        await _audit(s, deps, row.id, {"status": outcome.status.value, "code": outcome.code, "told": text is not None})
    log.info("selfupdate_outcome", update_id=domain.update_id(row.id), status=outcome.status.value, code=outcome.code)
    return 1


async def _watch_supervisor(deps: DevDeps, row: SelfUpdate, outcome: domain.Outcome, kick: SupervisorKick) -> None:
    """kind=supervisor: no heartbeat of the new release within `selfupdate.supervisor_switch_s` → switch it back."""
    snap = await settings.snapshot(deps)
    beat = await asyncio.to_thread(_read_json, deps.paths.run / "supervisor.heartbeat.json")
    release = beat.get("release") if isinstance(beat, dict) else None
    stamp = beat.get("last_tick_at") if isinstance(beat, dict) else None
    try:
        beat_at = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        beat_at = None
    wait_s = snap[defaults.SELFUPDATE_SUPERVISOR_SWITCH_S]
    failed = domain.supervisor_switch_failed(
        outcome,
        beat_release=release if isinstance(release, str) else None,
        beat_at=beat_at,
        now=deps.clock.now(),
        wait_s=wait_s,
    )
    if not failed:
        return
    previous = deps.paths.supervisor / "releases" / outcome.previous
    if not outcome.previous or not previous.is_dir():
        log.error("selfupdate_supervisor_previous_missing", update_id=domain.update_id(row.id))
        return
    await asyncio.to_thread(_swap_symlink, deps.paths.supervisor / "current", previous)
    kicked = await kick()
    async with deps.db.uow() as s:
        await store.set_self_update(s, row.id, result_code=SWITCHED_BACK)
        text = S.SELFUPDATE_SUPERVISOR_SWITCHED_BACK(
            project=SELF_PROJECT, version=outcome.version, seconds=wait_s, previous=outcome.previous
        )
        await _tell(s, deps, text, ref=f"selfupdate-{row.id}-switched-back")
        await _audit(s, deps, row.id, {"status": "switched_back", "kicked": kicked})
    log.error("selfupdate_supervisor_switched_back", update_id=domain.update_id(row.id), kicked=kicked)
