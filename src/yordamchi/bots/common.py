"""The owner-only edge and the loop around it, shared by both bots (SPEC §2.8, §3.2.1; UC-01; CONVENTIONS §3, §6, §7).

- `OwnerOnly`: the outer middleware on `dp.update`. It turns the update into the ids `shared/access` judges, and only
  an admitted update reaches a handler, with its `OwnerOrigin` in `data["origin"]`. A dropped update gets nothing at
  all; a foreign chat is left. aiogram's own `ErrorsMiddleware` and `UserContextMiddleware` sit before it (they only
  catch errors and read ids); nothing of ours runs before it.
- `guarded()`: wraps every turn and job. A failure is logged and told to the owner, never raised (one of the three
  allowed `except Exception` places, CONVENTIONS §7).
- `log_update_error`: the `@dp.errors()` handler for whatever escapes; it logs and returns True.
- `run_polling()`: long polling, each update fed in order to `dp.feed_update`, the offset advanced through the inbox
  after each batch, and a backoff that resumes within 60 s of connectivity (NFR-REL-03). Every attempt reaches the
  heartbeat hook, a failure with its kind (`poll_error_kind`), so the watchdog can tell offline from dead (T32b).
- `build_dispatcher()`: puts the three together; `bots/<bot>/app.py` passes its routers.
"""

import asyncio
import functools
import math
import random
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, NoReturn

from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.methods import GetUpdates
from aiogram.types import ChatMemberRestricted, ErrorEvent, TelegramObject, Update
from aiogram.types.update import UpdateTypeLookupError

from yordamchi.adapters.telegram.translate import translate
from yordamchi.core import strings as S
from yordamchi.core.aio import raise_if_cancelling
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    ProviderUnavailable,
    RateLimited,
    YordamchiError,
)
from yordamchi.core.log import LogValue, get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.shared.access import domain as access_domain
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import Admit, Arrival, ChatKind, EventType
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health.domain import PollErrorKind
from yordamchi.shared.inbox import service as inbox

log = get_log(__name__)

# The key under which an admitted update's `OwnerOrigin` reaches the handler: `async def on_message(message: Message,
# origin: OwnerOrigin) -> None`.
ORIGIN_KEY: Final = "origin"
# Bound on telling the owner that a turn or job failed. Mechanism, not behaviour, like the adapter's own retries.
NOTICE_TIMEOUT_S: Final = 5.0


# --- guarded() ------------------------------------------------------------------------------------------------------
async def guarded[T](deps: SharedDeps, work: Awaitable[T], *, job: str, chat_id: int | None = None) -> T | None:
    """Await `work`; on failure log it, tell the owner, and return None. It never raises (cancellation excepted).

    A `YordamchiError` is expected: a warning with its code, and the owner gets `S.ERRORS[code](**params)`. Anything
    else is a bug: an error with the scrubbed traceback (`core.log` scrubs it) and an incident id, and the owner gets
    `S.ERR_INTERNAL(incident=…)` to quote. The notice goes to `chat_id`, or to this bot's home chat (the dev group, or
    the owner's DM) when the job has none.
    """
    notice: Callable[[], BotText]
    try:
        return await work
    except YordamchiError as error:
        raise_if_cancelling()
        log.warning("job_failed", bot=deps.bot.value, job=job, error_code=error.code.value)
        notice = functools.partial(_expected_notice, error)
    except Exception as error:
        raise_if_cancelling()
        incident = secrets.token_hex(4)
        log.exception("job_crashed", bot=deps.bot.value, job=job, incident=incident, error_type=type(error).__name__)
        notice = functools.partial(S.ERR_INTERNAL, incident=incident)
    await _tell_owner(deps, notice, job=job, chat_id=chat_id)
    return None


def _expected_notice(error: YordamchiError) -> BotText:
    return S.ERRORS[error.code](**error.params)


async def _tell_owner(deps: SharedDeps, notice: Callable[[], BotText], *, job: str, chat_id: int | None) -> None:
    """Best effort: the notice is built and sent in here, so not even a catalog bug escapes `guarded()`."""
    try:
        target = chat_id if chat_id is not None else await access.resolve_home_chat(deps)
        await deps.messenger.send_text(target, notice(), timeout=NOTICE_TIMEOUT_S)
    except Exception as error:
        raise_if_cancelling()
        log.warning("owner_notice_failed", bot=deps.bot.value, job=job, error_type=type(error).__name__)


# --- OwnerOnly ------------------------------------------------------------------------------------------------------
_EVENT_TYPES: Final = {event.value: event for event in EventType if event is not EventType.OTHER}
_CHAT_KINDS: Final = {kind.value: kind for kind in ChatKind}


def arrival_of(update: Update, bot: BotName) -> Arrival:
    """The ids `shared/access` judges, read off an aiogram `Update`. No text, caption or file is copied."""
    context = UserContextMiddleware.resolve_event_context(event=update)
    try:
        event = _EVENT_TYPES.get(update.event_type, EventType.OTHER)
    except UpdateTypeLookupError:  # a Bot API update type newer than this aiogram
        event = EventType.OTHER
    bot_present = True
    if (change := update.my_chat_member) is not None:
        member = change.new_chat_member
        restricted_member = isinstance(member, ChatMemberRestricted) and member.is_member
        bot_present = access_domain.is_present(member.status, is_member=restricted_member)
    return Arrival(
        bot=bot,
        update_id=update.update_id,
        event=event,
        user_id=context.user_id,
        chat_id=context.chat_id,
        chat_kind=_CHAT_KINDS.get(context.chat.type) if context.chat is not None else None,
        bot_present=bot_present,
    )


class OwnerOnly(BaseMiddleware):
    """UC-01 at the edge: only the owner's updates in this bot's chats reach a handler (policy: `shared/access`).

    An admitted update runs its handler under `guarded()`, so a failed intake is told to the owner in that chat.
    """

    def __init__(self, deps: SharedDeps) -> None:
        self._deps = deps

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object],
    ) -> object:
        if not isinstance(event, Update):
            msg = f"OwnerOnly belongs on dp.update, not on {type(event).__name__}"
            raise TypeError(msg)
        verdict = await access.admit_update(self._deps, arrival_of(event, self._deps.bot))
        if not isinstance(verdict, Admit):
            return UNHANDLED
        data[ORIGIN_KEY] = verdict.origin
        return await guarded(self._deps, handler(event, data), job="owner_update", chat_id=verdict.origin.chat_id)


async def log_update_error(event: ErrorEvent) -> bool:
    """`@dp.errors()`: log what escaped (the access check itself, say) and report it handled, so polling goes on.

    aiogram calls it inside `ErrorsMiddleware`'s `except` block, so the traceback is the current exception's.
    """
    log.exception(  # noqa: LOG004  # runs inside ErrorsMiddleware's `except` block (see above)
        "update_failed", update_id=event.update.update_id, error_type=type(event.exception).__name__
    )
    return True


def build_dispatcher(deps: SharedDeps, *routers: Router) -> Dispatcher:
    """A dispatcher with `OwnerOnly` before every handler and `log_update_error` for what escapes. No FSM: the
    conversation state lives in the DB, never in aiogram."""
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(OwnerOnly(deps))
    dispatcher.errors.register(log_update_error)
    dispatcher.include_routers(*routers)
    return dispatcher


# --- the polling runner ---------------------------------------------------------------------------------------------
ALLOWED_UPDATES: Final = ("message", "edited_message", "my_chat_member")  # SPEC §3.2.1 step 1


@dataclass(frozen=True, slots=True)
class PollConfig:
    """Mechanism, not behaviour (like `core.aio.forever`'s backoff): SPEC §2.8 and NFR-REL-03 fix these values, and the
    watchdog's 120 s heartbeat limit relies on them.

    Why `resumes ≤ 60 s` holds: after a failure the next requests are short probes (`timeout=0`) bounded by
    `probe_budget_s`, so a dead connection costs ≤ 10 s, then ≤ 30 s of backoff: ≤ 40 s plus one round trip.
    """

    long_poll_s: int = 25  # getUpdates `timeout` while healthy
    request_grace_s: float = 10.0  # HTTP budget beyond the long poll
    probe_budget_s: float = 10.0  # HTTP budget of a probe after a failure
    initial_backoff_s: float = 1.0
    max_backoff_s: float = 30.0
    jitter: float = 0.2  # each wait is scaled into [1 - jitter, 1], so it never exceeds max_backoff_s
    allowed_updates: tuple[str, ...] = ALLOWED_UPDATES


DEFAULT_POLL: Final = PollConfig()


@dataclass(frozen=True, slots=True)
class PollTick:
    """One `getUpdates` attempt, for the heartbeat writer (T32): `at` is its `last_poll_attempt_at`, and also its
    `last_poll_ok_at` when `error` is NONE. A failed attempt has `updates=0` and its kind (T32b)."""

    at: datetime
    updates: int
    error: PollErrorKind = PollErrorKind.NONE


type Heartbeat = Callable[[PollTick], Awaitable[None]]

_rng = random.SystemRandom()


def backoff_s(failures: int, config: PollConfig, *, spread: float = 1.0) -> float:
    """The wait after `failures` failed `getUpdates` in a row: 1 → 2 → 4 … ≤ 30 s, scaled by `spread` (≤ 1)."""
    doublings = min(max(failures - 1, 0), 32)  # bounded: a days-long outage must not overflow the float
    ceiling = min(config.max_backoff_s, config.initial_backoff_s * 2.0**doublings)
    return ceiling * spread


def _wait_after(failures: int, error: ExternalError, config: PollConfig) -> float:
    wait = backoff_s(failures, config, spread=_rng.uniform(1 - config.jitter, 1.0))
    if isinstance(error, RateLimited) and error.retry_after is not None:
        wait = max(wait, error.retry_after)  # asking again sooner only earns another 429
    return wait


def poll_error_kind(error: ExternalError) -> PollErrorKind:
    """NETWORK when only the connection failed: DNS, connect or a timeout (`CallTimedOut`), or a 5xx or garbage from
    Telegram or a proxy (the adapter's retryable `ProviderUnavailable`: `network_error`, `server_error`,
    `malformed_response`). Waiting fixes those, so the watchdog must not restart the bot for them. OTHER for the
    rest: a revoked token, a 409 conflict, a 429, a refused request."""
    if isinstance(error, CallTimedOut) or (isinstance(error, ProviderUnavailable) and error.retryable):
        return PollErrorKind.NETWORK
    return PollErrorKind.OTHER


@dataclass(frozen=True, slots=True)
class _PollFailed:
    error: ExternalError
    reason: str


async def _get_updates(bot: Bot, *, offset: int, config: PollConfig, recovering: bool) -> list[Update] | _PollFailed:
    """One bounded `getUpdates`. A failure comes back translated (no vendor text, no URL with the token)."""
    budget = config.probe_budget_s if recovering else config.long_poll_s + config.request_grace_s
    method = GetUpdates(
        offset=offset or None,
        timeout=0 if recovering else config.long_poll_s,
        allowed_updates=list(config.allowed_updates),
    )
    try:
        async with asyncio.timeout(budget):
            return await bot(method, request_timeout=math.ceil(budget))
    except TimeoutError:
        return _PollFailed(CallTimedOut(operation="getUpdates"), "request_timeout")
    except (TelegramAPIError, ClientDecodeError) as exc:
        translated = translate(exc, operation="getUpdates")
        return _PollFailed(translated.error, translated.reason)


def _log_failure(bot: BotName, failed: _PollFailed, *, failures: int, wait: float) -> None:
    fields: dict[str, LogValue] = {
        "bot": bot.value,
        "error_code": failed.error.code.value,
        "reason": failed.reason,
        "failures": failures,
        "wait_ms": round(wait * 1000),
    }
    if isinstance(failed.error, AuthFailed):  # a revoked token: only the owner can fix it
        log.error("poll_failed", **fields)
    else:
        log.warning("poll_failed", **fields)


async def run_polling(
    deps: SharedDeps,
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    config: PollConfig = DEFAULT_POLL,
    heartbeat: Heartbeat | None = None,
) -> NoReturn:
    """Poll `getUpdates` for the life of the process (run it under `core.aio.forever`).

    Each batch is fed in `update_id` order, one update at a time (never as tasks), each under `guarded()`, so a broken
    update cannot stall the ones after it. Then `next_offset = max(update_id) + 1` is persisted (inbox): a crash
    mid-batch refetches the batch, and the inbox's idempotency key makes the replay harmless (NFR-REL-05). `heartbeat`
    runs after every `getUpdates` attempt (SPEC §2.8, T32b): a success before its batch is fed, a failure with its
    kind before the backoff wait. A failing heartbeat ends the runner, so `forever` restarts it and the stale attempts
    stay visible to the watchdog.
    """
    offset = await inbox.get_poll_offset(deps)
    failures = 0
    log.info("polling_started", bot=deps.bot.value, offset=offset)
    while True:
        result = await _get_updates(bot, offset=offset, config=config, recovering=failures > 0)
        if isinstance(result, _PollFailed):
            failures += 1
            wait = _wait_after(failures, result.error, config)
            _log_failure(deps.bot, result, failures=failures, wait=wait)
            if heartbeat is not None:
                await heartbeat(PollTick(at=deps.clock.now(), updates=0, error=poll_error_kind(result.error)))
            await deps.clock.sleep(wait)
            continue
        if failures:
            log.info("polling_resumed", bot=deps.bot.value, failures=failures)
            failures = 0
        if heartbeat is not None:
            await heartbeat(PollTick(at=deps.clock.now(), updates=len(result)))
        for update in sorted(result, key=lambda item: item.update_id):  # Telegram sends them in order; never rely on it
            await guarded(deps, dispatcher.feed_update(bot, update), job="feed_update")
        if result:
            offset = await inbox.advance_poll_offset(deps, [update.update_id for update in result])
