"""The polling runner on the recording aiogram session and virtual time (FakeClock): order, the persisted offset, the
1 → 30 s backoff, probes after a failure, the heartbeat hook point and the failure kind it carries (T32b), and
NFR-REL-03 (SPEC §2.8, §3.2.1 step 1, §4.6).

Every wait goes through `deps.clock.sleep`, so a test advances virtual time instead of sleeping; only a getUpdates
that hangs on purpose waits on the real loop (for a few milliseconds).
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Message, Update

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    STRANGER_ID,
    Fakes,
    TelegramJson,
    tg_message,
)
from tests.fakes.clock import FakeClock
from tests.fakes.telegram_session import Answer, RecordingSession
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.bots import common
from yordamchi.bots.common import PollConfig, PollTick
from yordamchi.core import strings as S
from yordamchi.core.redact import LOG_FIELDS_ATTR
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health.domain import PollErrorKind
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import IncomingUpdate, UpdateKind

STEADY = PollConfig(jitter=0.0)  # the production schedule without jitter, so waits are exact


class PollingSession(RecordingSession):
    """getUpdates answers what is scripted; when nothing is, it hangs until the test cancels the runner. While
    `offline_until` is ahead of the clock, every getUpdates fails as a dead network would."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.offline_until: datetime | None = None
        self.polled_at: list[datetime] = []
        self.closed_at: list[datetime] = []

    async def close(self) -> None:
        self.closed_at.append(self.clock.now())

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if method.__api_method__ == "getUpdates":
            self.polled_at.append(self.clock.now())
            if self.offline_until is not None and self.clock.now() < self.offline_until:
                self._scripted["getUpdates"].appendleft(_network_down)
            elif not self._scripted["getUpdates"]:
                self.hang = True  # nothing else is in flight while the runner polls
        return await super().make_request(bot, method, timeout)

    def polls(self) -> list[dict[str, Any]]:
        return [request.params for request in self.calls("getUpdates")]

    def poll_timeouts(self) -> list[int | None]:
        return [request.timeout for request in self.calls("getUpdates")]


def _network_down(method: TelegramMethod[Any]) -> BaseException:
    return TelegramNetworkError(method=method, message="Cannot connect to host api.telegram.org:443")


def _batch(*updates: TelegramJson) -> Answer:
    return Answer.json(200, {"ok": True, "result": list(updates)})


@dataclass
class Runner:
    deps: SharedDeps
    dispatcher: Dispatcher
    bot: Bot
    session: PollingSession
    clock: FakeClock
    fed: list[int] = field(default_factory=list)
    ticks: list[PollTick] = field(default_factory=list)

    async def heartbeat(self, tick: PollTick) -> None:
        self.ticks.append(tick)

    @property
    def ok_ticks(self) -> list[PollTick]:
        return [tick for tick in self.ticks if tick.error is PollErrorKind.NONE]

    @asynccontextmanager
    async def running(self, config: PollConfig = STEADY) -> AsyncIterator[None]:
        task = asyncio.get_running_loop().create_task(
            common.run_polling(self.deps, self.dispatcher, self.bot, config=config, heartbeat=self.heartbeat)
        )
        try:
            yield
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def offset(self) -> int:
        return await inbox.get_poll_offset(self.deps)

    async def stored(self) -> list[int]:
        async with self.deps.db.read() as s:
            return [row.update_id for row in await inbox_store.list_inbound_updates(s, self.deps.bot.value)]

    async def advance_through_waits(self, count: int) -> None:
        """Let the runner reach each of its next `count` backoff waits, and end each one."""
        for _ in range(count):
            await _until(lambda: self.clock.pending_sleepers == 1)
            await self.clock.advance(self.clock.sleeps[-1])


async def _until(condition: Callable[[], bool], *, within_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_s
    while not condition():
        assert loop.time() < deadline, "the runner did not get there"
        await asyncio.sleep(0.002)


def _router(fed: list[int]) -> Router:
    router = Router()

    @router.message()
    async def on_message(message: Message, origin: OwnerOrigin, deps: SharedDeps) -> None:
        fed.append(origin.update_id)
        incoming = IncomingUpdate(
            bot=deps.bot,
            update_id=origin.update_id,
            chat_id=message.chat.id,
            kind=UpdateKind.TEXT,
            user_id=origin.user_id,
            message_id=message.message_id,
            text=message.text,
        )
        await inbox.accept_update(deps, origin, incoming)

    return router


@pytest.fixture
def runner(dev_deps: DevDeps, fakes: Fakes) -> Runner:
    session = PollingSession(fakes.clock)
    messenger = TelegramMessenger(DEV_BOT_TOKEN, session=session)
    deps = replace(dev_deps, messenger=messenger)
    fed: list[int] = []
    dispatcher = common.build_dispatcher(deps, _router(fed))
    dispatcher["deps"] = deps
    return Runner(deps, dispatcher, messenger.bot, session, fakes.clock, fed=fed)


# --- order and the offset (SPEC §3.2.1 step 1, §4.6) ----------------------------------------------------------------
async def test_a_batch_is_fed_in_order_and_its_offset_persisted_after_it(runner: Runner) -> None:
    runner.session.script(
        "getUpdates", _batch(tg_message(10), tg_message(11, user_id=STRANGER_ID), tg_message(12, text="davom et"))
    )
    async with runner.running():
        await _until(lambda: len(runner.session.calls("getUpdates")) == 2)

    assert runner.fed == [10, 12]  # in update_id order; the stranger reached no handler
    assert await runner.stored() == [10, 12]
    assert await runner.offset() == 13  # the dropped update is confirmed too
    first, second = runner.session.polls()
    assert first == {"timeout": 25, "allowed_updates": ["message", "edited_message", "my_chat_member"]}
    assert second == {"offset": 13, "timeout": 25, "allowed_updates": ["message", "edited_message", "my_chat_member"]}
    assert runner.session.poll_timeouts() == [35, 35]  # the long poll plus its grace


async def test_order_comes_from_update_id_even_if_a_batch_arrives_shuffled(runner: Runner) -> None:
    runner.session.script("getUpdates", _batch(tg_message(16), tg_message(14), tg_message(15)))
    async with runner.running():
        await _until(lambda: len(runner.session.calls("getUpdates")) == 2)
    assert runner.fed == [14, 15, 16]
    assert await runner.offset() == 17


async def test_polling_resumes_from_the_persisted_offset(runner: Runner) -> None:
    await inbox.advance_poll_offset(runner.deps, [41])
    async with runner.running():
        await _until(lambda: len(runner.session.calls("getUpdates")) == 1)
    assert runner.session.polls()[0]["offset"] == 42


async def test_an_empty_batch_leaves_the_offset_alone(runner: Runner) -> None:
    runner.session.script("getUpdates", _batch())
    async with runner.running():
        await _until(lambda: len(runner.session.calls("getUpdates")) == 2)
    assert await runner.offset() == 0
    assert len(runner.ticks) == 1


# --- backoff (SPEC §2.8, NFR-REL-03) --------------------------------------------------------------------------------
async def test_errors_back_off_1_2_4_up_to_30_s_with_probes_then_long_polling_resumes(
    runner: Runner, caplog: pytest.LogCaptureFixture
) -> None:
    runner.session.script("getUpdates", *([_network_down] * 7), _batch(tg_message(20)))
    with caplog.at_level(logging.INFO):
        async with runner.running():
            await runner.advance_through_waits(7)
            await _until(lambda: len(runner.session.calls("getUpdates")) == 9)

    assert runner.clock.sleeps == [1, 2, 4, 8, 16, 30, 30]
    timeouts = [params["timeout"] for params in runner.session.polls()]
    assert timeouts == [25, 0, 0, 0, 0, 0, 0, 0, 25]  # after a failure: short probes, then long polling again
    assert runner.session.poll_timeouts() == [35, 10, 10, 10, 10, 10, 10, 10, 35]
    assert runner.fed == [20]
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("poll_failed") == 7
    assert "polling_resumed" in messages


async def test_waits_are_jittered_but_never_longer_than_the_schedule(runner: Runner) -> None:
    runner.session.script("getUpdates", *([_network_down] * 7))
    async with runner.running(config=PollConfig()):
        await runner.advance_through_waits(7)
    ceilings = [1, 2, 4, 8, 16, 30, 30]
    assert all(0.8 * ceiling <= wait <= ceiling for wait, ceiling in zip(runner.clock.sleeps, ceilings, strict=True))


async def test_a_429_waits_at_least_its_retry_after(runner: Runner) -> None:
    runner.session.script("getUpdates", Answer.fixture("too_many_requests"))
    async with runner.running():
        await _until(lambda: runner.clock.pending_sleepers == 1)
    assert runner.clock.sleeps == [7]


async def test_a_revoked_token_is_an_error_for_the_owner(runner: Runner, caplog: pytest.LogCaptureFixture) -> None:
    runner.session.script("getUpdates", Answer.fixture("unauthorized"), Answer.fixture("bad_gateway"))
    with caplog.at_level(logging.WARNING):
        async with runner.running():
            await runner.advance_through_waits(2)
    levels = [record.levelno for record in caplog.records if record.getMessage() == "poll_failed"]
    assert levels == [logging.ERROR, logging.WARNING]


async def test_a_hung_get_updates_is_cut_off_by_its_budget(runner: Runner, caplog: pytest.LogCaptureFixture) -> None:
    fast = PollConfig(jitter=0.0, long_poll_s=0, request_grace_s=0.02, probe_budget_s=0.02)
    with caplog.at_level(logging.WARNING):
        async with runner.running(config=fast):
            await runner.advance_through_waits(2)
    failed = [record for record in caplog.records if record.getMessage() == "poll_failed"]
    reasons = [getattr(record, LOG_FIELDS_ATTR)["reason"] for record in failed]
    assert reasons[:2] == ["request_timeout", "request_timeout"]
    assert runner.clock.sleeps[:2] == [1, 2]


async def test_polling_resumes_within_60_s_of_connectivity_after_a_10_minute_outage(runner: Runner) -> None:
    """NFR-REL-03: Wi-Fi off 10 min, then on → polling resumes ≤ 60 s later, and the waiting message is processed."""
    back_at = runner.clock.now() + timedelta(minutes=10)
    runner.session.offline_until = back_at
    runner.session.script("getUpdates", _batch(tg_message(30)))
    async with runner.running(config=PollConfig()):
        while not runner.ok_ticks:
            await _until(lambda: runner.clock.pending_sleepers == 1 or bool(runner.ok_ticks))
            if not runner.ok_ticks:
                await runner.clock.advance(runner.clock.sleeps[-1])
        await _until(lambda: runner.fed == [30])

    assert runner.ok_ticks[0].at - back_at <= timedelta(seconds=60)
    assert max(runner.clock.sleeps) <= 30
    assert len(runner.session.polled_at) > 15  # it kept trying through the outage
    failed = runner.ticks[: runner.ticks.index(runner.ok_ticks[0])]
    assert len(failed) == len(runner.session.polled_at) - 1  # every failed attempt reached the heartbeat
    assert {tick.error for tick in failed} == {PollErrorKind.NETWORK}  # offline, not dead (T32b)


# --- renewing the HTTP session during an outage (2026-10-27) -------------------------------------------------------
async def test_a_long_network_outage_renews_the_http_session_every_5_min(
    runner: Runner, caplog: pytest.LogCaptureFixture
) -> None:
    """A resolver repaired under a running bot went unseen for up to an hour, because aiogram caches DNS answers for
    that long. Attempts at 0, 1, 3, 7, 15, 31, 61, 91 … s: the first one ≥ 300 s into the streak renews, then the
    first one ≥ 300 s after that."""
    start = runner.clock.now()
    runner.session.offline_until = start + timedelta(minutes=12)
    runner.session.script("getUpdates", _batch(tg_message(50)))
    with caplog.at_level(logging.INFO):
        async with runner.running():
            while not runner.ok_ticks:
                await _until(lambda: runner.clock.pending_sleepers == 1 or bool(runner.ok_ticks))
                if not runner.ok_ticks:
                    await runner.clock.advance(runner.clock.sleeps[-1])
            await _until(lambda: runner.fed == [50])

    assert [at - start for at in runner.session.closed_at] == [timedelta(seconds=301), timedelta(seconds=601)]
    renewed = [record for record in caplog.records if record.getMessage() == "poll_session_renewed"]
    assert [getattr(record, LOG_FIELDS_ATTR)["failures"] for record in renewed] == [15, 25]


async def test_a_streak_broken_by_a_successful_poll_starts_its_5_min_again(runner: Runner) -> None:
    """Two 151 s streaks span 332 s in all, but neither lasts 300 s: a flapping network renews nothing."""
    streak = [_network_down] * 10
    runner.session.script("getUpdates", *streak, _batch(), *streak)
    async with runner.running():
        await runner.advance_through_waits(20)
    assert runner.session.polled_at[-1] - runner.session.polled_at[0] > timedelta(seconds=300)
    assert runner.session.closed_at == []


async def test_failures_other_than_the_network_never_renew_the_session(runner: Runner) -> None:
    runner.session.script("getUpdates", *([Answer.fixture("unauthorized")] * 16))
    async with runner.running():
        await runner.advance_through_waits(16)
    assert runner.session.polled_at[-1] - runner.session.polled_at[0] >= timedelta(seconds=300)
    assert runner.session.closed_at == []


async def test_aiogram_builds_a_new_connection_pool_after_close() -> None:
    """The renewal relies on this: `AiohttpSession.close()` drops the aiohttp session with its connector and DNS cache,
    and the next request builds new ones."""
    session = AiohttpSession()
    first = await session.create_session()
    await session.close()
    second = await session.create_session()
    try:
        assert first.closed
        assert second is not first
        assert second.connector is not first.connector
    finally:
        await session.close()


# --- the heartbeat hook point and a broken update ------------------------------------------------------------------
async def test_the_heartbeat_runs_after_every_attempt_and_a_failure_carries_its_kind(runner: Runner) -> None:
    failed_at = runner.clock.now()
    runner.session.script("getUpdates", _network_down, _batch(tg_message(40), tg_message(41)), _batch())
    async with runner.running():
        await runner.advance_through_waits(1)
        await _until(lambda: len(runner.ticks) == 3)
    assert runner.ticks == [
        PollTick(at=failed_at, updates=0, error=PollErrorKind.NETWORK),  # before the backoff wait
        PollTick(at=runner.clock.now(), updates=2),
        PollTick(at=runner.clock.now(), updates=0),
    ]


async def test_a_failure_is_network_only_when_the_connection_failed(runner: Runner) -> None:
    runner.session.script(
        "getUpdates",
        _network_down,
        Answer.fixture("bad_gateway"),
        Answer.fixture("proxy_html_502"),
        Answer.fixture("unauthorized"),
        Answer.fixture("conflict"),
        Answer.fixture("too_many_requests"),
    )
    async with runner.running():
        await runner.advance_through_waits(6)
    network, other = PollErrorKind.NETWORK, PollErrorKind.OTHER
    assert [tick.error for tick in runner.ticks] == [network, network, network, other, other, other]


async def test_a_hung_get_updates_is_a_network_failure(runner: Runner) -> None:
    fast = PollConfig(jitter=0.0, long_poll_s=0, request_grace_s=0.02, probe_budget_s=0.02)
    async with runner.running(config=fast):
        await runner.advance_through_waits(1)
    assert runner.ticks[0].error is PollErrorKind.NETWORK


async def test_a_broken_update_does_not_stall_the_ones_after_it(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = runner.dispatcher.feed_update

    async def feed_or_break(bot: Bot, update: Update) -> object:
        if update.update_id == 51:
            msg = "a bug outside every handler"
            raise RuntimeError(msg)
        return await feed(bot, update)

    monkeypatch.setattr(runner.dispatcher, "feed_update", feed_or_break)
    runner.session.script("getUpdates", _batch(tg_message(50), tg_message(51), tg_message(52)))
    async with runner.running():
        await _until(lambda: len(runner.session.calls("getUpdates")) == 2)

    assert runner.fed == [50, 52]
    assert await runner.offset() == 53
    [notice] = runner.session.calls("sendMessage")  # the owner hears of it in the dev group, with an incident id
    assert notice.params["chat_id"] == DEV_GROUP_ID
    incident = re.search(r"#([0-9a-f]{8})\)", str(notice.params["text"]))
    assert incident is not None
    assert notice.params["text"] == S.ERR_INTERNAL(incident=incident.group(1))
