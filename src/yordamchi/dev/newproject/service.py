"""A new project from the owner's description (SPEC §3.2.8, §3.2.6 step 4; UC-14; NFR-SEC-11; T53).

How the pieces fit:
- **Draft.** `start_draft` takes the burst routing called `new_project`; `take_message` takes each later burst an
  open draft owns (routing step 4, before the LLM: `domain.pick_draft`). Parts are appended until "tamom" / "boshla"
  or `newproject.draft_silence_s` of silence (`tend_drafts`, the body of `run_drafts`).
- **Folder.** Closing moves the draft to `creating` and starts `_create` in the background, so the router never waits
  on the disk or GitHub: the folder under `projects.root` (the first free of `<Name>`, `<Name>2`, …, checked against
  the folders, the registered projects and the GitHub account; an existing folder is never touched), `git init -b
  main`, the project registered (`projects.sync_projects`), then the private GitHub repository (`_ensure_repo`): made
  by Yordamchi before the first push, without a question (NFR-SEC-11), and retried before each later turn if it
  failed.
- **Plan.** The plan turn (`turns.submit`, kind `plan`: its session runs under the gate's PLAN_ONLY policy, so only
  `docs/**`, `README.md` and `.gitignore` can be written) is queued in the unit of work that moves the draft to
  `planning`. Messages meanwhile steer that turn. `on_turn_ended` (turns' hook) and `tend_drafts` see it end; the
  `docs/*.md` files (copied, scrubbed) and `S.NEWPROJECT_PLAN_READY` with "Rejani tasdiqlaysizmi?" go out, and the
  draft waits in `awaiting_approval`.
- **Approval.** No buttons: `take_message` reads the answer. "ha" → the build turn (kind `build`, FULL policy);
  "o'zgartir: …" or other words → a revision turn (`plan` again, then step 5 again); "yo'q" → cancelled, the folder
  and the repository stay. The build turn's end → `done`.

Ports (git, GitHub, the disk) are awaited only outside units of work (A3). Every state change is conditional, so a
racing "tamom" and silence timer, or two "ha"s, act once. Logs carry ids, codes and counts, never text.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from yordamchi import prompts
from yordamchi.core import aio, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError, NewProjectFolderFailed, ProjectNotFound, YordamchiError
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText, join
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate.domain import PendingItem
from yordamchi.dev.newproject import domain, store
from yordamchi.dev.newproject.domain import DraftPart, DraftStatus, OpenDraft, PlanVerdict
from yordamchi.dev.newproject.models import ProjectDraft
from yordamchi.dev.projects import domain as projects_domain
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import Project
from yordamchi.dev.routing import domain as routing_domain
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import DeliverySource, TurnKind, TurnRequest, TurnStatus
from yordamchi.dev.turns.service import TurnsState
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind, Op
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

_OPEN: Final = sorted(status.value for status in domain.OPEN)
_WATCHED: Final = sorted(status.value for status in (*domain.PLANNING, DraftStatus.BUILDING))
_RECENT_TURNS: Final = 10  # turns of a project read to find the plan turn's `Natija`


# --- in-memory state ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class NewProjectState:
    """Built once in `bots/dev/app.py`, next to the `TurnsState` the plan and build turns are queued on. `creating`
    holds the drafts whose folder is being made now, so the timer never starts a second creation of one."""

    turns: TurnsState
    creating: set[int] = field(default_factory=set)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, name: str, coro: Coroutine[object, object, None]) -> None:
        """`core.aio.spawn`, remembered until done, so `shutdown_drafts` leaves nothing running."""
        task = aio.spawn(name, coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


class DraftOutcome(StrEnum):
    NOT_TAKEN = "not_taken"  # not a draft's message: routing goes on with it
    OPENED = "opened"
    ADDED = "added"
    CLOSED = "closed"  # the description is complete; the project is being made
    EMPTY = "empty"  # "tamom" with nothing described yet
    STEERED = "steered"  # went into the running plan turn
    BUILDING = "building"
    REVISING = "revising"
    CANCELLED = "cancelled"
    ASKED = "asked"  # several drafts could take it, or "o'zgartir" said nothing: the owner was asked


@dataclass(frozen=True, slots=True)
class DraftAnswer:
    """What a message did to a draft. Every reply to the owner is already queued."""

    outcome: DraftOutcome
    draft_id: int | None = None
    project_id: str | None = None
    turn_id: int | None = None


_NOT_TAKEN: Final = DraftAnswer(DraftOutcome.NOT_TAKEN)


# --- small helpers --------------------------------------------------------------------------------------------------
async def _tell(
    s: Session,
    deps: DevDeps,
    snap: Snapshot,
    text: BotText,
    *,
    ref: str,
    project_id: str | None = None,
    kind: Kind = Kind.NOTICE,
    priority: int | None = None,
) -> None:
    """One owner message, queued in the caller's transaction; `about` lets a reply to it find the project."""
    await outbox.enqueue_text(
        s,
        bot=deps.bot,
        chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
        text=text,
        kind=kind,
        ref=f"newproject-{ref}",
        at=deps.clock.now(),
        max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        priority=priority,
        about=About(project_id=project_id),
    )


def _status(row: ProjectDraft) -> DraftStatus:
    return DraftStatus(row.status)


def _parts(row: ProjectDraft) -> tuple[DraftPart, ...]:
    return domain.parts_from_json(row.parts_json)


def _raw_text(item: TurnInput) -> str:
    """The owner's words in the burst, without labels: what an answer word is read from."""
    return " ".join((part.text or "").strip() for part in item.parts).strip()


def _scrubbed(part: DraftPart) -> DraftPart:
    return replace(part, text=redact.scrubbed(part.text))


def _root(deps: DevDeps, snap: Snapshot) -> Path:
    return Path(projects_domain.projects_root(snap[defaults.PROJECTS_ROOT], home=PurePosixPath(deps.paths.home)))


def _shown_path(snap: Snapshot, project_id: str) -> str:
    """The folder as the owner knows it (`~/Documents/Projects/X`), never this Mac's absolute home path."""
    return f"{snap[defaults.PROJECTS_ROOT].rstrip('/')}/{project_id}"


async def _get(deps: DevDeps, draft_id: int) -> ProjectDraft | None:
    async with deps.db.read() as s:
        return await store.get_draft(s, draft_id)


@dataclass(frozen=True, slots=True)
class _Signals:
    named: tuple[str, ...]
    targeted: tuple[str, ...]
    reply_project: str | None


async def _signals(deps: DevDeps, item: TurnInput) -> _Signals:
    """Routing's first two signals for this burst: the projects it names, and the project of the message it replies
    to (`message_links`)."""
    mentions = routing_domain.find_mentions(_raw_text(item), await projects.list_projects(deps))
    reply_to = next((part.reply_to_message_id for part in reversed(item.parts) if part.reply_to_message_id), None)
    link = await outbox.find_message_link(deps, chat_id=item.chat_id, message_id=reply_to) if reply_to else None
    return _Signals(mentions.named, mentions.targeted, link.about.project_id if link is not None else None)


# --- the draft ------------------------------------------------------------------------------------------------------
async def start_draft(deps: DevDeps, state: NewProjectState, origin: OwnerOrigin, item: TurnInput) -> DraftAnswer:
    """Routing said `new_project` ("yangi loyiha: …"): open a draft with this burst, or add it to the one being
    collected. A burst that ends with "tamom" also closes it."""
    require_owner_origin(origin, deps.bot)
    burst = domain.part_of(item)
    part = _scrubbed(burst.part) if burst.part is not None else None
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        collecting = await store.list_drafts_with_status(s, [DraftStatus.COLLECTING.value])
        if collecting:
            draft_id, outcome = collecting[0].id, DraftOutcome.ADDED
            if part is not None:
                await store.add_draft_part(
                    s, draft_id, part=domain.parts_json([part])[0], at=part.at, statuses=[DraftStatus.COLLECTING.value]
                )
        else:
            parts = [part] if part is not None else []
            row = await store.add_draft(
                s,
                status=DraftStatus.COLLECTING.value,
                parts=domain.parts_json(parts),
                opened_at=now,
                last_part_at=part.at if part is not None else now,
            )
            draft_id, outcome = row.id, DraftOutcome.OPENED
            text = domain.collecting_text(silence_s=snap[defaults.NEWPROJECT_DRAFT_SILENCE_S])
            await _tell(s, deps, snap, text, ref=f"{draft_id}-collecting", kind=Kind.ECHO)
    if outcome is DraftOutcome.OPENED:
        log.info("newproject_draft_opened", draft_id=draft_id)
    else:
        log.info("newproject_part_added", draft_id=draft_id)
    if burst.closes:
        return await _close(deps, state, draft_id, silence=False)
    return DraftAnswer(outcome, draft_id)


async def take_message(deps: DevDeps, state: NewProjectState, origin: OwnerOrigin, item: TurnInput) -> DraftAnswer:
    """Routing step 4 (SPEC §3.2.6): an open draft takes the owner's message unless another project is named
    (`domain.pick_draft`). While collecting it is description ("tamom" closes it, "yo'q" drops it); while the
    folder is made it joins the description; while the plan is written it steers the plan turn; while the plan waits
    it is the answer: "ha" builds, "o'zgartir: …" or other words revise, "yo'q" cancels. `NOT_TAKEN`: route it as
    usual. Only an owner update may answer (`origin`)."""
    require_owner_origin(origin, deps.bot)
    async with deps.db.read() as s:
        rows = await store.list_drafts_with_status(s, _OPEN)
    if not rows:
        return _NOT_TAKEN
    signals = await _signals(deps, item)
    views = [OpenDraft(row.id, _status(row), row.project_id) for row in rows]
    pick = domain.pick_draft(
        views,
        named=signals.named,
        targeted=signals.targeted,
        reply_project=signals.reply_project,
        starts_new=domain.starts_project(_raw_text(item)),
    )
    if pick is None:
        return _NOT_TAKEN
    if pick.draft_id is None:
        snap = await settings.snapshot(deps)
        async with deps.db.uow() as s:
            await _tell(s, deps, snap, domain.which_text(pick.choices), ref=f"which-{item.burst_id}", kind=Kind.ECHO)
        return DraftAnswer(DraftOutcome.ASKED)
    for _ in range(2):  # a lost race (the draft moved on meanwhile) is handled once more on its new state
        row = await _get(deps, pick.draft_id)
        if row is None or _status(row) not in domain.OPEN:
            return _NOT_TAKEN
        answer = await _dispatch(deps, state, row, item, names=signals.named)
        if answer is not None:
            return answer
    return _NOT_TAKEN


async def _dispatch(
    deps: DevDeps, state: NewProjectState, row: ProjectDraft, item: TurnInput, *, names: Sequence[str]
) -> DraftAnswer | None:
    status = _status(row)
    if status is DraftStatus.COLLECTING:
        return await _collect(deps, state, row, item, names=names)
    if status is DraftStatus.CREATING:
        return await _add_part(deps, row, domain.part_of(item), statuses=[DraftStatus.CREATING])
    if status in domain.PLANNING:
        return await _steer_plan(deps, state, row, item)
    return await _answer_plan(deps, state, row, item, names=names)


async def _add_part(
    deps: DevDeps, row: ProjectDraft, burst: domain.Burst, *, statuses: Sequence[DraftStatus]
) -> DraftAnswer | None:
    """One more part of the description, while the draft is still in `statuses`; None when it moved on."""
    if burst.part is not None:
        part = _scrubbed(burst.part)
        async with deps.db.uow() as s:
            added = await store.add_draft_part(
                s, row.id, part=domain.parts_json([part])[0], at=part.at, statuses=[status.value for status in statuses]
            )
        if not added:
            return None
        log.info("newproject_part_added", draft_id=row.id)
    return DraftAnswer(DraftOutcome.ADDED, row.id, row.project_id)


async def _collect(
    deps: DevDeps, state: NewProjectState, row: ProjectDraft, item: TurnInput, *, names: Sequence[str]
) -> DraftAnswer | None:
    burst = domain.part_of(item)
    if not burst.closes and domain.is_cancel(_raw_text(item), names=names):
        return await _cancel(deps, row, from_status=DraftStatus.COLLECTING)
    added = await _add_part(deps, row, burst, statuses=[DraftStatus.COLLECTING])
    if added is None:
        return None
    if burst.closes:
        return await _close(deps, state, row.id, silence=False)
    return added


async def _close(deps: DevDeps, state: NewProjectState, draft_id: int, *, silence: bool) -> DraftAnswer:
    """The description is complete ("tamom", or the silence): make the project. A description with nothing in it
    waits for one on "tamom" and is dropped after the silence."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.get_draft(s, draft_id)
        if row is None or _status(row) is not DraftStatus.COLLECTING:
            return DraftAnswer(DraftOutcome.CLOSED, draft_id)
        if domain.has_content(_parts(row)):
            await store.move_draft(
                s, draft_id, from_statuses=[DraftStatus.COLLECTING.value], to_status=DraftStatus.CREATING.value
            )
            outcome = DraftOutcome.CLOSED
        elif silence:
            await store.move_draft(
                s,
                draft_id,
                from_statuses=[DraftStatus.COLLECTING.value],
                to_status=DraftStatus.CANCELLED.value,
                closed_at=now,
            )
            await _tell(s, deps, snap, S.NEWPROJECT_EMPTY_CANCELLED(), ref=f"{draft_id}-empty-cancelled")
            outcome = DraftOutcome.CANCELLED
        else:
            await _tell(s, deps, snap, S.NEWPROJECT_EMPTY(), ref=f"{draft_id}-empty-{now.timestamp():.0f}")
            outcome = DraftOutcome.EMPTY
    log.info("newproject_draft_closed", draft_id=draft_id, silence=silence, outcome=outcome.value)
    if outcome is DraftOutcome.CLOSED:
        _start_creating(deps, state, draft_id)
    return DraftAnswer(outcome, draft_id)


async def _cancel(deps: DevDeps, row: ProjectDraft, *, from_status: DraftStatus) -> DraftAnswer | None:
    """ "yo'q": the draft is dropped. Whatever was made (the folder, the repository) stays as it is."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    project = row.project_id
    text = S.NEWPROJECT_PROJECT_CANCELLED(project=project) if project else S.NEWPROJECT_CANCELLED()
    async with deps.db.uow() as s:
        moved = await store.move_draft(
            s, row.id, from_statuses=[from_status.value], to_status=DraftStatus.CANCELLED.value, closed_at=now
        )
        if moved:
            await _tell(s, deps, snap, text, ref=f"{row.id}-cancelled", project_id=project)
    if not moved:
        return None
    log.info("newproject_cancelled", draft_id=row.id, project=project, stage=from_status.value)
    return DraftAnswer(DraftOutcome.CANCELLED, row.id, project)


# --- the folder and the repository ----------------------------------------------------------------------------------
def _start_creating(deps: DevDeps, state: NewProjectState, draft_id: int) -> None:
    if draft_id in state.creating:
        return
    state.creating.add(draft_id)
    state.spawn(f"newproject_create_{draft_id}", _run_create(deps, state, draft_id))


async def _run_create(deps: DevDeps, state: NewProjectState, draft_id: int) -> None:
    try:
        await _create(deps, state, draft_id)
    finally:
        state.creating.discard(draft_id)


def _entries(root: Path) -> set[str]:
    """Every name directly in `projects.root`, files and hidden ones included: none of them may be reused."""
    try:
        return {entry.name for entry in root.iterdir()}
    except OSError:
        return set()


def _is_fresh_repo(folder: Path) -> bool:
    """The folder this draft made before a restart: nothing in it but `.git`."""
    try:
        return folder.is_dir() and not folder.is_symlink() and {entry.name for entry in folder.iterdir()} <= {".git"}
    except OSError:
        return False


def _make_dir(root: Path, name: str) -> None:
    """`mkdir` that fails on any existing entry (APFS compares names case-insensitively, so does this)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / name).mkdir()


async def _github_owner(deps: DevDeps, *, timeout: float) -> str | None:
    """The `gh` account the repository goes under; None (and only folders count for names) when unknown."""
    try:
        return await deps.github.auth_user(timeout=timeout)
    except ExternalError as error:
        log.warning("newproject_github_unknown", error_code=error.code.value)
        return None


async def _free_name(deps: DevDeps, owner: str | None, wanted: str, taken: set[str], *, timeout: float) -> str:
    """The first of `<wanted>`, `<wanted>2`, … that no folder, project or repository of the account has."""
    for checks, candidate in enumerate(domain.name_candidates(wanted, taken)):
        if owner is None or checks >= domain.GITHUB_NAME_CHECKS:
            return candidate
        try:
            found = await deps.github.view_repo(owner, candidate, timeout=timeout)
        except ExternalError as error:
            log.warning("newproject_github_unknown", error_code=error.code.value)
            return candidate
        if found is None:
            return candidate
    raise NewProjectFolderFailed(name=wanted)  # unreachable: the candidates never run out


async def _make_folder(deps: DevDeps, row: ProjectDraft, root: Path, wanted: str, *, timeout: float) -> str:
    """The project's folder, made empty and `git init -b main`; returns its name. A folder this draft made before a
    restart (`proposed_name`, nothing in it but `.git`) is used again; any other existing entry is never touched."""
    if row.proposed_name is not None and await asyncio.to_thread(_is_fresh_repo, root / row.proposed_name):
        name = row.proposed_name
    else:
        owner = await _github_owner(deps, timeout=timeout)
        taken = await asyncio.to_thread(_entries, root)
        taken |= {project.id for project in await projects.list_projects(deps)}  # a vanished project keeps its name
        for _ in range(domain.NAME_ATTEMPTS):
            name = await _free_name(deps, owner, wanted, taken, timeout=timeout)
            async with deps.db.uow() as s:
                await store.set_draft(s, row.id, proposed_name=name)
            try:
                await asyncio.to_thread(_make_dir, root, name)
            except FileExistsError:
                taken.add(name)  # it appeared after the check: the next free name
                continue
            except OSError:
                raise NewProjectFolderFailed(name=name) from None
            break
        else:
            raise NewProjectFolderFailed(name=wanted)
    await deps.git.init(root / name, branch=domain.DEFAULT_BRANCH, timeout=domain.GIT_TIMEOUT_S)
    return name


async def _ensure_repo(
    deps: DevDeps, snap: Snapshot, draft_id: int, project: Project
) -> tuple[str | None, BotText | None]:
    """The project's private GitHub repository, made now if `origin` has none yet: before the first push, under the
    logged-in `gh` account, never asked about (NFR-SEC-11). Returns its URL, or why there is none (the turn still
    runs; the next turn tries again)."""
    row = await _get(deps, draft_id)
    if row is not None and row.repo_url:
        return row.repo_url, None
    folder = Path(project.path)
    timeout = snap[defaults.TIMEOUTS_GITHUB_S]
    try:
        url = await deps.git.remote_url(folder, remote=domain.REMOTE, timeout=domain.GIT_TIMEOUT_S)
        if url is None:
            owner = await deps.github.auth_user(timeout=timeout)
            if owner is None:
                log.warning("newproject_repo_skipped", project=project.id, reason="logged_out")
                return None, S.NEWPROJECT_REPO_FAILED(why=S.NEWPROJECT_GITHUB_LOGGED_OUT())
            repo = await deps.github.create_repo(owner, project.id, source=folder, private=True, timeout=timeout)
            url = repo.url
            log.info("newproject_repo_created", project=project.id, private=repo.private)
    except ExternalError as error:
        log.warning("newproject_repo_failed", project=project.id, error_code=error.code.value)
        return None, S.NEWPROJECT_REPO_FAILED(why=S.ERRORS[error.code](**error.params))
    async with deps.db.uow() as s:
        await store.set_draft(s, draft_id, repo_url=url)
    return url, None


async def _fail(deps: DevDeps, draft_id: int, error: YordamchiError) -> None:
    snap = await settings.snapshot(deps)
    why = S.ERRORS[error.code](**error.params)
    async with deps.db.uow() as s:
        moved = await store.move_draft(
            s,
            draft_id,
            from_statuses=[DraftStatus.CREATING.value],
            to_status=DraftStatus.FAILED.value,
            error_code=error.code.value,
            closed_at=deps.clock.now(),
        )
        if moved:
            await _tell(s, deps, snap, S.NEWPROJECT_FAILED(why=why), ref=f"{draft_id}-failed")
    log.warning("newproject_failed", draft_id=draft_id, error_code=error.code.value)


async def _create(deps: DevDeps, state: NewProjectState, draft_id: int) -> None:
    """`creating` → `planning`: the folder, git, the registry, the repository, then the plan turn."""
    row = await _get(deps, draft_id)
    if row is None or _status(row) is not DraftStatus.CREATING:
        return
    snap = await settings.snapshot(deps)
    wanted: str | None = None
    try:
        if row.project_id is None:
            wanted = domain.propose_name(domain.description(_parts(row)))
            name = await _make_folder(deps, row, _root(deps, snap), wanted, timeout=snap[defaults.TIMEOUTS_GITHUB_S])
            await projects.sync_projects(deps)  # registers the new folder now, not at the next scan
            project = await projects.get_project(deps, name)
            async with deps.db.uow() as s:
                await store.set_draft(s, draft_id, project_id=project.id)
            log.info("newproject_folder_created", draft_id=draft_id, project=project.id, renamed=name != wanted)
        else:
            project = await projects.get_project(deps, row.project_id)
    except (ExternalError, ProjectNotFound, NewProjectFolderFailed) as error:
        await _fail(deps, draft_id, error)
        return
    repo_url, repo_problem = await _ensure_repo(deps, snap, draft_id, project)
    text = domain.planning_text(
        project.id,
        path=_shown_path(snap, project.id),
        wanted=wanted,
        plan_max_min=snap[defaults.NEWPROJECT_PLAN_MAX_MIN],
        repo_url=repo_url,
        repo_problem=repo_problem,
    )
    now = deps.clock.now()
    async with deps.db.uow() as s:
        current = await store.get_draft(s, draft_id)  # parts added while the folder was made are in the plan too
        moved = current is not None and await store.move_draft(
            s, draft_id, from_statuses=[DraftStatus.CREATING.value], to_status=DraftStatus.PLANNING.value
        )
        if current is not None and moved:
            plan = prompts.render(
                "dev/newproject_plan", name=project.id, description=domain.description(_parts(current))
            )
            request = TurnRequest(project_id=project.id, text=plan, kind=TurnKind.PLAN, source=DeliverySource.SYSTEM)
            submitted = await turns.submit(s, request=request, at=now)
            await store.set_draft(s, draft_id, plan_turn_id=submitted.turn_id)
            await _tell(s, deps, snap, text, ref=f"{draft_id}-planning", project_id=project.id)
    if moved:
        state.turns.poke()
        log.info("newproject_plan_queued", draft_id=draft_id, project=project.id, repo=repo_url is not None)


# --- the plan turn and the answer -----------------------------------------------------------------------------------
async def _steer_plan(deps: DevDeps, state: NewProjectState, row: ProjectDraft, item: TurnInput) -> DraftAnswer | None:
    """A message while the plan is written goes into the plan turn, never into a coding turn: the project is not
    approved yet. With no plan turn working (it waits for an answer, or it ended), it queues another plan turn."""
    if row.project_id is None:
        return None
    request = turns_domain.request_from_input(item, row.project_id, kind=TurnKind.PLAN, merge=True)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        current = await store.get_draft(s, row.id)
        if current is None or _status(current) not in domain.PLANNING:
            return None
        submitted = await turns.submit(s, request=request, at=now)
        if not submitted.merged:
            await store.set_draft(s, row.id, plan_turn_id=submitted.turn_id)
    state.turns.poke()
    log.info("newproject_plan_steered", draft_id=row.id, turn_id=submitted.turn_id, merged=submitted.merged)
    return DraftAnswer(DraftOutcome.STEERED, row.id, row.project_id, submitted.turn_id)


async def _answer_plan(
    deps: DevDeps, state: NewProjectState, row: ProjectDraft, item: TurnInput, *, names: Sequence[str]
) -> DraftAnswer | None:
    answer = domain.plan_answer(_raw_text(item), names=names)
    if answer.verdict is PlanVerdict.CANCEL:
        return await _cancel(deps, row, from_status=DraftStatus.AWAITING_APPROVAL)
    project_id = row.project_id
    if project_id is None:
        return None
    if answer.verdict is PlanVerdict.REVISE and not answer.feedback:
        snap = await settings.snapshot(deps)
        async with deps.db.uow() as s:
            ask = S.NEWPROJECT_REVISE_WHAT(project=project_id)
            await _tell(s, deps, snap, ask, ref=f"{row.id}-revise-what-{item.burst_id}", project_id=project_id)
        return DraftAnswer(DraftOutcome.ASKED, row.id, project_id)
    snap = await settings.snapshot(deps)
    try:
        project = await projects.get_project(deps, project_id)
    except ProjectNotFound:
        log.warning("newproject_project_missing", draft_id=row.id, project=project_id)
        return None
    repo_url, repo_problem = await _ensure_repo(deps, snap, row.id, project)
    if answer.verdict is PlanVerdict.APPROVE:
        target, kind, outcome = DraftStatus.BUILDING, TurnKind.BUILD, DraftOutcome.BUILDING
        text = prompts.render("dev/newproject_build", name=project.id)
        reply = S.NEWPROJECT_BUILDING(project=project.id)
    else:
        target, kind, outcome = DraftStatus.REVISING, TurnKind.PLAN, DraftOutcome.REVISING
        text = prompts.render("dev/newproject_revise", name=project.id, feedback=redact.scrubbed(answer.feedback))
        reply = S.NEWPROJECT_REVISING(project=project.id)
    if repo_url is None and repo_problem is not None:
        reply = join([reply, repo_problem])
    request = TurnRequest(project_id=project.id, text=text, kind=kind, source=DeliverySource.SYSTEM)
    now = deps.clock.now()
    turn_id: int | None = None
    async with deps.db.uow() as s:
        if await store.move_draft(
            s, row.id, from_statuses=[DraftStatus.AWAITING_APPROVAL.value], to_status=target.value
        ):
            turn_id = (await turns.submit(s, request=request, at=now)).turn_id
            if kind is TurnKind.BUILD:
                await store.set_draft(s, row.id, build_turn_id=turn_id)
            else:
                await store.set_draft(s, row.id, plan_turn_id=turn_id)
            await _tell(s, deps, snap, reply, ref=f"{row.id}-{target.value}-{turn_id}", project_id=project.id)
    if turn_id is None:
        return None
    state.turns.poke()
    log.info("newproject_answered", draft_id=row.id, project=project.id, verdict=answer.verdict.value)
    return DraftAnswer(outcome, row.id, project.id, turn_id)


def _copy_docs(root: Path, target: Path, *, stem: str) -> tuple[tuple[str, ...], tuple[tuple[str, Path], ...]]:
    """The plan's `docs/*.md`: (every name chosen, the copies to send). Each copy is scrubbed and 0600 in the media
    cache, so a later revision cannot change what is being sent. A symlink, a hard link or a file over
    `DOC_MAX_BYTES` is named but never read: a document must not be a way out for a key or a file outside the
    project."""
    docs = root / domain.DOCS_DIR
    try:
        names = [entry.name for entry in docs.iterdir()]
    except OSError:
        return (), ()
    chosen = domain.plan_docs(names)
    base = root.resolve()
    copies: list[tuple[str, Path]] = []
    for index, name in enumerate(chosen):
        source = docs / name
        try:
            info = source.lstat()
            regular = source.is_file() and not source.is_symlink() and info.st_nlink == 1
            if not regular or info.st_size > domain.DOC_MAX_BYTES or not source.resolve().is_relative_to(base):
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy = target / f"{stem}-{index}-{name}"
            copy.touch(mode=0o600, exist_ok=True)
            copy.write_text(redact.scrubbed(text), encoding="utf-8")
        except OSError:
            continue
        copies.append((name, copy))
    return chosen, tuple(copies)


async def _plan_ready(deps: DevDeps, row: ProjectDraft, *, turn_id: int, natija: str | None) -> bool:
    """The plan turn ended: the documents, the summary and "Rejani tasdiqlaysizmi?" go out (SPEC §3.2.8 step 5)."""
    if row.project_id is None:
        return False
    try:
        project = await projects.get_project(deps, row.project_id)
    except ProjectNotFound:
        log.warning("newproject_project_missing", draft_id=row.id, project=row.project_id)
        return False
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    target = deps.paths.media / now.strftime("%Y%m%d")
    chosen, copies = await asyncio.to_thread(
        _copy_docs, Path(project.path), target, stem=f"newproject-{row.id}-{turn_id}"
    )
    text = domain.plan_ready_text(project.id, natija=natija, docs=chosen, repo_url=row.repo_url)
    about = About(project_id=project.id)
    async with deps.db.uow() as s:
        current = await store.get_draft(s, row.id)
        moved = (
            current is not None
            and current.plan_turn_id == turn_id
            and await store.move_draft(
                s,
                row.id,
                from_statuses=[status.value for status in domain.PLANNING],
                to_status=DraftStatus.AWAITING_APPROVAL.value,
            )
        )
        if moved:
            for index, (name, path) in enumerate(copies):
                await outbox.enqueue_file(
                    s,
                    bot=deps.bot,
                    chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
                    op=Op.SEND_DOCUMENT,
                    path=path,
                    kind=Kind.FILE,
                    ref=f"newproject-{row.id}-plan-{turn_id}-doc-{index}",
                    at=now,
                    max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                    caption=S.NEWPROJECT_DOC_CAPTION(project=project.id, name=name),
                    filename=name,
                    mime=domain.DOC_MIME,
                    priority=domain.PLAN_PRIORITY,
                    about=about,
                )
            await _tell(
                s,
                deps,
                snap,
                text,
                ref=f"{row.id}-plan-{turn_id}",
                project_id=project.id,
                kind=Kind.DECISION,
                priority=domain.PLAN_PRIORITY,
            )
    if moved:
        log.info("newproject_plan_ready", draft_id=row.id, project=project.id, turn_id=turn_id, docs=len(copies))
    return moved


async def _built(deps: DevDeps, row: ProjectDraft, *, project: str) -> bool:
    """The build turn ended `done`: the draft is finished (its result, commits and push were already reported)."""
    snap = await settings.snapshot(deps)
    lines = [S.NEWPROJECT_DONE(project=project)]
    if row.repo_url:
        lines.append(S.NEWPROJECT_REPO(url=row.repo_url))
    async with deps.db.uow() as s:
        moved = await store.move_draft(
            s,
            row.id,
            from_statuses=[DraftStatus.BUILDING.value],
            to_status=DraftStatus.DONE.value,
            closed_at=deps.clock.now(),
        )
        if moved:
            await _tell(s, deps, snap, join(lines), ref=f"{row.id}-done", project_id=project)
    if moved:
        log.info("newproject_done", draft_id=row.id, project=project)
    return moved


async def _advance(deps: DevDeps, *, project_id: str | None = None) -> int:
    """Drafts whose plan or build turn ended `done` take their next step; one that failed or was stopped waits ("davom
    ettir" goes on with the same turn, a message queues a new plan turn). Returns how many moved."""
    async with deps.db.read() as s:
        rows = [row for row in await store.list_drafts_with_status(s, _WATCHED) if project_id in {None, row.project_id}]
    watched = {
        row.id: row.build_turn_id if _status(row) is DraftStatus.BUILDING else row.plan_turn_id
        for row in rows
        if row.project_id is not None
    }
    statuses = await turns.turn_statuses(deps, [turn_id for turn_id in watched.values() if turn_id is not None])
    moved = 0
    for row in rows:
        turn_id = watched.get(row.id)
        if turn_id is None or statuses.get(turn_id) is not TurnStatus.DONE or row.project_id is None:
            continue
        if _status(row) is DraftStatus.BUILDING:
            moved += await _built(deps, row, project=row.project_id)
            continue
        recent = await turns.list_recent_turns(deps, row.project_id, limit=_RECENT_TURNS)
        natija = next((summary.natija for summary in recent if summary.turn_id == turn_id), None)
        moved += await _plan_ready(deps, row, turn_id=turn_id, natija=natija)
    return moved


async def on_turn_ended(deps: DevDeps, project_id: str) -> None:
    """Turns' `turn_ended` hook (`TurnEndHook`): a plan turn's end sends the plan, a build turn's end closes the
    draft. It shares the one hook slot with `dev/digest`; wire both (they are independent)."""
    await _advance(deps, project_id=project_id)


# --- the timer, recovery, shutdown ----------------------------------------------------------------------------------
async def tend_drafts(deps: DevDeps, state: NewProjectState) -> int:
    """One pass: close descriptions silent for `newproject.draft_silence_s`, start the folders a restart left half
    made, and move drafts whose turn ended while no hook was there to see it. Returns how many drafts changed."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    silence = snap[defaults.NEWPROJECT_DRAFT_SILENCE_S]
    async with deps.db.read() as s:
        rows = await store.list_drafts_with_status(s, [DraftStatus.COLLECTING.value, DraftStatus.CREATING.value])
    changed = 0
    for row in rows:
        if _status(row) is DraftStatus.CREATING:
            _start_creating(deps, state, row.id)
        elif domain.silent(row.last_part_at, now, silence_s=silence):
            await _close(deps, state, row.id, silence=True)
            changed += 1
    return changed + await _advance(deps)


async def run_drafts(deps: DevDeps, state: NewProjectState) -> None:
    """The long-lived loop: `aio.forever("newproject", lambda: newproject.run_drafts(deps, state))`."""
    while True:
        await tend_drafts(deps, state)
        await deps.clock.sleep(domain.TICK_S)


async def shutdown_drafts(deps: DevDeps, state: NewProjectState) -> int:
    """Cancel the folder work in flight; its draft stays `creating` and the next start finishes it. Returns how many
    tasks were cancelled."""
    tasks = list(state.tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("newproject_shutdown", bot=deps.bot.value, cancelled=len(tasks))
    return len(tasks)


# --- reads ----------------------------------------------------------------------------------------------------------
async def open_approvals(deps: DevDeps) -> tuple[PendingItem, ...]:
    """Plans waiting for "ha", as `PendingItem(kind="question")`: hand them to the gate's
    `DecisionReply.open_questions` (and count them in turns' `RevertReply.others_pending`), so a bare "ha" approves
    no gate decision while a plan waits too (R-18). Never kind "decision": the ids are draft ids, not decisions."""
    async with deps.db.read() as s:
        rows = await store.list_drafts_with_status(s, [DraftStatus.AWAITING_APPROVAL.value])
    return tuple(PendingItem("question", row.id, row.project_id) for row in rows)


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    """Finished drafts go 400 days after they closed; an open one is never touched. Returns how many were deleted."""
    cutoff = domain.draft_cutoff(deps.clock.now())
    finished = sorted(status.value for status in domain.TERMINAL)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_drafts_before(s, cutoff, statuses=finished, limit=limit)
    )
    log.info("newproject_retention_applied", drafts_deleted=deleted)
    return deleted
