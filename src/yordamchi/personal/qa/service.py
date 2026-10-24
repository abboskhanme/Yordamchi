"""The personal assistant (SPEC §3.2.13; UC-30, UC-36 fast path, Q4; T72).

`answer(deps, question)` is the default reply of the personal bot to a closed burst:
1. **Project status first** (UC-36): a plain "BonviHR qanday?" is answered from the dev digest through the read-only
   bridge, with its freshness line, without asking a model (well under 4 s).
2. **The conversation**: one per chat, one warm Claude session each (`assistant_sessions.session_id` is the CLI session
   id). After `qa.session_idle_rotate_h` idle hours or `qa.web_search_rotate_at` web searches a new one starts,
   seeded with the old one's five-line summary; "yangi suhbat" starts one without (`new_conversation`).
3. **The ask**: the frozen `prompts/personal/qa_system.md` (fixed parts only), and on every ask the owner-editable block
   `prompts/personal/qa_context.md` from this turn's settings (persona, verbosity, language, length and voice
   limits, his extra instructions, the time), so a change by chat applies to the next reply (SPEC §5.4, S4). Tools:
   WebSearch, WebFetch, the bridge's read-only status tools, plus whatever toolsets the caller adds this turn
   (`yordamchi_media` T73, `yordamchi_int` T76). What he forwarded goes in `<forwarded>` blocks: data, never commands.
4. **Streaming**: text deltas become `sendMessageDraft` updates in a private chat, at most every
   `qa.draft_interval_s`, never showing a marker or the voice script.
5. **The answer**: `domain.parse_answer` → the Telegram text within `text.max_chars` / `text.max_lines` (a shortened
   answer keeps its full version for "to'liqroq"), the voice script within `voice.max_words`. One unit of work stores
   the `answers` row, updates the conversation, queues the text and records the usage.
6. **Claude unavailable** (rate limit, login refused, CLI missing, broken or silent): the fallback model
   (`deps.fallback_assistant`, Gemini) answers, its text headed "(zaxira model) …" (Q4). A refused tool list
   (`PersonalToolsMismatch`, NFR-SEC-08) is never answered around: it propagates, and `guarded()` tells the owner.
7. **Voice**: once the text is queued, `voice.service.send_voice` (T71) turns the voice script into the voice note
   under the same ref, after the text; `answers.voice_status` records sent, skipped, failed or off.

Nothing here can write to dev.db or reach the dev executor: there is no such handle (C7, UC-36), and model output is
only ever text for the owner (it never changes a setting or approves anything).

Logs: ids, codes, counts, durations, models. Never the question, the answer or the instructions.
"""

import asyncio
import secrets
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact, timefmt
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, ExternalError, PersonalToolsMismatch, QaNothingMore, YordamchiError
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText, join
from yordamchi.persistence import bulk
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa import domain, store
from yordamchi.personal.qa.domain import Limits, ParsedAnswer, QaQuestion, RotateReason, VoiceStatus
from yordamchi.ports.assistant import AssistantDelta, AssistantReply, AssistantRequest, AssistantToolset
from yordamchi.shared.bridge import domain as bridge_domain
from yordamchi.shared.bridge import service as bridge
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.voice import service as voice
from yordamchi.shared.voice.domain import SkipReason, TtsResult

log = get_log(__name__)

KIND: Final = "qa"
SYSTEM_PROMPT: Final = "personal/qa_system"
FALLBACK_PROMPT: Final = "personal/qa_fallback_system"
CONTEXT_PROMPT: Final = "personal/qa_context"
EXTRA_PROMPT: Final = "personal/qa_context_extra"
PREVIOUS_PROMPT: Final = "personal/qa_context_previous"
REPLY_KIND: Final = Kind.RESULT
USAGE_SOURCE: Final = "qa"
BRIDGE_PROVIDER: Final = "bridge"
# The model is told a little less than the limit, so the lines Yordamchi adds (fallback header, freshness, hint) fit.
MODEL_MARGIN_CHARS: Final = 200
MODEL_MARGIN_LINES: Final = 3
TEXT_KEEP: Final = timedelta(days=30)  # SPEC §4.2 `answers`: text 30 d, row 400 d; sessions 90 d
ROW_KEEP: Final = timedelta(days=400)
SESSION_KEEP: Final = timedelta(days=90)
_PROVIDERS: Final = {"google": Provider.GOOGLE, "openrouter": Provider.OPENROUTER}


@dataclass(frozen=True, slots=True)
class QaOutcome:
    """What `answer` sent: the text is queued; the voice note (T71) follows it under the same `ref`."""

    answer_id: int
    text: BotText = field(repr=False)
    voice_script: str = field(repr=False)
    ref: str
    provider: str
    model: str
    fallback: bool = False
    compressed: bool = False
    tool_calls: tuple[str, ...] = ()
    first_text_ms: int | None = None
    total_ms: int = 0


@dataclass(frozen=True, slots=True)
class _Conversation:
    row_id: int
    session_id: str
    resume: bool
    seed: str | None = field(default=None, repr=False)  # the previous conversation's summary, first ask only
    summary: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class _Streamed:
    reply: AssistantReply | None = None
    error: ExternalError | None = None
    timed_out: bool = False
    first_text_ms: int | None = None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _limits(snap: Snapshot) -> Limits:
    return Limits(
        max_chars=snap[defaults.TEXT_MAX_CHARS],
        max_lines=snap[defaults.TEXT_MAX_LINES],
        voice_words=snap[defaults.VOICE_MAX_WORDS],
    )


# --- the per-turn instructions (SPEC §5.4) ---------------------------------------------------------------------------
def _instructions(snap: Snapshot, *, now: datetime, seed: str | None) -> str:
    """The owner-editable block, rendered from this turn's settings: it rides every ask."""
    zone = snap[defaults.UI_TIMEZONE]
    extra = snap[defaults.PROMPT_PERSONAL_EXTRA].strip()
    limits = _limits(snap)
    return prompts.render(
        CONTEXT_PROMPT,
        persona=snap[defaults.PERSONA_PERSONAL].strip(),
        verbosity=snap[defaults.PERSONA_PERSONAL_VERBOSITY],
        language=snap[defaults.LANG_REPLY],
        max_chars=str(max(200, limits.max_chars - MODEL_MARGIN_CHARS)),
        max_lines=str(max(3, limits.max_lines - MODEL_MARGIN_LINES)),
        voice_words=str(limits.voice_words),
        extra="\n" + prompts.render(EXTRA_PROMPT, extra=extra).rstrip() if extra else "",
        now=timefmt.local(now, zone).strftime("%Y-%m-%d %H:%M, %A"),
        timezone=zone,
        previous="\n" + prompts.render(PREVIOUS_PROMPT, summary=seed).rstrip() if seed else "",
    ).strip()


def _toolsets(
    deps: PersonalDeps, snap: Snapshot, tool_log: bridge.ToolLog, extra: Sequence[AssistantToolset]
) -> tuple[AssistantToolset, ...]:
    """The bridge's read-only tools when dev.db is reachable, then the caller's (`yordamchi_media`, `yordamchi_int`)."""
    found: list[AssistantToolset] = []
    if deps.peer_db is not None:
        found.append(bridge.toolset(deps, timeout_s=snap[defaults.TIMEOUTS_BRIDGE_S], tool_log=tool_log))
    found.extend(extra)
    return tuple(found)


def _request(
    snap: Snapshot,
    question: QaQuestion,
    conversation: _Conversation,
    *,
    now: datetime,
    images: Sequence[Path],
    toolsets: tuple[AssistantToolset, ...],
) -> AssistantRequest:
    return AssistantRequest(
        kind=KIND,
        system_prompt=prompts.render(SYSTEM_PROMPT),
        prompt=domain.prompt_of(question),
        model=snap[defaults.MODELS_QA],
        settings_version=snap.version,
        effort=snap[defaults.MODELS_QA_EFFORT],
        images=tuple(images),
        conversation=conversation.session_id,
        instructions=_instructions(snap, now=now, seed=conversation.seed),
        prompt_version=snap.version,
        resume=conversation.resume,
        toolsets=toolsets,
        max_web_searches=snap[defaults.QA_WEB_SEARCHES_MAX],
    )


# --- conversations ---------------------------------------------------------------------------------------------------
async def _end_session(deps: PersonalDeps, snap: Snapshot, session_id: str) -> None:
    """Close the warm session of an ended conversation (a port call: never inside a unit of work)."""
    try:
        await deps.assistant.reset(KIND, conversation=session_id, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    except ExternalError as error:
        log.warning("qa_reset_failed", code=error.code.value)


async def _conversation(deps: PersonalDeps, snap: Snapshot, chat_id: int, *, now: datetime) -> _Conversation:
    """This chat's live conversation, or a new one when there is none or the old one is due to rotate."""
    async with deps.db.read() as s:
        row = await store.find_live_session(s, chat_id)
    reason: RotateReason | None = None
    if row is not None:
        reason = domain.rotation_due(
            row.last_active_at,
            row.websearch_count,
            now=now,
            idle_rotate_h=snap[defaults.QA_SESSION_IDLE_ROTATE_H],
            rotate_at=snap[defaults.QA_WEB_SEARCH_ROTATE_AT],
        )
        if reason is None:
            seed = row.seed if row.answers_count == 0 else None
            return _Conversation(row.id, row.session_id, resume=row.answers_count > 0, seed=seed, summary=row.summary)
        await _end_session(deps, snap, row.session_id)
    seed = row.summary if row is not None else None
    return await _start(
        deps, snap, chat_id, now=now, previous=row.id if row is not None else None, reason=reason, seed=seed
    )


async def _start(
    deps: PersonalDeps,
    snap: Snapshot,
    chat_id: int,
    *,
    now: datetime,
    previous: int | None,
    reason: RotateReason | None,
    seed: str | None,
) -> _Conversation:
    session_id = str(uuid.uuid4())
    async with deps.db.uow() as s:
        if previous is not None and reason is not None:
            await store.mark_session_closed(s, previous, at=now, reason=reason.value)
        row = await store.add_session(
            s, chat_id=chat_id, session_id=session_id, at=now, seed=seed, prompt_version=snap.version
        )
    log.info("qa_conversation_started", chat_id=chat_id, reason=reason.value if reason else "first", seeded=bool(seed))
    return _Conversation(row.id, session_id, resume=False, seed=seed)


async def new_conversation(deps: PersonalDeps, chat_id: int) -> BotText:
    """ "yangi suhbat": end this chat's conversation and start a fresh one, with no summary of the old (it resets)."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.read() as s:
        row = await store.find_live_session(s, chat_id)
    if row is not None:
        await _end_session(deps, snap, row.session_id)
    await _start(
        deps,
        snap,
        chat_id,
        now=now,
        previous=row.id if row is not None else None,
        reason=RotateReason.OWNER if row is not None else None,
        seed=None,
    )
    text = S.QA_NEW_CONVERSATION()
    await _queue(deps, snap, chat_id, text, ref=f"qa-new-{secrets.token_hex(6)}")
    return text


async def warm_conversation(deps: PersonalDeps, chat_id: int) -> None:
    """Start the warm session of this chat's conversation before its next question (startup, after "yangi suhbat").
    Best effort: a failure only means the next ask starts cold."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    conversation = await _conversation(deps, snap, chat_id, now=now)
    question = QaQuestion(chat_id=chat_id, own_text="")
    toolsets = _toolsets(deps, snap, bridge.ToolLog(), ())
    request = _request(snap, question, conversation, now=now, images=(), toolsets=toolsets)
    try:
        await deps.assistant.warm(request.profile, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    except YordamchiError as error:
        log.warning("qa_warm_failed", code=error.code.value)


# --- streaming drafts ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Drafts:
    """`sendMessageDraft` while the answer streams: private chats only, at most every `qa.draft_interval_s`, and
    never past a marker. A failing draft switches drafts off for this answer; the answer itself is unaffected."""

    deps: PersonalDeps
    chat_id: int
    draft_id: int
    every_s: float
    timeout_s: float
    max_chars: int
    last: float = float("-inf")
    shown: str = ""
    off: bool = False
    sent: int = 0

    async def offer(self, partial: str) -> None:
        if self.off or self.chat_id <= 0 or time.monotonic() - self.last < self.every_s:
            return
        text = domain.draft_text(partial, self.max_chars)
        if not text or text == self.shown:
            return
        self.last = time.monotonic()
        try:
            await self.deps.messenger.send_draft(
                self.chat_id, self.draft_id, redact.scrubbed(text), timeout=self.timeout_s
            )
        except ExternalError as error:
            self.off = True
            log.info("qa_draft_failed", code=error.code.value)
            return
        self.shown = text
        self.sent += 1


async def _stream(deps: PersonalDeps, snap: Snapshot, request: AssistantRequest, drafts: _Drafts) -> _Streamed:
    started = time.monotonic()
    out = _Streamed()
    partial: list[str] = []
    try:
        async with asyncio.timeout(snap[defaults.TIMEOUTS_QA_S]):
            async for item in deps.assistant.stream(request, idle_timeout=snap[defaults.TIMEOUTS_QA_IDLE_S]):
                if isinstance(item, AssistantDelta):
                    if out.first_text_ms is None and item.text.strip():
                        out.first_text_ms = _ms(started)
                    partial.append(item.text)
                    await drafts.offer("".join(partial))
                else:
                    out.reply = item
    except TimeoutError:
        out.timed_out = True
    except PersonalToolsMismatch:
        raise
    except ExternalError as error:
        out.error = error
    return out


# --- the fallback model (Q4) -----------------------------------------------------------------------------------------
async def _fallback(
    deps: PersonalDeps, snap: Snapshot, question: QaQuestion, *, now: datetime
) -> AssistantReply | None:
    assistant = deps.fallback_assistant
    if assistant is None:
        return None
    request = AssistantRequest(
        kind=KIND,
        system_prompt=prompts.render(FALLBACK_PROMPT),
        prompt=domain.prompt_of(question),
        model=snap[defaults.MODELS_FALLBACK_TEXT],
        settings_version=snap.version,
        instructions=_instructions(snap, now=now, seed=None),
        prompt_version=snap.version,
    )
    try:
        return await assistant.ask(request, timeout=snap[defaults.TIMEOUTS_QA_FALLBACK_S])
    except ExternalError as error:
        log.warning("qa_fallback_failed", code=error.code.value)
        return None


def _fallback_header(code: str) -> BotText:
    reason = S.QA_FALLBACK_REASONS.get(code) or S.QA_FALLBACK_REASON_OTHER
    return S.QA_FALLBACK_HEADER(reason=reason())


# --- delivery --------------------------------------------------------------------------------------------------------
async def _queue(deps: PersonalDeps, snap: Snapshot, chat_id: int, text: BotText, *, ref: str) -> None:
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=REPLY_KIND,
            ref=ref,
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )


def _ref(question: QaQuestion) -> str:
    return f"qa-{question.burst_id}" if question.burst_id is not None else f"qa-x{secrets.token_hex(6)}"


def _usage(reply: AssistantReply, *, fallback: bool, duration_ms: int) -> Usage:
    provider = _PROVIDERS.get(reply.provider, Provider.GOOGLE) if fallback else Provider.ANTHROPIC_PLAN
    units = {name: count for name, count in reply.usage.items() if count >= 0}
    return Usage(
        source=USAGE_SOURCE,
        provider=provider,
        model=reply.model or "unknown",
        is_paid_api=fallback,  # an API key bills (or counts against a free tier); the Claude plan never does
        cost_usd_micros=reply.cost_usd_micros,
        duration_ms=duration_ms,
        units=units,
    )


@dataclass(frozen=True, slots=True)
class _Delivery:
    text: BotText
    parsed: ParsedAnswer
    provider: str
    model: str
    fallback: bool
    first_text_ms: int | None
    total_ms: int
    reply: AssistantReply | None = None


async def _deliver(
    deps: PersonalDeps,
    snap: Snapshot,
    question: QaQuestion,
    delivery: _Delivery,
    conversation: _Conversation | None,
) -> QaOutcome:
    """One unit of work: the `answers` row, the conversation's counters and summary, the text, the usage row."""
    now = deps.clock.now()
    ref = _ref(question)
    voice = VoiceStatus.PENDING if snap[defaults.VOICE_ENABLED] and delivery.parsed.voice else VoiceStatus.OFF
    reply = delivery.reply
    full = redact.scrubbed(delivery.parsed.full) if delivery.parsed.full else None
    asked = question.own_text or " ".join(question.forwarded)
    async with deps.db.uow() as s:
        row = await store.add_answer(
            s,
            session_row_id=conversation.row_id if conversation is not None else None,
            chat_id=question.chat_id,
            burst_id=question.burst_id,
            text_full=full,
            provider=delivery.provider,
            model=delivery.model,
            voice_status=voice.value,
            first_text_ms=delivery.first_text_ms,
            total_ms=delivery.total_ms,
            fallback=delivery.fallback,
            at=now,
        )
        if conversation is not None and not delivery.fallback:
            line = domain.exchange_line(asked, delivery.parsed.text)
            await store.mark_session_answered(
                s,
                conversation.row_id,
                at=now,
                web_searches=reply.web_searches if reply is not None else 0,
                prompt_version=snap.version,
                summary=redact.scrubbed(domain.rolled_summary(conversation.summary, line)),
            )
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=question.chat_id,
            text=delivery.text,
            kind=REPLY_KIND,
            ref=ref,
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
        if reply is not None:
            await cost.record_usage(
                s, bot=deps.bot, at=now, usage=_usage(reply, fallback=delivery.fallback, duration_ms=delivery.total_ms)
            )
    log.info(
        "qa_answered",
        answer_id=row.id,
        provider=delivery.provider,
        model=delivery.model,
        fallback=delivery.fallback,
        compressed=delivery.parsed.compressed,
        cut=delivery.parsed.cut,
        first_text_ms=delivery.first_text_ms,
        total_ms=delivery.total_ms,
        tools=len(reply.tool_calls) if reply is not None else 0,
        web_searches=reply.web_searches if reply is not None else 0,
        cost_usd_micros=reply.cost_usd_micros if reply is not None else 0,
    )
    return QaOutcome(
        answer_id=row.id,
        text=delivery.text,
        voice_script=delivery.parsed.voice,
        ref=ref,
        provider=delivery.provider,
        model=delivery.model,
        fallback=delivery.fallback,
        compressed=delivery.parsed.compressed,
        tool_calls=reply.tool_calls if reply is not None else (),
        first_text_ms=delivery.first_text_ms,
        total_ms=delivery.total_ms,
    )


def _compose(
    raw: str, snap: Snapshot, *, now: datetime, header: BotText | None, tool_log: bridge.ToolLog
) -> tuple[BotText, ParsedAnswer]:
    """The text the owner reads: the optional fallback header, the answer, the freshness line when project data was
    used, and the "to'liqroq" hint when the answer was shortened."""
    freshness = bridge_domain.freshness_line(tool_log.oldest, now=now) if tool_log.read else None
    added = [line for line in (header, freshness, S.QA_MORE_HINT()) if line is not None]
    reserve_chars, reserve_lines = domain.reserve_for(added)
    parsed = domain.parse_answer(raw, _limits(snap), reserve_chars=reserve_chars, reserve_lines=reserve_lines)
    if not parsed.text.strip():
        return S.QA_EMPTY_ANSWER(), parsed
    lines: list[BotText] = [] if header is None else [header]
    lines.append(redact.scrubbed(parsed.text))
    if freshness is not None:
        lines.append(freshness)
    if parsed.compressed:
        lines.append(S.QA_MORE_HINT())
    return join(lines), parsed


# --- the use cases ---------------------------------------------------------------------------------------------------
async def answer(
    deps: PersonalDeps,
    question: QaQuestion,
    *,
    images: Sequence[Path] = (),
    toolsets: Sequence[AssistantToolset] = (),
) -> QaOutcome | None:
    """The personal bot's reply to one closed burst (module docstring): the text is queued first, then its voice note
    follows under the same ref (`voice.service.send_voice`, T71; the text never waits for it). None when nothing was
    answered: the burst was answered before (a rerun after a crash), it was empty (the owner is told), or the answer
    timed out with no fallback (the owner is told). `toolsets` are this turn's extra in-process tools (T76
    integrations; media briefs from T73 travel in `question.material`)."""
    outcome = await _answer(deps, question, images=images, toolsets=toolsets)
    if outcome is not None and outcome.voice_script:
        await _speak(deps, question, outcome)
    return outcome


def _voice_status(result: TtsResult) -> tuple[VoiceStatus, str | None]:
    if result.ok:
        return VoiceStatus.SENT, None
    reason = result.reason
    if reason is SkipReason.DISABLED:
        return VoiceStatus.OFF, None
    code = reason.value if reason is not None else None
    if reason in {SkipReason.EMPTY, SkipReason.NO_PROVIDER, SkipReason.SUMMARY}:
        return VoiceStatus.SKIPPED, code
    return VoiceStatus.FAILED, code


async def _speak(deps: PersonalDeps, question: QaQuestion, outcome: QaOutcome) -> None:
    """The voice note after the text (SPEC §3.2.13 step 6): `voice.max_words`, the provider chain and the notices are
    `shared/voice`'s; the answer row records what became of it."""
    result = await voice.send_voice(deps, chat_id=question.chat_id, script=outcome.voice_script, ref=outcome.ref)
    status, code = _voice_status(result)
    await record_voice(deps, outcome.answer_id, status=status, error_code=code, voice_ms=result.latency_ms or None)


async def _answer(
    deps: PersonalDeps,
    question: QaQuestion,
    *,
    images: Sequence[Path],
    toolsets: Sequence[AssistantToolset],
) -> QaOutcome | None:
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    if question.burst_id is not None:
        async with deps.db.read() as s:
            if await store.find_answer_by_burst(s, question.burst_id) is not None:
                log.info("qa_already_answered", burst_id=question.burst_id)
                return None
    if question.empty and not images:
        await _queue(deps, snap, question.chat_id, S.QA_EMPTY_QUESTION(), ref=_ref(question))
        return None
    status = None
    if not question.forwarded and not question.material and not images:
        status = await bridge.status_reply(
            deps, question.own_text, timezone=snap[defaults.UI_TIMEZONE], timeout=snap[defaults.TIMEOUTS_BRIDGE_S]
        )
    if status is not None:
        parsed = ParsedAnswer(text=status, voice=domain.voice_script(status, snap[defaults.VOICE_MAX_WORDS]))
        delivery = _Delivery(status, parsed, BRIDGE_PROVIDER, BRIDGE_PROVIDER, False, _ms(started), _ms(started))
        return await _deliver(deps, snap, question, delivery, None)

    conversation = await _conversation(deps, snap, question.chat_id, now=now)
    tool_log = bridge.ToolLog()
    request = _request(
        snap,
        question,
        conversation,
        now=now,
        images=images,
        toolsets=_toolsets(deps, snap, tool_log, toolsets),
    )
    drafts = _Drafts(
        deps,
        question.chat_id,
        draft_id=question.burst_id or secrets.randbelow(2**31 - 2) + 1,
        every_s=snap[defaults.QA_DRAFT_INTERVAL_S],
        timeout_s=snap[defaults.TIMEOUTS_QA_DRAFT_S],
        max_chars=snap[defaults.TEXT_MAX_CHARS],
    )
    streamed = await _stream(deps, snap, request, drafts)
    reply, fallback, header = streamed.reply, False, None
    if reply is None:
        code = streamed.error.code if streamed.error is not None else ErrorCode.CALL_TIMED_OUT
        if not domain.falls_back(code) and streamed.error is not None:
            raise streamed.error
        log.warning("qa_claude_unavailable", code=code.value, drafts=drafts.sent)
        reply = await _fallback(deps, snap, question, now=deps.clock.now())
        if reply is None:
            if streamed.error is not None:
                raise streamed.error  # guarded() tells the owner Claude's own error
            await _queue(
                deps, snap, question.chat_id, S.ERR_CALL_TIMED_OUT(operation=S.QA_OPERATION()), ref=_ref(question)
            )
            return None
        fallback, header = True, _fallback_header(code.value)
    text, parsed = _compose(reply.text, snap, now=deps.clock.now(), header=header, tool_log=tool_log)
    delivery = _Delivery(
        text=text,
        parsed=parsed,
        provider=reply.provider,
        model=reply.model,
        fallback=fallback,
        first_text_ms=streamed.first_text_ms,
        total_ms=_ms(started),
        reply=reply,
    )
    return await _deliver(deps, snap, question, delivery, conversation)


async def more(deps: PersonalDeps, chat_id: int) -> BotText:
    """ "to'liqroq": the full version of this chat's last shortened answer (split, or a `.md` file when long)."""
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        last = await store.find_latest_answer(s, chat_id)
    if last is None or not last.text_full:
        raise QaNothingMore()
    text = redact.scrubbed(last.text_full)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await store.mark_answer_expanded(s, last.id, at=now)
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=REPLY_KIND,
            ref=f"qa-full-{last.id}",
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
    log.info("qa_full_sent", answer_id=last.id, chars=len(text))
    return text


async def record_voice(
    deps: PersonalDeps,
    answer_id: int,
    *,
    status: VoiceStatus,
    error_code: str | None = None,
    voice_ms: int | None = None,
) -> None:
    """The voice step's outcome for one answer (T71 reports sent, skipped or failed)."""
    async with deps.db.uow() as s:
        await store.set_answer_voice(s, answer_id, status=status.value, error_code=error_code, voice_ms=voice_ms)


async def apply_retention(deps: PersonalDeps) -> int:
    """SPEC §4.2: answer texts after 30 days, answer rows after 400, closed conversations' texts after 90 (the row
    goes once no answer points at it). Returns how many rows were touched."""
    now = deps.clock.now()
    touched = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.clear_answer_texts_before(s, now - TEXT_KEEP, limit=limit)
    )
    touched += await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_answers_before(s, now - ROW_KEEP, limit=limit)
    )
    touched += await bulk.run_in_batches(
        deps.db, lambda s, limit: store.clear_session_texts_before(s, now - SESSION_KEEP, limit=limit)
    )
    touched += await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_sessions_closed_before(s, now - SESSION_KEEP, limit=limit)
    )
    log.info("qa_retention_applied", rows=touched)
    return touched
