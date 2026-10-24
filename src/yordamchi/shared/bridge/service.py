"""The read-only bridge from the personal bot into `dev.db` (SPEC §2.3, §3.1; UC-36; T70).

The handle is `deps.peer_db`, opened by `bots/personal/app.py` as `open_database(dev.db, readonly=True)`:
`sqlite+aiosqlite:///file:…/dev.db?mode=ro&uri=true` with `PRAGMA query_only=ON`. Every read here checks that it is
that handle (`Database.readonly`, the URL's `mode=ro`) and reads in `db.read()`, never `uow()`; `check_read_only`
also asks SQLite (`PRAGMA query_only`), for the startup self-check. There is no write path and no executor path:
`shared` cannot import `dev` (C1), the personal bot cannot import it even indirectly (C7), and the store only selects.

- `status_reply(deps, text, …)`: the UC-36 fast path. A plain status question ("BonviHR qanday?") is answered from the
  digest, with the freshness line, in well under 4 s; anything else returns None and the assistant answers.
- `toolset(deps, …)`: the `yordamchi_bridge` tools of the personal assistant (`project_status`, `server_status`),
  read-only. Their results are data for the model; `log` records which projects were read, so the answer can end
  with the freshness line.
- `list_projects`, `project_status`, `server_checks`: the reads themselves, each bounded by `timeouts.bridge_s`.

What the bridge does not read yet (SPEC §2.3): `integrations` and `vault_secrets` rows assigned to `personal` (T76's
gateway owns that read) and `usage_records` (the personal `xarajat?`, P2 cost report). `setting_versions` of BOTH
keys is read by `shared/settings` itself (ASSUMPTIONS 2026-10-24).
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core.errors import BridgeNotReadOnly, BridgeUnavailable
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.persistence.uow import Database
from yordamchi.ports.assistant import AssistantTool, AssistantToolset, ToolOutput
from yordamchi.shared.bridge import domain, store
from yordamchi.shared.bridge.domain import ProjectName, ProjectRef, ProjectStatus, ServerView
from yordamchi.shared.deps import SharedDeps

log = get_log(__name__)

SERVER: Final = "yordamchi_bridge"
PROJECT_TOOL: Final = "project_status"
SERVER_TOOL: Final = "server_status"
PROJECT_TOOL_PROMPT: Final = "personal/bridge_project_status"
SERVER_TOOL_PROMPT: Final = "personal/bridge_server_status"
SERVER_LOOKBACK: Final = timedelta(hours=24)  # older checks are not "the latest" any more
PROJECT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"project": {"type": "string", "minLength": 1, "maxLength": 80}},
    "required": ["project"],
    "additionalProperties": False,
}
SERVER_SCHEMA: Final[JsonObject] = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass
class ToolLog:
    """What the bridge tools read during one assistant ask: (project, `checked_at`) per digest read."""

    read: list[tuple[str, datetime | None]] = field(default_factory=list)

    @property
    def oldest(self) -> datetime | None:
        """The oldest data the answer used (None when a digest had none, or nothing was read)."""
        moments = [moment for _, moment in self.read]
        if not moments or any(moment is None for moment in moments):
            return None
        return min(moment for moment in moments if moment is not None)


# --- the handle ------------------------------------------------------------------------------------------------------
def _handle(deps: SharedDeps) -> Database:
    db = deps.peer_db
    if db is None:
        raise BridgeUnavailable()
    url = db.engine.url
    if not db.readonly or url.query.get("mode") != "ro" or url.query.get("uri") != "true":
        log.error("bridge_refused", reason="not_read_only")
        raise BridgeNotReadOnly()
    return db


async def check_read_only(deps: SharedDeps, *, timeout: float) -> None:
    """The startup self-check (UC-36): the handle is `mode=ro` and SQLite reports `query_only=ON`. Raises otherwise."""
    db = _handle(deps)
    try:
        async with asyncio.timeout(timeout), db.read() as s:
            query_only = await store.get_query_only(s)
    except TimeoutError:
        raise BridgeUnavailable() from None
    if not query_only:
        log.error("bridge_refused", reason="query_only_off")
        raise BridgeNotReadOnly()


# --- reads -----------------------------------------------------------------------------------------------------------
async def list_projects(deps: SharedDeps, *, timeout: float) -> tuple[ProjectRef, ...]:
    db = _handle(deps)
    try:
        async with asyncio.timeout(timeout), db.read() as s:
            rows = await store.list_projects(s)
    except TimeoutError:
        raise BridgeUnavailable() from None
    return tuple(
        ProjectRef(project_id=project_id, description=description, missing=missing)
        for project_id, description, _archive_name, missing in rows
    )


def _int(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(row: dict[str, object], name: str) -> str | None:
    value = row.get(name)
    return value if isinstance(value, str) else None


def _moment(row: dict[str, object], name: str) -> datetime | None:
    value = row.get(name)
    return value if isinstance(value, datetime) else None


def _json(row: dict[str, object], name: str) -> JsonValue:
    value = row.get(name)
    return value if isinstance(value, list | dict) else None


def _status(project: ProjectRef, row: dict[str, object] | None) -> ProjectStatus:
    if row is None:
        return ProjectStatus(project_id=project.project_id, description=project.description)
    return ProjectStatus(
        project_id=project.project_id,
        description=project.description,
        checked_at=_moment(row, "checked_at"),
        branch=_text(row, "git_branch"),
        head=_text(row, "git_head"),
        upstream=_text(row, "git_upstream"),
        ahead=_int(row, "ahead"),
        behind=_int(row, "behind"),
        dirty=_int(row, "dirty_count"),
        untracked=_int(row, "untracked_count"),
        git_error_code=_text(row, "git_error_code"),
        commits=domain.commits_from_json(_json(row, "recent_commits_json")),
        results=domain.results_from_json(_json(row, "results_json")),
        open_questions=_int(row, "open_questions"),
        pending_decisions=_int(row, "pending_decisions"),
        queued=_int(row, "queued"),
        parked=_int(row, "parked"),
        handoff_note=_text(row, "handoff_note"),
    )


async def project_status(deps: SharedDeps, project: ProjectRef, *, timeout: float) -> ProjectStatus:
    db = _handle(deps)
    try:
        async with asyncio.timeout(timeout), db.read() as s:
            row = await store.get_digest(s, project.project_id)
    except TimeoutError:
        raise BridgeUnavailable() from None
    return _status(project, row)


async def server_checks(deps: SharedDeps, *, timeout: float) -> tuple[ServerView, ...]:
    db = _handle(deps)
    since = deps.clock.now() - SERVER_LOOKBACK
    try:
        async with asyncio.timeout(timeout), db.read() as s:
            rows = await store.list_latest_checks(s, since=since)
    except TimeoutError:
        raise BridgeUnavailable() from None
    return tuple(
        ServerView(host=host, at=at, ok=ok, summary=domain.summary_of(summary), error_code=error_code)
        for host, at, ok, summary, error_code in rows
    )


async def project_names(deps: SharedDeps, *, timeout: float) -> tuple[ProjectName, ...]:
    """The dev bot's project names for the speech vocabulary (T77: "BonviHR qanday?" said aloud is spelled right).
    Best effort: no bridge, or a bridge that does not answer, gives none (a voice note never fails for it)."""
    if deps.peer_db is None:
        return ()
    try:
        projects = await list_projects(deps, timeout=timeout)
    except (BridgeUnavailable, BridgeNotReadOnly) as error:
        log.warning("bridge_names_unread", error_code=error.code.value)
        return ()
    return tuple(ProjectName(id=project.project_id) for project in projects if not project.missing)


# --- UC-36: the fast path -------------------------------------------------------------------------------------------
async def status_reply(deps: SharedDeps, text: str, *, timezone: str, timeout: float) -> BotText | None:
    """The answer to a plain status question ("BonviHR qanday?") from the digest, ending with the freshness line;
    None when `text` is anything else or the personal bot has no bridge (the assistant answers then)."""
    if deps.peer_db is None or not text.strip():
        return None
    projects = await list_projects(deps, timeout=timeout)
    named = domain.status_question(text, projects)
    project = next((item for item in projects if item.project_id == named), None)
    if project is None:
        return None
    status = await project_status(deps, project, timeout=timeout)
    log.info("bridge_status_answered", project=project.project_id, has_digest=status.has_digest)
    return redact.scrubbed(domain.status_text(status, now=deps.clock.now(), timezone=timezone))


# --- the assistant's tools (`yordamchi_bridge`) ----------------------------------------------------------------------
def _output(payload: JsonObject) -> ToolOutput:
    return ToolOutput(text=redact.scrubbed(json.dumps(payload, ensure_ascii=False, sort_keys=True)))


def toolset(deps: SharedDeps, *, timeout_s: float, tool_log: ToolLog) -> AssistantToolset:
    """The read-only status tools. Each call reads through the bridge once; failures are error results, never raises."""

    async def project_tool(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        name = arguments.get("project")
        if not isinstance(name, str) or not name.strip():
            return ToolOutput(text="missing_project", is_error=True)
        try:
            projects = await list_projects(deps, timeout=timeout_s)
            project = domain.find_project(name, projects)
            if project is None:
                return _output(domain.unknown_project_json(name, projects))
            status = await project_status(deps, project, timeout=timeout_s)
        except (BridgeUnavailable, BridgeNotReadOnly) as error:
            return ToolOutput(text=error.code.value, is_error=True)
        tool_log.read.append((project.project_id, status.checked_at))
        return _output(domain.status_json(status, now=deps.clock.now()))

    async def server_tool(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        del arguments
        try:
            views = await server_checks(deps, timeout=timeout_s)
        except (BridgeUnavailable, BridgeNotReadOnly) as error:
            return ToolOutput(text=error.code.value, is_error=True)
        return _output(domain.servers_json(views, now=deps.clock.now()))

    return AssistantToolset(
        server=SERVER,
        tools=(
            AssistantTool(
                name=PROJECT_TOOL,
                description=prompts.render(PROJECT_TOOL_PROMPT),
                input_schema=PROJECT_SCHEMA,
                timeout_s=timeout_s,
                handler=project_tool,
            ),
            AssistantTool(
                name=SERVER_TOOL,
                description=prompts.render(SERVER_TOOL_PROMPT),
                input_schema=SERVER_SCHEMA,
                timeout_s=timeout_s,
                handler=server_tool,
            ),
        ),
    )
