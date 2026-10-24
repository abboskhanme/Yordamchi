"""The gate at work (SPEC §3.2.3, §5.2, §5.7; UC-13; NFR-SEC-06, NFR-SEC-10): facts, decisions, grants, the owner's
answer, and the control-socket backends of the Layer 2 shims and the pre-push hook.

- `make_guard(deps, scope)` is the `SessionSpec.guard` of one executor session (T24 builds the scope). Every tool
  call goes through `check_call`: it fetches the facts `domain.needs` lists (≤ `FACT_ROUNDS` rounds, each bounded by
  a share of `gate.hook_timeout_s`; a fact that cannot be read stays unknown and the domain fails closed), then
  `domain.classify` decides. Allow → run. Forbid → deny, never asked. Ask → the exact action's grant is consumed (one
  conditional UPDATE: single use), or the call is denied at once with `gate_pending` and, the first time, ONE pending
  decision is stored and its question posted. The hook never waits for the owner.
- `answer_decision(deps, origin, reply)` binds an owner reply with `domain.bind_approval` and resolves it: "ha" turns
  the bound decision into a grant that expires (`domain.resolve_approval`), an expired one is asked again, "yo'q" or
  another instruction declines it. It returns what to deliver to the agent; `turns` (T24) delivers it.
- `check_shim` / `report_secret_block` answer the control socket's `gate_check` and `secret_blocked`
  (`bots/cli/protocol.py` holds the wire format; T34 mounts both).

Every verdict that is not a plain allow, every question, answer and consumed grant is an audit event written in the
unit of work of the change it describes. Ports (git, the Messenger) are awaited only outside units of work (A3).
"""

import asyncio
import json
import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import DecisionNotFound, ExternalError, YordamchiError
from yordamchi.core.json_value import JsonObject, JsonValue, dumps
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import domain, store
from yordamchi.dev.gate.domain import (
    Action,
    ApprovalOutcome,
    ConsumedGrant,
    DecisionState,
    GateContext,
    GateNeeds,
    GateRules,
    GateVerdict,
    GrantAction,
    OwnerVerdict,
    PendingItem,
    Policy,
    Reason,
    ScriptRef,
)
from yordamchi.dev.gate.models import Decision
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import FAIL_CLOSED, DeployBranches
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.agent import GuardFn, GuardVerdict, ToolCall
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

GATE_KIND: Final = "gate"
COMMAND_SHOWN_CHARS: Final = 300  # of the command in the owner's question
INPUT_STORED_CHARS: Final = 2000
MAX_DEPLOY_REFS: Final = 4  # a push sending more refs than this is judged as "every push deploys"
_FACT_ERRORS: Final = (YordamchiError, OSError, TimeoutError, ValueError)  # → the fact stays unknown (fail closed)
_RECENT_DECISIONS: Final = 200  # scanned to map a replied-to message to its decision until `message_links` (T15)


# --- who is asking --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GateScope:
    """One executor session of one project at this tool call, as `turns` (T24) knows it.

    `session_key` is a stable id of the session (`agent_sessions.id`): a grant consumed in it, and the shims called
    from it, match on it. `dirty_before_turn` is whether `git status` showed changes when the turn started (None =
    not recorded, so discarding commands ask).
    """

    project_id: str
    project_root: Path
    session_key: str
    policy: Policy = Policy.FULL
    turn_id: int | None = None
    dirty_before_turn: bool | None = None
    protected_pids: frozenset[int] = frozenset()
    mcp_read_only_tools: frozenset[str] = frozenset()
    mcp_preallowed_tools: frozenset[str] = frozenset()


def make_guard(deps: DevDeps, scope: Callable[[], GateScope]) -> GuardFn:
    """The `SessionSpec.guard` of one session. `scope` is read at every call: the turn and its facts change."""

    async def guard(call: ToolCall) -> GuardVerdict:
        return await check_call(deps, scope(), call)

    return guard


def _rules_of(snap: Snapshot) -> GateRules:
    return GateRules.from_settings(
        ask_classes=snap[defaults.GATE_ASK_CLASSES],
        readonly_remote_commands=snap[defaults.GATE_READONLY_REMOTE_COMMANDS],
        jump_only_hosts=snap[defaults.GATE_JUMP_ONLY_HOSTS],
        git_hosts=snap[defaults.GATE_GIT_HOSTS],
        http_write_allow_hosts=snap[defaults.GATE_HTTP_WRITE_ALLOW_HOSTS],
        temp_roots=snap[defaults.GATE_TEMP_ROOTS],
    )


# --- facts (SPEC §3.2.3 step 2) -------------------------------------------------------------------------------------
# A fetched fact is a change to the context; a fact that could not be read produces none and stays unknown.
type _Update = Callable[[GateContext], GateContext]


def _base_context(deps: DevDeps, scope: GateScope, cwd: str | None) -> GateContext:
    return GateContext(
        policy=scope.policy,
        home=str(deps.paths.home),
        cwd=cwd or str(scope.project_root),
        project_root=str(scope.project_root),
        data_dir=str(deps.paths.data),
        config_dir=str(deps.paths.config),
        dirty_before_turn=scope.dirty_before_turn,
        protected_pids=scope.protected_pids,
        mcp_read_only_tools=scope.mcp_read_only_tools,
        mcp_preallowed_tools=scope.mcp_preallowed_tools,
    )


def _private_roots(deps: DevDeps) -> tuple[Path, ...]:
    """Never read for a fact, whatever a call names: Yordamchi's data and keys, Claude's own files (the gate forbids
    the call anyway; the bot must not parse their content into a question or an audit row)."""
    return (deps.paths.data, deps.paths.config, deps.paths.home / ".claude")


def _read_prefix(path: Path, limit: int, private: tuple[Path, ...]) -> bytes | None:
    """Up to `limit` bytes of a regular file; None if there is no such file. A private path or anything but a regular
    file (a FIFO would block this thread for good) raises, so the fact stays unknown (worker thread only)."""
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if any(resolved.is_relative_to(root.resolve()) for root in private):
        raise PermissionError(path.name)
    if not resolved.is_file():
        raise IsADirectoryError(path.name)
    with resolved.open("rb") as file:
        return file.read(limit)


async def _read(path: Path, private: tuple[Path, ...]) -> bytes | None:
    return await asyncio.to_thread(_read_prefix, path, domain.SCRIPT_READ_BYTES + 1, private)


async def _first_text(directory: Path, names: Sequence[str], private: tuple[Path, ...]) -> str | None:
    """The text of the first of `names` that exists in `directory`. Too large to read raises (the fact stays
    unknown); a binary file reads as None."""
    for name in names:
        data = await _read(directory / name, private)
        if data is None:
            continue
        known, text = domain.script_text(data)
        if not known:
            raise ValueError(name)
        return text
    return None


async def _recipe(ref: ScriptRef, private: tuple[Path, ...]) -> domain.RecipeLookup:
    directory = Path(ref.directory)
    if ref.kind == "file":
        return domain.script_text(await _read(Path(ref.name), private))
    if ref.kind == "npm":
        text = await _first_text(directory, ("package.json",), private)
        return (True, None) if text is None else domain.package_script(json.loads(text), ref.name)
    if ref.kind == "make":
        makefile, _, target = ref.name.rpartition(":")
        text = await _first_text(directory, (makefile,) if makefile else domain.MAKEFILE_NAMES, private)
        return (True, None) if text is None else domain.makefile_recipe(text, target)
    for folder in (directory, *directory.parents):  # `just` looks for its justfile upward
        text = await _first_text(folder, domain.JUSTFILE_NAMES, private)
        if text is not None:
            return domain.justfile_recipe(text, ref.name)
    return True, None


async def _script_fact(ref: ScriptRef, private: tuple[Path, ...]) -> _Update | None:
    known, text = await _recipe(ref, private)
    if not known:
        return None
    return lambda ctx: replace(ctx, recipes={**ctx.recipes, ref: text})


async def _untracked_fact(deps: DevDeps, root: Path, *, timeout: float) -> _Update:
    files = frozenset(await deps.git.untracked_files(root, timeout=timeout))
    return lambda ctx: replace(ctx, untracked=files)


async def _default_branch_fact(deps: DevDeps, scope: GateScope, *, timeout: float) -> _Update:
    project = await projects.get_project(deps, scope.project_id)
    branch = project.default_branch or await deps.git.default_branch(scope.project_root, timeout=timeout)
    return lambda ctx: replace(ctx, default_branch=branch)


async def _current_branch_fact(deps: DevDeps, root: Path, *, timeout: float) -> _Update:
    branch = await deps.git.current_branch(root, timeout=timeout)  # None when detached: stays unknown
    return lambda ctx: replace(ctx, current_branch=branch)


async def _deploy_fact(deps: DevDeps, project_id: str, refs: Sequence[str], *, timeout: float) -> _Update:
    """Which pushes deploy, read fresh for the commits being pushed (the workflows there are the ones CI runs)."""
    found: DeployBranches = FAIL_CLOSED if len(refs) > MAX_DEPLOY_REFS else DeployBranches()
    for ref in refs[:MAX_DEPLOY_REFS] or ("HEAD",):
        found = found.union(await projects.deploy_branches(deps, project_id, ref=ref, timeout=timeout))
    return lambda ctx: replace(ctx, deploy_branches=found)


async def _db_hosts_fact(root: Path, private: tuple[Path, ...]) -> _Update:
    files: dict[str, str] = {}
    for name in domain.DB_CONFIG_FILES:
        text = await _first_text(root, (name,), private)
        if text is not None:
            files[name] = text
    hosts = domain.db_hosts_from(files)
    return lambda ctx: replace(ctx, db_hosts=hosts)


async def _remote_fact(deps: DevDeps, root: Path, name: str, *, timeout: float) -> _Update:
    remote = name
    if name == domain.DEFAULT_PUSH_REMOTE:  # a bare push goes to the upstream's remote, else origin
        upstream = (await deps.git.status(root, timeout=timeout)).upstream
        remote = upstream.split("/", 1)[0] if upstream else "origin"
    url = await deps.git.remote_url(root, remote=remote, timeout=timeout)  # never carries credentials
    return lambda ctx: replace(ctx, remote_urls={**ctx.remote_urls, name: url})


async def _attempt(fact: str, fetch: Awaitable[_Update | None], *, timeout: float) -> _Update | None:
    try:
        async with asyncio.timeout(timeout):
            return await fetch
    except _FACT_ERRORS as error:
        log.warning("gate_fact_failed", fact=fact, error_type=type(error).__name__)
        return None


type _Fetch = Callable[[], Awaitable[_Update | None]]


def _fetches(deps: DevDeps, scope: GateScope, need: GateNeeds, *, timeout: float) -> dict[str, _Fetch]:
    """How to fetch each fact `need` lists, keyed by a name that is fetched at most once per call."""
    root = scope.project_root
    private = _private_roots(deps)
    fetches: dict[str, _Fetch] = {}
    if need.untracked:
        fetches["untracked"] = partial(_untracked_fact, deps, root, timeout=timeout)
    if need.default_branch:
        fetches["default_branch"] = partial(_default_branch_fact, deps, scope, timeout=timeout)
    if need.current_branch:
        fetches["current_branch"] = partial(_current_branch_fact, deps, root, timeout=timeout)
    if need.deploy_branches:
        fetches["deploy_branches"] = partial(_deploy_fact, deps, scope.project_id, need.deploy_refs, timeout=timeout)
    if need.db_hosts:
        fetches["db_hosts"] = partial(_db_hosts_fact, root, private)
    for name in need.remotes:
        fetches[f"remote:{name}"] = partial(_remote_fact, deps, root, name, timeout=timeout)
    for ref in need.scripts:
        fetches[f"script:{ref.kind}:{ref.directory}:{ref.name}"] = partial(_script_fact, ref, private)
    return fetches


async def _context(
    deps: DevDeps, scope: GateScope, call: ToolCall, rules: GateRules, *, budget_s: float
) -> GateContext:
    """The facts `classify` needs for `call`, fetched outside any unit of work. Each is tried once; `needs` is asked
    again after each round, because a recipe can name further facts (≤ `FACT_ROUNDS` rounds)."""
    ctx = _base_context(deps, scope, call.cwd)
    tried: set[str] = set()
    timeout = domain.fact_timeout_s(budget_s)
    for _ in range(domain.FACT_ROUNDS):
        fetches = _fetches(deps, scope, domain.needs(call, rules, ctx), timeout=timeout)
        new = {name: fetch for name, fetch in fetches.items() if name not in tried}
        if not new:
            break
        tried |= new.keys()
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(_attempt(name.split(":", 1)[0], fetch(), timeout=timeout))
                for name, fetch in new.items()
            ]
        for task in tasks:
            update = task.result()
            if update is not None:
                ctx = update(ctx)
    return ctx


# --- texts ----------------------------------------------------------------------------------------------------------
_DENY_PROMPTS: Final = {
    Reason.READ_ONLY_POLICY: "dev/gate_read_only",
    Reason.PLAN_ONLY_POLICY: "dev/gate_plan_only",
    Reason.SELF_UPDATE_MAIN: "dev/gate_self_update_main",
}


def _describe(call: ToolCall) -> str:
    """The action as the owner reads it and the agent replays it: the Bash command, else the tool and its input."""
    command = call.tool_input.get("command")
    text = command.strip() if call.tool_name == "Bash" and isinstance(command, str) else ""
    if not text:
        text = f"{call.tool_name} {dumps(dict(call.tool_input))}"
    return redact.scrubbed(text)[:INPUT_STORED_CHARS]


def _forbidden_message(verdict: GateVerdict) -> str:
    target = redact.scrubbed(verdict.target or "-")
    name = _DENY_PROMPTS.get(verdict.reason)
    if name is not None:
        return prompts.render(name, target=target)
    return prompts.render("dev/gate_forbidden", reason=verdict.reason.value, target=target)


def _pending_message(decision_id: int) -> str:
    return prompts.render("dev/gate_pending", decision_id=str(decision_id))


def _payload(row: Decision) -> JsonObject:
    return dict(row.payload_json) if isinstance(row.payload_json, dict) else {}


def _question(row: Decision) -> BotText:
    """SPEC §3.2.3 step 4: one question naming the project, what runs, where, and the consequence."""
    template = S.DECISION_QUESTIONS.get(row.gate_class or "unclear", S.DECISION_QUESTIONS["unclear"])
    consequence = S.GATE_CONSEQUENCES.get(str(_payload(row).get("reason", "")))
    command = row.input_redacted or ""
    shown = command if len(command) <= COMMAND_SHOWN_CHARS else command[: COMMAND_SHOWN_CHARS - 1] + "…"
    return template(
        project=row.project_id or "?",
        decision_id=row.id,
        command=shown,
        where=row.target or row.project_id or "?",
        consequence=consequence() if consequence is not None else "—",
    )


_STATUSES: Final[dict[str, domain.DecisionStatus]] = {
    status: status for status in (*domain.OPEN_STATUSES, *domain.TERMINAL_STATUSES)
}


def _state(row: Decision) -> DecisionState:
    return DecisionState(
        id=row.id,
        status=_STATUSES.get(row.status, "cancelled"),
        created_at=row.created_at,
        expires_at=row.expires_at,
        grant_expires_at=row.grant_expires_at,
        consumed_at=row.consumed_at,
    )


async def _tell(deps: DevDeps, snap: Snapshot, text: BotText, *, reply_to: int | None) -> int | None:
    """One message to the dev group, now (until the outbox, T15); a failure is logged, never raised."""
    try:
        sent = await deps.messenger.send_text(
            snap[defaults.ACCESS_DEV_GROUP_ID], text, reply_to=reply_to, timeout=domain.NOTICE_SEND_TIMEOUT_S
        )
    except ExternalError as error:
        log.warning("gate_notice_failed", error_code=error.code.value)
        return None
    return sent.message_id


# --- the tool call (SPEC §3.2.3 steps 1–3) --------------------------------------------------------------------------
async def check_call(deps: DevDeps, scope: GateScope, call: ToolCall) -> GuardVerdict:
    """The gate's answer to one tool call. Never waits for the owner: an ask is denied at once."""
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    rules = _rules_of(snap)
    ctx = await _context(deps, scope, call, rules, budget_s=snap[defaults.GATE_HOOK_TIMEOUT_S])
    verdict = domain.classify(call, rules, ctx)
    if verdict.action is Action.ASK:
        covers = domain.remote_invocations(call, rules, ctx)
        result = await _ask(deps, scope, call, verdict, covers=covers, snap=snap)
    else:
        result = (
            GuardVerdict(allow=True)
            if verdict.allowed
            else GuardVerdict(allow=False, message=_forbidden_message(verdict), reason_code=verdict.reason.value)
        )
        if verdict.findings or not verdict.allowed:  # a plain free call is not an event worth a row
            async with deps.db.uow() as s:
                await _audit_verdict(s, deps, scope, call, verdict, outcome="allowed" if result.allow else "denied")
    log.info(
        "gate_checked",
        project=scope.project_id,
        tool=call.tool_name,
        action=verdict.action.value,
        reason=verdict.reason.value,
        allowed=result.allow,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


async def _audit_verdict(
    s: Session, deps: DevDeps, scope: GateScope, call: ToolCall, verdict: GateVerdict, *, outcome: str
) -> None:
    await audit.record_event(
        s,
        bot=deps.bot,
        at=deps.clock.now(),
        actor=AuditActor.EXECUTOR,
        kind=AuditKind.GATE_VERDICT,
        project_id=scope.project_id,
        ref_type="tool_call",
        ref_id=call.tool_use_id[:64] or None,
        data={
            "tool": call.tool_name,
            "agent_id": call.agent_id,
            "session_key": scope.session_key,
            "action": verdict.action.value,
            "reason": verdict.reason.value,
            "gate_class": verdict.gate_class.value if verdict.gate_class else None,
            "target": verdict.target,
            "findings": [f"{finding.action.value}:{finding.reason.value}" for finding in verdict.findings],
            "input_sha256": domain.input_sha256(call.tool_name, call.tool_input),
            "command": _describe(call)[:500],
            "outcome": outcome,
        },
    )


async def _ask(
    deps: DevDeps,
    scope: GateScope,
    call: ToolCall,
    verdict: GateVerdict,
    *,
    covers: tuple[tuple[str | None, ...], ...],
    snap: Snapshot,
) -> GuardVerdict:
    now = deps.clock.now()
    async with deps.db.uow() as s:
        action, decision_id = await _grant_step(
            s, deps, scope, call, verdict, covers=covers, now=now, ttl_min=snap[defaults.GATE_APPROVAL_TTL_MIN]
        )
    if action is GrantAction.USE_GRANT:
        log.info("grant_consumed", project=scope.project_id, decision_id=decision_id, tool=call.tool_name)
        return GuardVerdict(allow=True)
    if action is GrantAction.ASK_NEW:
        await post_question(deps, decision_id, snap=snap)
    return GuardVerdict(allow=False, message=_pending_message(decision_id), reason_code=verdict.reason.value)


async def _grant_step(
    s: Session,
    deps: DevDeps,
    scope: GateScope,
    call: ToolCall,
    verdict: GateVerdict,
    *,
    covers: tuple[tuple[str | None, ...], ...],
    now: datetime,
    ttl_min: int,
) -> tuple[GrantAction, int]:
    """In one unit of work: expire what is stale, then consume the exact action's grant, or keep its pending
    question, or store a new one (SPEC §3.2.3 step 3)."""
    key = domain.input_sha256(call.tool_name, call.tool_input)
    rows = await store.list_decisions_for_action(
        s, project_id=scope.project_id, tool_name=call.tool_name, input_sha256=key, statuses=domain.OPEN_STATUSES
    )
    step = domain.grant_step([_state(row) for row in rows], now=now)
    await _expire(s, deps, step.expired_ids, at=now)
    by_id = {row.id: row for row in rows}
    if step.action is GrantAction.USE_GRANT and step.decision_id is not None:
        row = by_id[step.decision_id]
        payload: JsonObject = {
            **_payload(row),
            "consumed_by": scope.session_key,
            "tool_use_id": call.tool_use_id,
            "covers": [list[JsonValue](invocation) for invocation in covers],
        }
        if await store.mark_decision_consumed(s, row.id, at=now, payload=payload):
            await _audit_decision(s, deps, row, AuditKind.GRANT_CONSUMED, AuditActor.EXECUTOR, {"covers": len(covers)})
            return GrantAction.USE_GRANT, row.id
        return GrantAction.STILL_PENDING, row.id  # another call consumed it first: this one does not run
    if step.action is GrantAction.STILL_PENDING and step.decision_id is not None:
        await _audit_verdict(s, deps, scope, call, verdict, outcome="still_pending")
        return GrantAction.STILL_PENDING, step.decision_id
    row = await store.add_decision(
        s,
        kind=GATE_KIND,
        project_id=scope.project_id,
        turn_id=scope.turn_id,
        gate_class=verdict.gate_class.value if verdict.gate_class else None,
        tool_name=call.tool_name,
        input_sha256=key,
        input_redacted=_describe(call),
        target=redact.scrubbed(verdict.target)[:200] or None,
        payload={
            "reason": verdict.reason.value,
            "session_key": scope.session_key,
            "findings": [finding.reason.value for finding in verdict.findings],
        },
        created_at=now,
        expires_at=domain.decision_expires_at(now, ttl_min),
    )
    await _audit_decision(s, deps, row, AuditKind.DECISION_ASKED, AuditActor.EXECUTOR, {"reason": verdict.reason.value})
    return GrantAction.ASK_NEW, row.id


async def _expire(s: Session, deps: DevDeps, decision_ids: Sequence[int], *, at: datetime) -> None:
    if await store.set_decisions_expired(s, decision_ids, at=at):
        for decision_id in decision_ids:
            await audit.record_event(
                s,
                bot=deps.bot,
                at=at,
                actor=AuditActor.SYSTEM,
                kind=AuditKind.DECISION_RESOLVED,
                ref_type="decision",
                ref_id=str(decision_id),
                data={"status": "expired"},
            )


async def _audit_decision(
    s: Session, deps: DevDeps, row: Decision, kind: AuditKind, actor: AuditActor, data: JsonObject
) -> None:
    await audit.record_event(
        s,
        bot=deps.bot,
        at=deps.clock.now(),
        actor=actor,
        kind=kind,
        project_id=row.project_id,
        ref_type="decision",
        ref_id=str(row.id),
        data={
            "decision_kind": row.kind,
            "gate_class": row.gate_class,
            "tool": row.tool_name,
            "input_sha256": row.input_sha256,
            "target": row.target,
            **data,
        },
    )


async def post_question(deps: DevDeps, decision_id: int, *, snap: Snapshot | None = None) -> bool:
    """Post a pending decision's one question and remember its message (the owner may reply to it). True if sent.

    Until the outbox (T15) this is a direct, bounded send after the decision committed; a decision whose send failed
    keeps no `message_id`, and `post_unasked_decisions` sends it later.
    """
    snap = snap or await settings.snapshot(deps)
    async with deps.db.read() as s:
        row = await store.get_decision(s, decision_id)
    if row is None or row.status != "pending":
        return False
    message_id = await _tell(deps, snap, _question(row), reply_to=None)
    if message_id is None:
        return False
    async with deps.db.uow() as s:
        current = await store.get_decision(s, decision_id)
        if current is not None:
            await store.set_decision_payload(s, decision_id, {**_payload(current), "message_id": message_id})
    log.info("decision_asked", project=row.project_id, decision_id=decision_id, gate_class=row.gate_class)
    return True


async def post_unasked_decisions(deps: DevDeps) -> int:
    """Startup and periodic (T34): send the question of every pending decision that has none yet. Returns how many."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.read() as s:
        rows = await store.list_decisions_with_status(s, ("pending",), limit=_RECENT_DECISIONS)
    unasked = [row.id for row in rows if "message_id" not in _payload(row) and now < row.expires_at]
    return sum([await post_question(deps, decision_id, snap=snap) for decision_id in unasked])


async def pending_items(deps: DevDeps) -> tuple[PendingItem, ...]:
    """The decisions still waiting for the owner, for `holat?` and for question binding (one pending item rule)."""
    now = deps.clock.now()
    async with deps.db.read() as s:
        rows = await store.list_decisions_with_status(s, ("pending",), limit=_RECENT_DECISIONS)
    return tuple(PendingItem("decision", row.id, row.project_id) for row in rows if now < row.expires_at)


# --- the owner's answer (SPEC §3.2.3 steps 5–8) ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionReply:
    """An owner message that may answer a decision. `names` are the spellings routing matched for the mentioned
    projects; `open_questions` the agent questions waiting too (`dev/questions`), so a bare "ha" with a question also
    open approves nothing."""

    text: str
    reply_to_message_id: int | None = None
    mentioned_projects: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    open_questions: tuple[PendingItem, ...] = ()


class AnswerKind(StrEnum):
    NOT_AN_APPROVAL = "not_an_approval"  # not about a decision: questions or routing take the message
    AMBIGUOUS = "ambiguous"  # `S.WHICH_DECISION` was sent; nothing changed
    APPROVED = "approved"
    DECLINED = "declined"
    INSTRUCTED = "instructed"  # declined, and the owner's text goes to the agent instead
    REASKED = "reasked"  # it had expired: a fresh decision was asked
    ALREADY_APPROVED = "already_approved"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    """What happened. `delivery` is for the agent of `turn_id` (T24 sends it: `next` if the turn is live, else
    `new_turn`); `reply` is what the owner was told."""

    kind: AnswerKind
    decision_id: int | None = None
    project_id: str | None = None
    turn_id: int | None = None
    delivery: str = ""
    reply: BotText | None = None


@dataclass(frozen=True, slots=True)
class _Resolved:
    answer: DecisionAnswer
    message_id: int | None = None  # the question the owner's reply should thread under
    new_decision_id: int | None = None  # a fresh decision whose question is still to be posted


async def answer_decision(deps: DevDeps, origin: OwnerOrigin, reply: DecisionReply) -> DecisionAnswer:
    """Bind an owner message to a decision and resolve it (R-18: a stray "ha" approves nothing). Only an owner
    update can approve (`origin`); executor output never reaches this. Raises `DecisionNotFound` for an unknown
    `#id`."""
    if origin.bot is not deps.bot:
        msg = f"an owner origin of the {origin.bot} bot cannot answer {deps.bot} decisions"
        raise ValueError(msg)
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.read() as s:
        recent = await store.list_recent_decisions(s, limit=_RECENT_DECISIONS)
    reply_to: int | None = None
    if reply.reply_to_message_id is not None:
        reply_to = next(
            (row.id for row in recent if _payload(row).get("message_id") == reply.reply_to_message_id), None
        )
        if reply_to is None:
            return DecisionAnswer(AnswerKind.NOT_AN_APPROVAL)  # a reply to another message is about that message
    pending = [PendingItem("decision", row.id, row.project_id) for row in reversed(recent) if row.status == "pending"]
    binding = domain.bind_approval(
        reply.text,
        reply_to=reply_to,
        pending=[*pending, *reply.open_questions],
        mentioned_projects=reply.mentioned_projects,
        names=reply.names,
    )
    if isinstance(binding, domain.NotAnApproval):
        return DecisionAnswer(AnswerKind.NOT_AN_APPROVAL)
    if isinstance(binding, domain.Ambiguous):
        projects_of = {row.id: row.project_id or "?" for row in recent}
        items = (S.DECISION_ITEM(decision_id=i, project=projects_of.get(i, "?")) for i in binding.decision_ids)
        text = S.WHICH_DECISION(decisions=", ".join(items))
        await _tell(deps, snap, text, reply_to=None)
        log.info("decision_reply_ambiguous", decisions=len(binding.decision_ids))
        return DecisionAnswer(AnswerKind.AMBIGUOUS, reply=text)
    async with deps.db.uow() as s:
        resolved = await _resolve(s, deps, origin, binding, now=now, ttl_min=snap[defaults.GATE_APPROVAL_TTL_MIN])
    if resolved.answer.reply is not None:
        await _tell(deps, snap, resolved.answer.reply, reply_to=resolved.message_id)
    if resolved.new_decision_id is not None:
        await post_question(deps, resolved.new_decision_id, snap=snap)
    log.info("decision_answered", decision_id=resolved.answer.decision_id, kind=resolved.answer.kind.value)
    return resolved.answer


async def _resolve(
    s: Session, deps: DevDeps, origin: OwnerOrigin, binding: domain.Bound, *, now: datetime, ttl_min: int
) -> _Resolved:
    row = await store.get_decision(s, binding.decision_id)
    if row is None:
        raise DecisionNotFound(decision_id=binding.decision_id)
    if binding.verdict is OwnerVerdict.APPROVE:
        return await _approve(s, deps, origin, row, now=now, ttl_min=ttl_min)
    return await _decline(s, deps, origin, row, binding, now=now)


def _answered(row: Decision, kind: AnswerKind, reply: BotText, delivery: str = "") -> _Resolved:
    message_id = _payload(row).get("message_id")
    answer = DecisionAnswer(kind, row.id, row.project_id, row.turn_id, delivery, reply)
    return _Resolved(answer, message_id if isinstance(message_id, int) else None)


async def _approve(
    s: Session, deps: DevDeps, origin: OwnerOrigin, row: Decision, *, now: datetime, ttl_min: int
) -> _Resolved:
    """ "ha": a single-use grant that expires (step 6); an expired question is asked again instead (step 8)."""
    project = row.project_id or "?"
    result = domain.resolve_approval(_state(row), now=now, ttl_min=ttl_min)
    if result.outcome is ApprovalOutcome.APPROVED:
        await store.mark_decision_resolved(
            s, row.id, status="approved", at=now, update_id=origin.update_id, grant_expires_at=result.grant_expires_at
        )
        await _audit_decision(s, deps, row, AuditKind.DECISION_RESOLVED, AuditActor.OWNER, {"status": "approved"})
        delivery = ""
        if row.kind == GATE_KIND:
            delivery = prompts.render("dev/gate_approved", decision_id=str(row.id), command=row.input_redacted or "")
        return _answered(row, AnswerKind.APPROVED, S.DECISION_APPROVED(project=project, decision_id=row.id), delivery)
    if result.outcome is ApprovalOutcome.REASK:
        fresh = await _reask(s, deps, row, now=now, ttl_min=ttl_min)
        resolved = _answered(row, AnswerKind.REASKED, S.DECISION_EXPIRED(project=project, decision_id=row.id))
        return replace(resolved, answer=replace(resolved.answer, decision_id=fresh), new_decision_id=fresh)
    if result.outcome is ApprovalOutcome.ALREADY_APPROVED:
        reply = S.DECISION_ALREADY_APPROVED(project=project, decision_id=row.id)
        return _answered(row, AnswerKind.ALREADY_APPROVED, reply)
    return _answered(row, AnswerKind.CLOSED, S.DECISION_CLOSED(project=project, decision_id=row.id))


async def _decline(
    s: Session, deps: DevDeps, origin: OwnerOrigin, row: Decision, binding: domain.Bound, *, now: datetime
) -> _Resolved:
    """ "yo'q", or another instruction (step 7): a pending question is denied, an unused grant is withdrawn."""
    project = row.project_id or "?"
    status = "denied" if row.status == "pending" else "cancelled"
    changed = row.status in domain.OPEN_STATUSES and await store.mark_decision_resolved(
        s, row.id, status=status, at=now, update_id=origin.update_id, from_status=row.status
    )
    if not changed:
        return _answered(row, AnswerKind.CLOSED, S.DECISION_CLOSED(project=project, decision_id=row.id))
    data: JsonObject = {"status": status, "verdict": binding.verdict.value}
    await _audit_decision(s, deps, row, AuditKind.DECISION_RESOLVED, AuditActor.OWNER, data)
    if binding.verdict is OwnerVerdict.INSTRUCT:
        delivery = prompts.render("dev/gate_instructed", decision_id=str(row.id), instruction=binding.instruction)
        reply = S.DECISION_REDIRECTED(project=project, decision_id=row.id)
        return _answered(row, AnswerKind.INSTRUCTED, reply, delivery)
    delivery = prompts.render("dev/gate_declined", decision_id=str(row.id))
    return _answered(row, AnswerKind.DECLINED, S.DECISION_DECLINED(project=project, decision_id=row.id), delivery)


async def _reask(s: Session, deps: DevDeps, row: Decision, *, now: datetime, ttl_min: int) -> int:
    """SPEC §3.2.3 step 8: a "ha" to an expired decision asks again, as a fresh decision about the same action."""
    await _expire(s, deps, [row.id], at=now)
    payload = {key: value for key, value in _payload(row).items() if key != "message_id"}
    fresh = await store.add_decision(
        s,
        kind=row.kind,
        project_id=row.project_id,
        turn_id=row.turn_id,
        gate_class=row.gate_class,
        tool_name=row.tool_name,
        input_sha256=row.input_sha256,
        input_redacted=row.input_redacted,
        target=row.target,
        payload={**payload, "reasked_from": row.id},
        created_at=now,
        expires_at=domain.decision_expires_at(now, ttl_min),
    )
    await store.set_superseded_by(s, row.id, fresh.id)
    await _audit_decision(s, deps, fresh, AuditKind.DECISION_ASKED, AuditActor.SYSTEM, {"reasked_from": row.id})
    return fresh.id


# --- the control socket: shims and the pre-push hook (SPEC §3.1.1, §5.7 Layer 2) ------------------------------------
@dataclass(frozen=True, slots=True)
class ShimRequest:
    """`gate_check`: a shim is about to run `tool argv…` in `cwd`."""

    tool: str
    argv: tuple[str, ...]
    cwd: str


@dataclass(frozen=True, slots=True)
class ShimAnswer:
    allow: bool
    reason: str


def _consumed_grant(row: Decision, session_key: str) -> ConsumedGrant | None:
    payload = _payload(row)
    covers = payload.get("covers")
    if payload.get("consumed_by") != session_key or row.consumed_at is None or not isinstance(covers, list):
        return None
    invocations = tuple(
        tuple(part if isinstance(part, str) else None for part in item)
        for item in covers
        if isinstance(item, list) and all(part is None or isinstance(part, str) for part in item)
    )
    return ConsumedGrant(session_key, row.consumed_at, invocations)


async def check_shim(deps: DevDeps, scope: GateScope | None, request: ShimRequest) -> ShimAnswer:
    """May a shim run the real ssh/scp/sftp/rsync? Only a read-only (or git-host) command, or one a grant consumed in
    this session within `gate.shim_grant_window_s` covers. An unknown session token is refused."""
    if scope is None:
        log.warning("shim_refused", tool=request.tool, reason="unknown_session")
        return ShimAnswer(allow=False, reason="unknown_session")
    snap = await settings.snapshot(deps)
    rules = _rules_of(snap)
    command = shlex.join([request.tool, *request.argv])
    call = ToolCall("Bash", {"command": command}, "shim", None, scope.session_key, cwd=request.cwd or None)
    ctx = await _context(deps, scope, call, rules, budget_s=snap[defaults.GATE_HOOK_TIMEOUT_S])
    now = deps.clock.now()
    window_s = snap[defaults.GATE_SHIM_GRANT_WINDOW_S]
    async with deps.db.read() as s:
        rows = await store.list_consumed_grants(s, project_id=scope.project_id, since=now - timedelta(seconds=window_s))
    grants = [grant for row in rows if (grant := _consumed_grant(row, scope.session_key)) is not None]
    verdict = domain.shim_verdict(
        request.tool,
        request.argv,
        session_id=scope.session_key,
        rules=rules,
        ctx=ctx,
        grants=grants,
        now=now,
        window_s=window_s,
    )
    if verdict.findings or not verdict.allowed:
        async with deps.db.uow() as s:
            outcome = "shim_allowed" if verdict.allowed else "shim_refused"
            await _audit_verdict(s, deps, scope, call, verdict, outcome=outcome)
    log.info("shim_checked", project=scope.project_id, tool=request.tool, allowed=verdict.allowed)
    return ShimAnswer(allow=verdict.allowed, reason=verdict.reason.value)


@dataclass(frozen=True, slots=True)
class SecretReport:
    """`secret_blocked`: the pre-push scan found a secret at `file:line` by `rule` (a pattern kind, a file-name rule
    or `vault_value`). Never the secret itself."""

    cwd: str
    file: str
    line: int
    rule: str


@dataclass(frozen=True, slots=True)
class SecretAnswer:
    ok: bool
    enforce: bool  # False only when the owner switched `gate.secret_scan_enabled` off: the push may go on


async def report_secret_block(deps: DevDeps, scope: GateScope | None, report: SecretReport) -> SecretAnswer:
    """The pre-push hook found a secret: audit it and tell the owner which file and line (UC-13, NFR-SEC-02). The
    hook blocks unless the answer says the owner switched the scan off; an unknown session is always blocked."""
    snap = await settings.snapshot(deps)
    enforce = snap[defaults.GATE_SECRET_SCAN_ENABLED] or scope is None
    file = redact.scrubbed(report.file)[:300]
    async with deps.db.uow() as s:
        await audit.record_event(
            s,
            bot=deps.bot,
            at=deps.clock.now(),
            actor=AuditActor.EXECUTOR,
            kind=AuditKind.GATE_VERDICT,
            project_id=scope.project_id if scope else None,
            ref_type="pre_push",
            data={
                "session_key": scope.session_key if scope else None,
                "file": file,
                "line": report.line,
                "rule": report.rule,
                "enforced": enforce,
            },
        )
    if enforce and scope is not None:
        text = S.PUSH_BLOCKED_SECRET(project=scope.project_id, file=file, line=report.line, rule=report.rule)
        await _tell(deps, snap, text, reply_to=None)
    log.warning("push_secret_found", project=scope.project_id if scope else None, rule=report.rule, enforced=enforce)
    return SecretAnswer(ok=True, enforce=enforce)


# --- retention (SPEC §4.2: 90 days; open decisions are never deleted) ----------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    cutoff = domain.retention_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(
        deps.db,
        lambda s, limit: store.delete_decisions_before(s, cutoff, statuses=domain.TERMINAL_STATUSES, limit=limit),
    )
    log.info("decisions_retention_applied", deleted=deleted)
    return deleted
