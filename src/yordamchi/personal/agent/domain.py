"""The personal agent's guard, pure (SPEC §3.2.18 item 2): the gate classifier's verdict, the owner's grants and
refusals, and the one question that may wait for him.

The classifier is the dev bot's (`dev/gate/domain.classify`, SPEC §5.7). It is the one module the personal side takes
from `dev` (C1 and C7-personal carry exactly this import as an exception, REQUIREMENTS D15), and only this module
imports it: the service reaches the classifier through `gate_rules`, `gate_context` and `judge`.

The personal workspace is not a git project and has no registry entry, so the facts the dev service reads from git
and the projects table stay unknown here and the classifier fails toward asking (§5.7: "an extra question is
acceptable; a miss is not").

`step_of` maps one verdict to what the guard does:
- allow → run it;
- forbid → deny with the gate's reason, never asked;
- ask → a live grant for exactly this call (`input_sha256`) is used up and the call runs; a live refusal denies it
  without asking again; else the call is denied at once and, when no other question waits, ONE ask is registered
  for the pipeline to post after the answer (the guard never waits for the owner).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.json_value import dumps
from yordamchi.core.text import BotText
from yordamchi.dev.gate.domain import Action, GateContext, GateRules, GateVerdict, Policy, classify, input_sha256
from yordamchi.ports.agent import ToolCall

# The classifier's types, named here so the service needs no `dev` import of its own.
type Verdict = GateVerdict
type Rules = GateRules

WHAT_SHOWN_CHARS: Final = 300  # of the action in the owner's question (as the dev gate's COMMAND_SHOWN_CHARS)


class Step(StrEnum):
    """What the guard does with one tool call; the values are the log's `step` and the audit's `outcome`."""

    ALLOW = "allow"  # free: it runs
    GRANTED = "granted"  # an ask the owner approved: the grant is used up and it runs
    FORBID = "forbid"  # never allowed, never asked
    REFUSED = "refused"  # an ask the owner said no to, within the approval window: denied, not asked again
    ASK = "ask"  # denied now; this call becomes the question posted after the answer
    WAITING = "waiting"  # denied now; another question already waits, so this one is not asked

    @property
    def allowed(self) -> bool:
        return self in {Step.ALLOW, Step.GRANTED}


@dataclass(frozen=True, slots=True)
class Grant:
    """The owner's word on one exact call (`input_sha256`), usable until `expires_at` (`gate.approval_ttl_min`)."""

    key: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PendingAsk:
    """The one question the guard registered during an answer. `what` is already scrubbed and short."""

    number: int
    key: str
    what: str
    reason: str


# --- the classifier, as the personal side calls it ------------------------------------------------------------------
def gate_rules(
    *,
    ask_classes: Iterable[str],
    readonly_remote_commands: Iterable[str],
    jump_only_hosts: Iterable[str],
    git_hosts: Iterable[str],
    http_write_allow_hosts: Iterable[str],
    temp_roots: Iterable[str],
) -> Rules:
    """The `gate.*` setting values, as `dev/gate/service._rules_of` passes them."""
    return GateRules.from_settings(
        ask_classes=ask_classes,
        readonly_remote_commands=readonly_remote_commands,
        jump_only_hosts=jump_only_hosts,
        git_hosts=git_hosts,
        http_write_allow_hosts=http_write_allow_hosts,
        temp_roots=temp_roots,
    )


def gate_context(
    *,
    home: str,
    workspace: str,
    cwd: str | None,
    data_dir: str,
    config_dir: str,
    protected_pids: frozenset[int] = frozenset(),
) -> GateContext:
    """`Policy.FULL` with the workspace as the project root (as `dev/gate/service._base_context`); every git and
    project fact stays unknown, so a call that depends on one is asked about."""
    return GateContext(
        policy=Policy.FULL,
        home=home,
        cwd=cwd or workspace,
        project_root=workspace,
        data_dir=data_dir,
        config_dir=config_dir,
        protected_pids=protected_pids,
    )


def judge(call: ToolCall, rules: Rules, ctx: GateContext) -> Verdict:
    return classify(call, rules, ctx)


def key_of(call: ToolCall) -> str:
    """The grant key: the exact tool and input (the dev gate's `input_sha256`)."""
    return input_sha256(call.tool_name, call.tool_input)


def describe(call: ToolCall) -> str:
    """The action as the owner reads it: the Bash command, else the tool and its input (not yet scrubbed)."""
    command = call.tool_input.get("command")
    text = command.strip() if call.tool_name == "Bash" and isinstance(command, str) else ""
    if not text:
        text = f"{call.tool_name} {dumps(dict(call.tool_input))}"
    return text


def shortened(text: str, limit: int = WHAT_SHOWN_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- the owner's word -----------------------------------------------------------------------------------------------
def live(grants: Sequence[Grant], now: datetime) -> tuple[Grant, ...]:
    """The grants (or refusals) still inside their window."""
    return tuple(grant for grant in grants if grant.expires_at > now)


def grant_of(key: str, *, now: datetime, ttl_min: int) -> Grant:
    return Grant(key=key, expires_at=now + timedelta(minutes=ttl_min))


def without(grants: Sequence[Grant], key: str) -> tuple[Grant, ...]:
    """`grants` with the first one for `key` used up (single use)."""
    for index, grant in enumerate(grants):
        if grant.key == key:
            return (*grants[:index], *grants[index + 1 :])
    return tuple(grants)


def step_of(
    verdict: Verdict,
    *,
    key: str,
    grants: Sequence[Grant],
    refusals: Sequence[Grant],
    pending: PendingAsk | None,
    now: datetime,
) -> Step:
    """The guard's move for one classified call (module docstring)."""
    if verdict.action is Action.ALLOW:
        return Step.ALLOW
    if verdict.action is Action.FORBID:
        return Step.FORBID
    if any(grant.key == key for grant in live(grants, now)):
        return Step.GRANTED
    if any(refusal.key == key for refusal in live(refusals, now)):
        return Step.REFUSED
    if pending is not None and pending.key != key:
        return Step.WAITING
    return Step.ASK


def ask_text(ask: PendingAsk) -> BotText:
    """The owner's question: what Claude wants to run and the gate's consequence of it (SPEC §5.7)."""
    consequence = S.GATE_CONSEQUENCES.get(ask.reason)
    return S.AGENT_ASK(number=ask.number, what=ask.what, consequence=consequence() if consequence is not None else "—")
