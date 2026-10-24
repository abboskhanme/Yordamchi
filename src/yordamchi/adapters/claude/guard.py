"""The gate bridge: `PreToolUse` and `can_use_tool` → `SessionSpec.guard` (SPEC §5.2, §3.2.2, §3.2.3; S3, N6).

- **Fail closed.** Every tool call, in the main thread or a subagent (`agent_id`), goes through `decide()`: a guard that
  raises, returns something else, or takes longer than `spec.hook_timeout_s` is a deny. The CLI's own hook timeout is
  set `HOOK_MARGIN_S` later, because a PreToolUse hook the CLI times out does not block (STACK §4.1): our deny must
  always land first. Nothing here ever raises into the SDK.
- **Deny-all while stopping** and before a tool-restricted session's `init` is verified (`blocked()`).
- **`ToolDenied` comes from here**, the moment a deny is returned: `ResultMessage.permission_denials` misses denials
  inside a background subagent (S3), so it is only a cross-check.
- **Never waits for the owner in the hook.** `guard` must answer at once (the gate stores a pending decision and
  denies, STACK §4.1). The one hold is `AskUserQuestion` in `can_use_tool`, by design (§3.2.2): main-thread questions
  become `QuestionAsked` and wait up to `spec.question_hold_s` for `answer()`; then they are denied with
  `spec.question_parked_message`. Subagents never get `AskUserQuestion` (S3); if one does, it is denied and the
  subagent reports the question instead (§3.2.2 step 6 is the gate's deny + message).
- **`can_use_tool`** is reached under bypass only by `AskUserQuestion` and by calls no mode auto-approves; the latter
  go through the guard again.

Logs carry the tool name, reason code and error type only, never tool input or answers.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import (
    HookContext,
    HookEvent,
    HookInput,
    HookJSONOutput,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookSpecificOutput,
    SyncHookJSONOutput,
    ToolPermissionContext,
    UserPromptSubmitHookSpecificOutput,
)

from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.ports.agent import AgentEvent, GuardVerdict, QuestionAsked, SessionSpec, ToolCall, ToolDenied

log = get_log(__name__)

type Emit = Callable[[AgentEvent], None]
type Blocked = Callable[[], str | None]
type PermissionResult = PermissionResultAllow | PermissionResultDeny

ASK_TOOL: Final = "AskUserQuestion"
HOOK_MARGIN_S: Final = 5.0  # the CLI's hook timeout = our guard timeout + this
CONTEXT_HOOK_TIMEOUT_S: Final = 10.0  # SPEC §5.3: UserPromptSubmit(context, timeout 10)
REASON_TIMEOUT: Final = "guard_timeout"
REASON_ERROR: Final = "guard_error"
REASON_DENIED: Final = "denied"
REASON_PARKED: Final = "question_parked"
REASON_SUBAGENT_QUESTION: Final = "subagent_question"
SUBAGENT_QUESTION_MESSAGE: Final = (
    "AskUserQuestion is not available inside a subagent. Finish the independent work and report the open "
    "question in your final message to the main agent."
)


def deny_output(reason: str) -> HookJSONOutput:
    specific = PreToolUseHookSpecificOutput(
        hookEventName="PreToolUse", permissionDecision="deny", permissionDecisionReason=reason
    )
    return SyncHookJSONOutput(hookSpecificOutput=specific)


def allow_output(*, explicit: bool) -> HookJSONOutput:
    """`{}` lets the session's mode decide; `dontAsk` denies anything not pre-approved, so it gets an explicit allow."""
    if not explicit:
        return SyncHookJSONOutput()
    specific = PreToolUseHookSpecificOutput(hookEventName="PreToolUse", permissionDecision="allow")
    return SyncHookJSONOutput(hookSpecificOutput=specific)


def _mapping(value: Any) -> dict[str, JsonValue]:
    return {str(key): item for key, item in value.items()} if isinstance(value, Mapping) else {}


class GuardBridge:
    """One per session. `emit` puts an event on the session's queue; `blocked` returns a reason to deny everything."""

    def __init__(self, spec: SessionSpec, *, session_id: str, emit: Emit, blocked: Blocked) -> None:
        self._spec = spec
        self._session_id = session_id
        self._emit = emit
        self._blocked = blocked
        self._questions: dict[str, asyncio.Future[Mapping[str, str] | None]] = {}

    def hooks(self) -> dict[HookEvent, list[HookMatcher]]:
        hooks: dict[HookEvent, list[HookMatcher]] = {
            "PreToolUse": [
                HookMatcher(matcher=None, hooks=[self.pre_tool_use], timeout=self._spec.hook_timeout_s + HOOK_MARGIN_S)
            ]
        }
        if self._spec.context_provider is not None:
            hooks["UserPromptSubmit"] = [
                HookMatcher(matcher=None, hooks=[self.user_prompt_submit], timeout=CONTEXT_HOOK_TIMEOUT_S)
            ]
        return hooks

    # --- the decision -------------------------------------------------------------------------------------------
    async def decide(self, call: ToolCall) -> GuardVerdict:
        """The guard's verdict, or a deny on any error, a non-verdict, a timeout or a blocked session."""
        blocked = self._blocked()
        if blocked is not None:
            return GuardVerdict(allow=False, message=blocked, reason_code=blocked)
        try:
            async with asyncio.timeout(self._spec.hook_timeout_s):
                verdict = await self._spec.guard(call)
        except TimeoutError:
            log.warning("guard_timed_out", tool=call.tool_name, timeout_s=self._spec.hook_timeout_s)
            return GuardVerdict(allow=False, message=REASON_TIMEOUT, reason_code=REASON_TIMEOUT)
        except Exception as error:  # fail closed (CONVENTIONS §7)
            log.error("guard_failed", tool=call.tool_name, error_type=type(error).__name__)
            return GuardVerdict(allow=False, message=REASON_ERROR, reason_code=REASON_ERROR)
        checked: object = verdict  # the guard is caller code: trust its type only after checking it
        if not isinstance(checked, GuardVerdict):
            log.error("guard_failed", tool=call.tool_name, error_type=type(checked).__name__)
            return GuardVerdict(allow=False, message=REASON_ERROR, reason_code=REASON_ERROR)
        return checked

    def _denied(self, tool_name: str, verdict: GuardVerdict) -> str:
        reason = verdict.reason_code or REASON_DENIED
        self._emit(ToolDenied(tool_name=tool_name, reason_code=reason))
        log.info("tool_denied", tool=tool_name, reason=reason)
        return verdict.message or reason

    # --- PreToolUse ---------------------------------------------------------------------------------------------
    async def pre_tool_use(self, data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        del context
        tool_name = "unknown"
        try:
            payload: object = data
            raw: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
            tool_name = str(raw.get("tool_name") or "unknown")
            call = ToolCall(
                tool_name=tool_name,
                tool_input=_mapping(raw.get("tool_input")),
                tool_use_id=str(raw.get("tool_use_id") or tool_use_id or ""),
                agent_id=str(raw["agent_id"]) if raw.get("agent_id") else None,
                session_id=str(raw.get("session_id") or self._session_id),
            )
            verdict = await self.decide(call)
            if not verdict.allow:
                return deny_output(self._denied(tool_name, verdict))
            # AskUserQuestion must still reach can_use_tool (the answers come from there), so never pre-approve it.
            explicit = self._spec.permission_mode == "dontAsk" and tool_name != ASK_TOOL
            return allow_output(explicit=explicit)
        except Exception as error:  # fail closed: the hook never raises into the SDK
            log.error("guard_failed", tool=tool_name, error_type=type(error).__name__)
            try:
                self._emit(ToolDenied(tool_name=tool_name, reason_code=REASON_ERROR))
            except Exception as emit_error:
                log.error("guard_failed", tool=tool_name, error_type=type(emit_error).__name__)
            return deny_output(REASON_ERROR)

    # --- UserPromptSubmit: the per-turn context (SPEC §5.4) -----------------------------------------------------
    async def user_prompt_submit(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        del data, tool_use_id, context
        provider = self._spec.context_provider
        if provider is None:
            return SyncHookJSONOutput()
        try:
            text = provider()
        except Exception as error:  # a missing context must not block the turn; the frozen prompt still holds
            log.error("turn_context_failed", error_type=type(error).__name__)
            return SyncHookJSONOutput()
        if not text:
            return SyncHookJSONOutput()
        specific = UserPromptSubmitHookSpecificOutput(hookEventName="UserPromptSubmit", additionalContext=text)
        return SyncHookJSONOutput(hookSpecificOutput=specific)

    # --- can_use_tool -------------------------------------------------------------------------------------------
    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        try:
            if tool_name == ASK_TOOL:
                return await self._question(tool_input, context)
            call = ToolCall(
                tool_name=tool_name,
                tool_input=_mapping(tool_input),
                tool_use_id=str(context.tool_use_id or ""),
                agent_id=context.agent_id or None,
                session_id=self._session_id,
            )
            verdict = await self.decide(call)
            if verdict.allow:
                return PermissionResultAllow(updated_input=dict(tool_input))
            return PermissionResultDeny(message=self._denied(tool_name, verdict))
        except Exception as error:  # fail closed
            log.error("guard_failed", tool=tool_name, error_type=type(error).__name__)
            return PermissionResultDeny(message=REASON_ERROR)

    async def _question(self, tool_input: dict[str, Any], context: ToolPermissionContext) -> PermissionResult:
        blocked = self._blocked()
        if blocked is not None:
            return PermissionResultDeny(message=self._denied(ASK_TOOL, GuardVerdict(False, blocked, blocked)))
        if context.agent_id:
            verdict = GuardVerdict(False, SUBAGENT_QUESTION_MESSAGE, REASON_SUBAGENT_QUESTION)
            return PermissionResultDeny(message=self._denied(ASK_TOOL, verdict))
        question_id = str(context.tool_use_id or uuid.uuid4())
        raw_questions = tool_input.get("questions")
        questions = tuple(_mapping(question) for question in (raw_questions if isinstance(raw_questions, list) else ()))
        future: asyncio.Future[Mapping[str, str] | None] = asyncio.get_running_loop().create_future()
        self._questions[question_id] = future
        self._emit(QuestionAsked(question_id=question_id, questions=questions))
        log.info("question_asked", question_id=question_id, questions=len(questions))
        try:
            async with asyncio.timeout(self._spec.question_hold_s):
                answers = await future
        except TimeoutError:
            answers = None
        finally:
            self._questions.pop(question_id, None)
        if answers is None:
            verdict = GuardVerdict(False, self._spec.question_parked_message or REASON_PARKED, REASON_PARKED)
            return PermissionResultDeny(message=self._denied(ASK_TOOL, verdict))
        log.info("question_answered", question_id=question_id, answers=len(answers))
        return PermissionResultAllow(updated_input={**tool_input, "answers": dict(answers)})

    def answer(self, question_id: str, answers: Mapping[str, str]) -> bool:
        """Resolve a held question. False when its callback is gone (hold elapsed, answered, session closed)."""
        future = self._questions.get(question_id)
        if future is None or future.done():
            return False
        future.set_result(dict(answers))
        return True

    def pending_questions(self) -> tuple[str, ...]:
        return tuple(self._questions)

    def release_questions(self) -> None:
        """Session closing: every held question is parked (denied) instead of hanging."""
        for future in self._questions.values():
            if not future.done():
                future.set_result(None)
