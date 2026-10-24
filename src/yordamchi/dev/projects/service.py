"""The project registry (SPEC §3.1 `dev/projects`, §3.2.6, §5.7; UC-10 prerequisites, UC-13 #1, UC-18; NFR-CAP-01).

- `sync_projects` is the scan the scheduler runs every `domain.SCAN_INTERVAL_S` (T34): it lists `projects.root`,
  registers new folders, refreshes known ones and marks vanished ones missing. The disk is read in a worker thread
  and git is asked outside the unit of work (A3); the results are written in one short transaction. A root that
  cannot be listed changes nothing, so a detached disk never marks every project missing.
- `list_projects`, `get_project`, `find_project` and `show_projects` read the rows and apply the owner's settings
  (aliases, archived, descriptions, executor) from this call's snapshot: a chat change applies at once.
- `deploy_branches` is the gate's fact (§3.2.3 step 2): computed fresh from the working tree's workflows and the
  pushed commit's, never from the stored copy, and any read failure counts as "every push deploys".
- `alias_project`, `archive_project` and `describe_project` are the UC-18 chat edits: each writes a new settings
  version (an `OwnerOrigin` is required) and returns the owner's reply.

The `projects` table is kept forever (SPEC §4.2), so there is no retention function.
"""

import asyncio
import stat
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import yaml

from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError, GitFailed, ProjectNotFound
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import domain, store
from yordamchi.dev.projects.domain import (
    DeployBranches,
    DescriptionSource,
    DocText,
    FolderFacts,
    GitFacts,
    GitKind,
    Project,
    ProjectPrefs,
    ProjectRecord,
    SyncReport,
    WorkflowFile,
    WorkflowStat,
)
from yordamchi.dev.projects.models import Project as ProjectRow
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

REMOTE = "origin"


# --- the disk (worker thread only: these block) ---------------------------------------------------------------------
def _read_prefix(path: Path, limit: int) -> bytes | None:
    try:
        with path.open("rb") as file:
            return file.read(limit)
    except OSError:
        return None


def _listing(folder: Path) -> dict[str, Path]:
    """Entries of `folder` by case-folded name; empty when it cannot be listed."""
    try:
        return {entry.name.casefold(): entry for entry in folder.iterdir()}
    except OSError:
        return {}


def _docs(folder: Path) -> tuple[DocText, ...]:
    """The first existing file of each description source, in `domain.DOC_FILES` order."""
    top, docs_dir = _listing(folder), _listing(folder / "docs")
    found: list[DocText] = []
    for source, names in domain.DOC_FILES:
        for name in names:
            parent, _, leaf = name.rpartition("/")
            entry = (docs_dir if parent == "docs" else top).get(leaf.casefold())
            data = _read_prefix(entry, domain.DOC_READ_BYTES) if entry is not None and entry.is_file() else None
            if data:
                found.append(DocText(source=source, text=data.decode("utf-8", errors="replace")))
                break
    return tuple(found)


def _workflow_stats(folder: Path) -> tuple[WorkflowStat, ...] | None:
    try:
        entries = list((folder / domain.WORKFLOWS_DIR).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except OSError:
        return None
    stats: list[WorkflowStat] = []
    for entry in entries:
        if not domain.is_workflow_name(entry.name):
            continue
        try:
            info = entry.stat()
        except OSError:
            stats.append(WorkflowStat(name=entry.name, size=-1, mtime_ns=-1))
            continue
        if stat.S_ISREG(info.st_mode):
            stats.append(WorkflowStat(name=entry.name, size=info.st_size, mtime_ns=info.st_mtime_ns))
    return tuple(sorted(stats, key=lambda item: item.name))


def _may_exist(path: Path) -> bool:
    """False only when the path is surely absent; a path that cannot be checked may exist (fail closed)."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _platform_files(folder: Path) -> tuple[str, ...]:
    """The `domain.PLATFORM_FILES` in the working tree."""
    return tuple(name for name in domain.PLATFORM_FILES if _may_exist(folder / name))


def _folder_facts(folder: Path) -> FolderFacts:
    dot_git = folder / ".git"
    git = GitKind.REPO if dot_git.is_dir() else GitKind.WORKTREE if dot_git.is_file() else GitKind.NONE
    return FolderFacts(
        name=folder.name,
        path=PurePosixPath(folder),
        git=git,
        docs=_docs(folder),
        workflow_stats=_workflow_stats(folder),
        platform_files=_platform_files(folder),
    )


@dataclass(frozen=True, slots=True)
class _RootScan:
    folders: tuple[FolderFacts, ...]
    skipped: int


def _scan_root(root: Path, excluded: Collection[str]) -> _RootScan | None:
    """Every project folder directly under `root` but the excluded ones; None when `root` cannot be listed."""
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return None
    folders: list[FolderFacts] = []
    skipped = 0
    for entry in entries:
        reason = domain.skip_reason(entry.name, is_dir=entry.is_dir(), is_symlink=entry.is_symlink(), excluded=excluded)
        if reason is None:
            folders.append(_folder_facts(entry))
        else:
            skipped += 1
    return _RootScan(folders=tuple(folders), skipped=skipped)


def _parse_workflow(data: bytes | None) -> object:
    """`yaml.safe_load` only (never a loader that builds objects). Unreadable or too large → `UNPARSEABLE`."""
    if data is None or len(data) > domain.WORKFLOW_MAX_BYTES:
        return domain.UNPARSEABLE
    try:
        document: object = yaml.safe_load(data)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        return domain.UNPARSEABLE
    return document


def _worktree_workflows(folder: Path) -> tuple[WorkflowFile, ...]:
    directory = folder / domain.WORKFLOWS_DIR
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except OSError:
        return (WorkflowFile(name=domain.WORKFLOWS_DIR, document=domain.UNPARSEABLE),)
    return tuple(
        WorkflowFile(name=entry.name, document=_parse_workflow(_read_prefix(entry, domain.WORKFLOW_MAX_BYTES + 1)))
        for entry in entries
        if domain.is_workflow_name(entry.name) and entry.is_file()
    )


def _parse_files(files: Mapping[str, bytes | None]) -> tuple[WorkflowFile, ...]:
    return tuple(
        WorkflowFile(name=name, document=_parse_workflow(data))
        for name, data in sorted(files.items())
        if domain.is_workflow_name(name)
    )


# --- git (outside any unit of work) ---------------------------------------------------------------------------------
async def _git_facts(deps: DevDeps, repo: Path, *, timeout: float) -> GitFacts:
    head = await deps.git.find_commit(repo, "HEAD", timeout=timeout)
    return GitFacts(
        head=head.sha if head else None,
        head_at=head.committed_at if head else None,
        remote_url=await deps.git.remote_url(repo, remote=REMOTE, timeout=timeout),
        default_branch=await deps.git.default_branch(repo, timeout=timeout),
    )


async def _committed_deploy(deps: DevDeps, repo: Path, ref: str, *, timeout: float) -> DeployBranches:
    files = await deps.git.tree_files(
        repo, ref, domain.WORKFLOWS_DIR, max_bytes=domain.WORKFLOW_MAX_BYTES, timeout=timeout
    )
    return domain.deploy_branches(await asyncio.to_thread(_parse_files, files))


def _worktree_inputs(repo: Path) -> tuple[tuple[WorkflowFile, ...], tuple[str, ...]]:
    return _worktree_workflows(repo), _platform_files(repo)


async def _worktree_deploy(repo: Path, *, default_branch: str | None) -> DeployBranches:
    """What the working tree deploys: its workflows, plus the default branch when a platform config is there."""
    workflows, platform = await asyncio.to_thread(_worktree_inputs, repo)
    return domain.deploy_branches(workflows).union(domain.platform_deploy(platform, default_branch=default_branch))


# --- the scan -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Seen:
    """One folder after the git step: the facts to write. `deploy_key=None` makes the next scan check again."""

    folder: FolderFacts
    git: GitFacts | None
    deploy: DeployBranches
    deploy_key: str | None
    git_failed: bool = False


async def _inspect(deps: DevDeps, folder: FolderFacts, previous: ProjectRecord | None) -> _Seen:
    """git facts and deploy branches of one folder. Deploy branches are recomputed only when the HEAD commit, a
    workflow file, a platform config or the default branch changed (`domain.deploy_key`). A git failure keeps the last
    facts and adds what the working tree shows, so the stored value only ever errs towards asking."""
    repo = Path(folder.path)
    project = domain.project_id(folder.name)
    timeout = domain.GIT_TIMEOUT_S
    known_branch = previous.default_branch if previous is not None else None
    try:
        git = await _git_facts(deps, repo, timeout=timeout) if folder.git is not GitKind.NONE else None
        branch = git.default_branch if git is not None else known_branch
        key = domain.deploy_key(
            git.head if git else None, folder.workflow_stats, platform=folder.platform_files, default_branch=branch
        )
        if previous is not None and previous.deploy_checked_sha == key:
            return _Seen(folder=folder, git=git, deploy=previous.deploy, deploy_key=key)
        deploy = await _worktree_deploy(repo, default_branch=branch)
        if git is not None and git.head is not None:
            deploy = deploy.union(await _committed_deploy(deps, repo, git.head, timeout=timeout))
        return _Seen(folder=folder, git=git, deploy=deploy, deploy_key=key)
    except ExternalError as error:
        log.warning("project_git_failed", project=project, error_code=error.code.value)
        known = previous.deploy if previous is not None else domain.NO_DEPLOY
        deploy = known.union(await _worktree_deploy(repo, default_branch=known_branch))
        return _Seen(folder=folder, git=None, deploy=deploy, deploy_key=None, git_failed=True)


async def _inspect_all(
    deps: DevDeps, folders: tuple[FolderFacts, ...], known: Mapping[str, ProjectRecord]
) -> list[_Seen]:
    limit = asyncio.Semaphore(domain.GIT_CONCURRENCY)

    async def bounded(folder: FolderFacts) -> _Seen:
        async with limit:
            return await _inspect(deps, folder, known.get(domain.project_id(folder.name)))

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(bounded(folder)) for folder in folders]
    return [task.result() for task in tasks]


def _record(row: ProjectRow) -> ProjectRecord:
    try:
        source = DescriptionSource(row.description_source)
    except ValueError:
        source = DescriptionSource.NONE
    return ProjectRecord(
        id=row.id,
        path=row.path,
        description=row.description,
        description_source=source,
        is_worktree=row.is_worktree,
        is_archive_name=row.is_archive_name,
        executor=row.executor,
        git_remote_url=row.git_remote_url,
        default_branch=row.default_branch,
        deploy=DeployBranches.from_json(row.deploy_branches_json),
        deploy_checked_sha=row.deploy_checked_sha,
        discovered_at=row.discovered_at,
        last_seen_at=row.last_seen_at,
        missing_since=row.missing_since,
        last_activity_at=row.last_activity_at,
    )


def _prefs(snap: Snapshot) -> ProjectPrefs:
    return ProjectPrefs(
        aliases=snap[defaults.PROJECTS_ALIASES],
        descriptions=snap[defaults.PROJECTS_DESCRIPTIONS],
        archived=snap[defaults.PROJECTS_ARCHIVED],
        unarchived=snap[defaults.PROJECTS_UNARCHIVED],
        executor_default=snap[defaults.EXECUTOR_DEFAULT],
        executor_per_project=snap[defaults.EXECUTOR_PER_PROJECT],
    )


def _root(deps: DevDeps, snap: Snapshot) -> PurePosixPath:
    return domain.projects_root(snap[defaults.PROJECTS_ROOT], home=PurePosixPath(deps.paths.home))


async def sync_projects(deps: DevDeps) -> SyncReport:
    """One scan of `projects.root` (UC-18: a new folder is registered by the next scan, ≤ 5 min)."""
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    root = _root(deps, snap)
    scan = await asyncio.to_thread(_scan_root, Path(root), snap[defaults.PROJECTS_EXCLUDED])
    if scan is None:
        log.warning("projects_root_unreadable")
        return SyncReport(total=0, root_readable=False)
    async with deps.db.read() as s:
        known = {row.id: _record(row) for row in await store.list_projects(s)}
    seen = await _inspect_all(deps, scan.folders, known)

    prefs = _prefs(snap)
    now = deps.clock.now()
    records = [
        domain.record_for(
            item.folder,
            git=item.git,
            deploy=item.deploy,
            deploy_checked_sha=item.deploy_key,
            previous=known.get(domain.project_id(item.folder.name)),
            prefs=prefs,
            now=now,
        )
        for item in seen
    ]
    # Scrubbed before the write: a README may quote a key (SPEC §4.5).
    records = [replace(record, description=redact.scrubbed(record.description)) for record in records]
    gone = domain.vanished(known, {record.id for record in records})
    async with deps.db.uow() as s:
        for record in records:
            await store.set_project(s, record)
        await store.mark_projects_missing(s, gone, at=now)

    report = SyncReport(
        total=len(records),
        added=tuple(record.id for record in records if record.id not in known),
        returned=tuple(record.id for record in records if record.id in known and known[record.id].missing_since),
        vanished=gone,
        skipped=scan.skipped,
        git_failed=tuple(domain.project_id(item.folder.name) for item in seen if item.git_failed),
    )
    for project in report.added:
        log.info("project_registered", project=project)
    for project in report.vanished:
        log.info("project_missing", project=project)
    log.info(
        "projects_synced",
        total=report.total,
        added=len(report.added),
        returned=len(report.returned),
        vanished=len(report.vanished),
        skipped=report.skipped,
        git_failed=len(report.git_failed),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return report


# --- reads ----------------------------------------------------------------------------------------------------------
async def _views(deps: DevDeps, snap: Snapshot) -> tuple[Project, ...]:
    async with deps.db.read() as s:
        rows = await store.list_projects(s)
    prefs, excluded = _prefs(snap), snap[defaults.PROJECTS_EXCLUDED]
    return tuple(
        domain.project_view(_record(row), prefs)
        for row in rows
        if not domain.is_hidden(row.id) and not domain.is_excluded(row.id, excluded)
    )


async def list_projects(deps: DevDeps) -> tuple[Project, ...]:
    """Every registered project with the owner's current settings applied, missing and archived ones included
    (`Project.routable` says which content routing may pick). Yordamchi's hidden smoke project is not among them."""
    return await _views(deps, await settings.snapshot(deps))


async def ensure_smoke_project(deps: DevDeps) -> Project:
    """Register (or refresh) the hidden project of the CLI smoke test, at `DATA/sessions/smoke` (SPEC §5.3, §5.6).
    Turns reach it by its exact id, `domain.SMOKE_PROJECT_ID`; listing and routing never show it."""
    snap = await settings.snapshot(deps)
    path = str(deps.paths.session_dir("smoke"))
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.get_project(s, domain.SMOKE_PROJECT_ID)
        record = domain.smoke_record(path, previous=None if row is None else _record(row), now=now)
        await store.set_project(s, record)
    return domain.project_view(record, _prefs(snap))


async def get_project(deps: DevDeps, project_id: str) -> Project:
    """The project with this exact id (the hidden smoke project too); `ProjectNotFound` otherwise, also for a project
    the owner excluded after it was registered."""
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        row = await store.get_project(s, project_id)
    if row is None or domain.is_excluded(row.id, snap[defaults.PROJECTS_EXCLUDED]):
        raise ProjectNotFound(name=project_id)
    return domain.project_view(_record(row), _prefs(snap))


async def find_project(deps: DevDeps, name: str) -> Project:
    """The one project whose folder name or alias is exactly `name` (case-insensitive); `ProjectNotFound` or
    `ProjectAmbiguous` otherwise."""
    return domain.resolve_project(name, await list_projects(deps))


async def show_projects(deps: DevDeps) -> BotText:
    """The `loyihalar` reply (UC-18, P1 basic)."""
    snap = await settings.snapshot(deps)
    return domain.listing(await _views(deps, snap), root=snap[defaults.PROJECTS_ROOT])


# --- the gate's fact ------------------------------------------------------------------------------------------------
async def deploy_branches(
    deps: DevDeps, project_id: str, *, ref: str = "HEAD", timeout: float = domain.GIT_TIMEOUT_S
) -> DeployBranches:
    """Which pushes deploy, read now (SPEC §5.7 `deploy_push`): the working tree's workflows plus those committed at
    `ref` (pass the commit being pushed), and the default branch when the working tree has a platform config (T17b).
    A workflow that cannot be read, or a git failure reading the commit, makes every branch and tag count. A folder
    that is not a git repository has no commit side."""
    project = await get_project(deps, project_id)
    repo = Path(project.path)
    result = await _worktree_deploy(repo, default_branch=project.default_branch)
    try:
        committed = await _committed_deploy(deps, repo, ref, timeout=timeout)
    except GitFailed as error:
        if error.reason != "not_a_repo":
            log.warning("deploy_check_failed", project=project.id, error_code=error.code.value, reason=error.reason)
            return result.union(domain.FAIL_CLOSED)
        committed = domain.NO_DEPLOY
    except ExternalError as error:
        log.warning("deploy_check_failed", project=project.id, error_code=error.code.value)
        return result.union(domain.FAIL_CLOSED)
    return result.union(committed)


# --- the owner's edits (UC-18) --------------------------------------------------------------------------------------
async def alias_project(deps: DevDeps, origin: OwnerOrigin, name: str, alias: str) -> BotText:
    """ "BonviHR'ni 'hr' deb ham tani": `alias` now names the project, for routing too, from the next message."""
    origin = require_owner_origin(origin, deps.bot)
    snap = await settings.snapshot(deps)
    projects = await _views(deps, snap)
    project = domain.resolve_project(name, projects)
    aliases = domain.with_alias(snap[defaults.PROJECTS_ALIASES], projects, project=project, alias=alias)
    if aliases is not None:
        await settings.record_version(
            deps, origin, defaults.PROJECTS_ALIASES, settings.as_json(aliases), reason="change"
        )
        log.info("project_alias_added", project=project.id)
    return S.PROJECT_ALIAS_ADDED(project=project.id, alias=" ".join(alias.split()))


async def archive_project(deps: DevDeps, origin: OwnerOrigin, name: str, *, archived: bool) -> BotText:
    """ "X'ni arxivla" / "X'ni arxivdan chiqar": an archived project answers only to its exact name (UC-18 AC)."""
    origin = require_owner_origin(origin, deps.bot)
    snap = await settings.snapshot(deps)
    projects = await _views(deps, snap)
    project = domain.resolve_project(name, projects)
    change = domain.with_archived(_prefs(snap), project, archived=archived)
    # Two settings, two versions. `projects.archived` goes first and wins over `projects.unarchived`, so a failure
    # between the writes leaves the project archived, never unexpectedly routable.
    if change.archived is not None:
        value = settings.as_json(change.archived)
        await settings.record_version(deps, origin, defaults.PROJECTS_ARCHIVED, value, reason="change")
    if change.unarchived is not None:
        value = settings.as_json(change.unarchived)
        await settings.record_version(deps, origin, defaults.PROJECTS_UNARCHIVED, value, reason="change")
    log.info("project_archive_set", project=project.id, archived=archived)
    if archived:
        return S.PROJECT_ARCHIVED(project=project.id)
    return S.PROJECT_UNARCHIVED(project=project.id)


async def describe_project(deps: DevDeps, origin: OwnerOrigin, name: str, text: str) -> BotText:
    """ "X tavsifi: …": the owner's one-line description replaces the one built from README/CLAUDE.md/SPEC."""
    origin = require_owner_origin(origin, deps.bot)
    snap = await settings.snapshot(deps)
    project = domain.resolve_project(name, await _views(deps, snap))
    descriptions = domain.with_description(snap[defaults.PROJECTS_DESCRIPTIONS], project, text)
    value = settings.as_json(descriptions)
    await settings.record_version(deps, origin, defaults.PROJECTS_DESCRIPTIONS, value, reason="change")
    log.info("project_described", project=project.id)
    return S.PROJECT_DESCRIBED(project=project.id)
