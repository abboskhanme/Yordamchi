"""The personal agent's guard and the owner's word on it (SPEC §3.2.18 items 1–2; REQUIREMENTS D15).

- `agent_mode(deps, state)` is this answer's `AgentMode` when `agent.enabled`: the workspace from
  `agent.workspace_dir` (a leading `~` is the owner's home, `deps.paths.home`) and the guard below. The adapter creates
  the folder 0700 and refuses one inside Yordamchi's data dir. None = the restricted session of SPEC §5.3.
- `check_call(deps, state, workspace, call)`, the guard: the gate classifier (`domain.judge`) with `Policy.FULL` and
  the workspace as the project root, then `domain.step_of`. It never waits for the owner: an ask is a deny with the
  pending message, and the FIRST ask of an answer is kept in `state.pending` for the pipeline to post after it.
  A forbidden call is denied and never asked; a live grant for exactly this call is used up (single use).
- `answer_ask(deps, state, origin, ask, …)`: the owner's "ha" stores a grant for that exact call
  (`gate.approval_ttl_min`), "yo'q" a refusal for the same window; either way it returns the trusted follow-up the
  pipeline queues into the same conversation, so the agent carries on (approved: retry it; refused: say what instead).

The state lives in memory, like the settings question waiting in `bots/personal/pipeline.py`: a restart forgets a
question and its grants, and the agent simply asks again. Every verdict that is not a plain allow, and every answer,
is an audit event; logs carry ids and codes only (never the command).
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core.json_value import JsonObject
from yordamchi.core.log import get_log
from yordamchi.personal.agent import domain
from yordamchi.personal.agent.domain import Grant, PendingAsk, Step
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa.domain import QaQuestion
from yordamchi.ports.agent import GuardFn, GuardVerdict, ToolCall
from yordamchi.ports.assistant import AgentMode
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.settings.registry import FromBootstrap, PerBot, Setting, SettingValue

log = get_log(__name__)

FORBIDDEN_PROMPT: Final = "personal/agent_forbidden"
PENDING_PROMPT: Final = "personal/agent_pending"
REFUSED_PROMPT: Final = "personal/agent_refused"
APPROVED_PROMPT: Final = "personal/agent_approved"
DECLINED_PROMPT: Final = "personal/agent_declined"
AUDIT_COMMAND_CHARS: Final = 500  # of the scrubbed command in an audit row (as the dev gate's)
_HOME_PREFIX: Final = "~/"


@dataclass(slots=True)
class AgentState:
    """Built once by the pipeline. `pending` is the question the guard registered in the answer being written;
    `grants` and `refusals` are the owner's words on exact calls, each usable until it expires."""

    pending: PendingAsk | None = None
    grants: tuple[Grant, ...] = ()
    refusals: tuple[Grant, ...] = ()
    asked: int = 0  # numbers the questions of this process

    def take_pending(self) -> PendingAsk | None:
        """The registered question, once: the pipeline posts it, or drops it with a stopped answer."""
        pending, self.pending = self.pending, None
        return pending


# --- the session ----------------------------------------------------------------------------------------------------
async def agent_mode(deps: PersonalDeps, state: AgentState) -> AgentMode | None:
    """This answer's agent mode (module docstring); None when `agent.enabled` is off."""
    snap = await settings.snapshot(deps)
    if not snap[defaults.AGENT_ENABLED]:
        return None
    workspace = workspace_of(deps, snap[defaults.AGENT_WORKSPACE_DIR])
    return AgentMode(workspace=workspace, guard=make_guard(deps, state, workspace))


def workspace_of(deps: PersonalDeps, configured: str) -> Path:
    """`agent.workspace_dir` as an absolute path: `~` and a relative path are under the owner's home."""
    text = configured.strip()
    if text == "~":
        return deps.paths.home
    if text.startswith(_HOME_PREFIX):
        return deps.paths.home / text.removeprefix(_HOME_PREFIX)
    path = Path(text)
    return path if path.is_absolute() else deps.paths.home / path


def make_guard(deps: PersonalDeps, state: AgentState, workspace: Path) -> GuardFn:
    async def guard(call: ToolCall) -> GuardVerdict:
        return await check_call(deps, state, workspace, call)

    return guard


# --- the guard ------------------------------------------------------------------------------------------------------
def _gate_value[T: SettingValue](snap: Snapshot, setting: Setting[T]) -> T:
    """A `gate.*` value. They are the dev bot's settings (`bots=DEV`), so the personal snapshot does not hold them and
    the personal guard uses their declared defaults (ASSUMPTIONS 2026-10-31); a value in this bot's scope wins."""
    if setting.key in snap.values:
        return snap[setting]
    default = setting.default
    if isinstance(default, (PerBot, FromBootstrap)):
        msg = f"setting {setting.key!r} has no shared default"
        raise TypeError(msg)
    return default


def _rules_of(snap: Snapshot) -> domain.Rules:
    return domain.gate_rules(
        ask_classes=_gate_value(snap, defaults.GATE_ASK_CLASSES),
        readonly_remote_commands=_gate_value(snap, defaults.GATE_READONLY_REMOTE_COMMANDS),
        jump_only_hosts=_gate_value(snap, defaults.GATE_JUMP_ONLY_HOSTS),
        git_hosts=_gate_value(snap, defaults.GATE_GIT_HOSTS),
        http_write_allow_hosts=_gate_value(snap, defaults.GATE_HTTP_WRITE_ALLOW_HOSTS),
        temp_roots=_gate_value(snap, defaults.GATE_TEMP_ROOTS),
    )


def _what(call: ToolCall) -> str:
    """The action, scrubbed before it is cut (a cut could split a key the scrubber would then miss)."""
    return redact.scrubbed(domain.describe(call))


def _message(step: Step, verdict: domain.Verdict) -> str:
    if step is Step.FORBID:
        target = redact.scrubbed(verdict.target or "-")
        return prompts.render(FORBIDDEN_PROMPT, reason=verdict.reason.value, target=target)
    if step is Step.REFUSED:
        return prompts.render(REFUSED_PROMPT)
    return prompts.render(PENDING_PROMPT)


async def check_call(deps: PersonalDeps, state: AgentState, workspace: Path, call: ToolCall) -> GuardVerdict:
    """The guard's answer to one tool call (module docstring). Never waits for the owner."""
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    ctx = domain.gate_context(
        home=str(deps.paths.home),
        workspace=str(workspace),
        cwd=call.cwd,
        data_dir=str(deps.paths.data),
        config_dir=str(deps.paths.config),
        protected_pids=frozenset({os.getpid()}),
    )
    verdict = domain.judge(call, _rules_of(snap), ctx)
    now = deps.clock.now()
    key = domain.key_of(call)
    step = domain.step_of(
        verdict, key=key, grants=state.grants, refusals=state.refusals, pending=state.pending, now=now
    )
    # The state changes before the next await: two calls of one answer never both use one grant or both ask.
    if step is Step.GRANTED:
        state.grants = domain.without(domain.live(state.grants, now), key)
    elif step is Step.ASK and state.pending is None:
        state.asked += 1
        state.pending = PendingAsk(
            number=state.asked, key=key, what=domain.shortened(_what(call)), reason=verdict.reason.value
        )
    result = (
        GuardVerdict(allow=True)
        if step.allowed
        else GuardVerdict(allow=False, message=_message(step, verdict), reason_code=verdict.reason.value)
    )
    if step is not Step.ALLOW or verdict.findings:  # a plain free call is not an event worth a row
        data: JsonObject = {
            "tool": call.tool_name,
            "action": verdict.action.value,
            "reason": verdict.reason.value,
            "gate_class": verdict.gate_class.value if verdict.gate_class else None,
            "target": verdict.target,
            "step": step.value,
            "input_sha256": key,
            "command": _what(call)[:AUDIT_COMMAND_CHARS],
        }
        kind = AuditKind.GRANT_CONSUMED if step is Step.GRANTED else AuditKind.GATE_VERDICT
        async with deps.db.uow() as s:
            await audit.record_event(
                s, bot=deps.bot, at=now, actor=AuditActor.EXECUTOR, kind=kind, ref_type="tool_call", data=data
            )
    log.info(
        "agent_guard_checked",
        tool=call.tool_name,
        action=verdict.action.value,
        reason=verdict.reason.value,
        step=step.value,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


# --- the owner's word -----------------------------------------------------------------------------------------------
async def answer_ask(
    deps: PersonalDeps,
    state: AgentState,
    origin: OwnerOrigin,
    ask: PendingAsk,
    *,
    approved: bool,
    chat_id: int,
    burst_id: int | None,
) -> QaQuestion:
    """The owner's "ha" or "yo'q" on `ask` (only from his own verified update): a grant or a refusal for that exact
    call, and the follow-up the pipeline queues into the same conversation as ordinary work."""
    origin = require_owner_origin(origin, deps.bot)
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    word = domain.grant_of(ask.key, now=now, ttl_min=_gate_value(snap, defaults.GATE_APPROVAL_TTL_MIN))
    if approved:
        state.grants = (*domain.live(state.grants, now), word)
    else:
        state.refusals = (*domain.live(state.refusals, now), word)
    async with deps.db.uow() as s:
        await audit.record_event(
            s,
            bot=deps.bot,
            at=now,
            actor=AuditActor.OWNER,
            kind=AuditKind.DECISION_RESOLVED,
            ref_type="update",
            ref_id=str(origin.update_id),
            data={"number": ask.number, "approved": approved, "reason": ask.reason, "input_sha256": ask.key},
        )
    log.info("agent_ask_answered", number=ask.number, approved=approved)
    prompt = prompts.render(APPROVED_PROMPT if approved else DECLINED_PROMPT, what=ask.what)
    return QaQuestion(chat_id=chat_id, own_text=prompt, burst_id=burst_id)
