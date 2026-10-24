"""Chat: the dev group's plain answers (owner, 2026-10-24; SPEC §3.2.6 `chat`; the rules are in `domain`).

`answer_chat(deps, item)` runs once routing recorded a burst as `chat` (a `route_decisions` row; no turn, no
delivery). The router (`bots/dev/pipeline.py`) starts it as a background job: an answer takes seconds on the chat
model, and a "to'xta" must never wait behind it. It asks the warm `chat` Assistant outside any transaction (A3),
bounded by `timeouts.chat_s`, then queues the answer and its usage row in one short unit of work: a `report`-kind
message at priority 0 (an answer the owner waits for), sent as a reply to his message. That message also ends the
"yozmoqda…" hint intake started, so the hint shows until the answer is out. A failure or a timeout queues the one line
`S.CHAT_FAILED`; nothing is retried here (adapters retry, CONVENTIONS §7).

Logs carry ids, codes, counts and durations, never the question or the answer.
"""

import time
from dataclasses import dataclass
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.chat import domain
from yordamchi.dev.chat.domain import ChatSettings
from yordamchi.dev.deps import DevDeps
from yordamchi.ports.assistant import AssistantReply, AssistantRequest
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.cost import domain as cost_domain
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

log = get_log(__name__)

EMPTY: Final = "empty"  # `ChatAnswer.error_code` of a reply with no text
_PROVIDERS: Final = {"claude": Provider.ANTHROPIC_PLAN, "gemini": Provider.GOOGLE, "openrouter": Provider.OPENROUTER}


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    """What `answer_chat` queued. `provider` None: no answer came, the owner got `S.CHAT_FAILED`."""

    text: BotText
    provider: str | None = None
    error_code: str | None = None  # why no answer came: the `ExternalError` code, or `EMPTY`
    duration_ms: int = 0


def _request(cfg: ChatSettings, *, settings_version: int, prompt: str) -> AssistantRequest:
    """Every ask, and the warm-up, share one profile (system prompt, model, effort, no schema, no tools)."""
    return AssistantRequest(
        kind="chat",
        system_prompt=prompts.render(domain.SYSTEM_PROMPT),
        prompt=prompt,
        model=cfg.model,
        settings_version=settings_version,
        effort=cfg.effort,
        instructions=prompts.render(domain.CONTEXT_PROMPT, **domain.context_slots(cfg)),
        prompt_version=settings_version,
    )


async def warm_chat(deps: DevDeps) -> None:
    """Start the warm chat client (startup, after a settings change; `bots/dev/runtime.py`) with exactly the profile
    every ask uses, so the first answer is warm."""
    snap = await settings.snapshot(deps)
    request = _request(domain.chat_settings(snap), settings_version=snap.version, prompt="")
    try:
        await deps.assistant.warm(request.profile, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    except ExternalError as error:  # the first ask then starts it cold; it still answers within `timeouts.chat_s`
        log.warning("chat_warm_failed", error_code=error.code.value)


def _usage(reply: AssistantReply) -> Usage | None:
    """The ask as a `usage_records` row; None for an unknown provider or a reply that cannot be recorded."""
    provider = _PROVIDERS.get(reply.provider)
    if provider is None:
        return None
    usage = Usage(
        source=domain.SOURCE,
        provider=provider,
        model=reply.model,
        is_paid_api=provider in cost_domain.PAYABLE and reply.cost_usd_micros > 0,
        cost_usd_micros=reply.cost_usd_micros,
        duration_ms=reply.duration_ms,
        units=dict(reply.usage),
        project_id=None,
    )
    return None if cost_domain.problems(usage) else usage


async def answer_chat(deps: DevDeps, item: TurnInput) -> ChatAnswer:
    """Answer one `chat` burst in the dev group: the chat Assistant's reply, or `S.CHAT_FAILED`. Queued once per
    burst (`chat-<burst_id>`), as a reply to the owner's last message."""
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    cfg = domain.chat_settings(snap)
    prompt = domain.chat_prompt(item, now=deps.clock.now(), timezone=cfg.timezone, scrub=redact.scrubbed)
    reply: AssistantReply | None = None
    error_code: str | None = None
    try:
        reply = await deps.assistant.ask(
            _request(cfg, settings_version=snap.version, prompt=prompt), timeout=cfg.timeout_s
        )
    except ExternalError as error:
        error_code = error.code.value
        log.warning("chat_answer_failed", burst_id=item.burst_id, error_code=error_code)
    answer = domain.answer_text(reply.text, redact.scrubbed) if reply is not None else None
    if reply is not None and answer is None:
        error_code = EMPTY
        log.warning("chat_answer_empty", burst_id=item.burst_id)
    answered = answer is not None
    text = answer if answer is not None else S.CHAT_FAILED()
    usage = _usage(reply) if reply is not None else None
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=item.chat_id,
            text=text,
            kind=Kind.REPORT,
            ref=f"chat-{item.burst_id}",
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            priority=outbox_domain.HIGHEST_PRIORITY,
            reply_to=item.own_parts[-1].message_id if item.own_parts else None,
        )
        if usage is not None:
            await cost.record_usage(s, bot=deps.bot, at=now, usage=usage)
    duration_ms = round((time.monotonic() - started) * 1000)
    log.info(
        "chat_answered",
        burst_id=item.burst_id,
        answered=answered,
        provider=reply.provider if reply is not None else None,
        error_code=error_code,
        chars=len(text),
        duration_ms=duration_ms,
    )
    return ChatAnswer(
        text=text,
        provider=reply.provider if reply is not None and answered else None,
        error_code=error_code,
        duration_ms=duration_ms,
    )
