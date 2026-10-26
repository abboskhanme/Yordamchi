"""The fault and load suites' world: the real wired dev bot, its real poller and a fake Telegram that outlives it.

`tests/integration/dev_bot_harness.py` feeds updates to the dispatcher by hand and leaves the poller out. A fault
test needs the opposite: what Telegram holds while the bot is down or offline, what it confirms, what it really
posted when the answer was lost. So:

- `FakeTelegram` is the Bot API server, outside every bot process: it keeps the owner's updates until a `getUpdates`
  with a higher offset confirms them (Bot API semantics), assigns message ids, records every request on the virtual
  clock, and knows the ground truth of each send (posted or not) whatever the bot was told. It can be down (no
  connection), answer 5xx, time out, drop the connection after posting, or answer 429 with `retry_after`. It flags any
  request that breaks Telegram's limits (1 message/s per chat, 20/min per group; edits count, as SPEC §3.2.1 step 9).
- A `BotProcess` is one run of `bots/dev/runtime.DevApp` with `polling=True`: its own SQLite engine on the shared
  `dev.db` file, its own aiogram session bound to the fake Telegram, and the shared fakes behind a proxy. `kill()` is
  `kill -9`: at that instant every task of the process is cancelled, and every DB or port call it would still make
  (a `finally`, an `except` handler) raises at once, so nothing reaches the DB, Telegram or the executor after the
  kill. No shutdown pass runs and no clean marker is written. A kill can also be armed inside the bot, at the n-th unit
  of work: just before it begins, or just after it commits (`FaultDatabase.arm`).
- `World` holds what survives a process: the data dir, the DB file, the fakes (clock, executor, git, keystore), the
  fake Telegram and an observer engine the test reads the DB through.

All times are virtual (`FakeClock`), so a latency is measured in virtual seconds; between clock steps the real event
loop (aiosqlite threads included) is given a few milliseconds to catch up.
"""

import asyncio
import html
import inspect
import json
import random
import re
import shutil
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import TelegramType
from sqlalchemy import event, select

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    FAKE_KEYS,
    OWNER_ID,
    Fakes,
    TelegramJson,
    make_dev_deps,
    open_test_db,
    store_secret,
    tg_chat,
    tg_user,
)
from tests.fakes.agent import FakeAgentRuntime, ScriptItem
from tests.fakes.clock import FakeClock
from tests.fakes.telegram_session import Answer, ForumTopics, RecordingSession
from tests.integration.dev_bot_harness import register_projects, short_paths
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.bots.dev import runtime
from yordamchi.bots.dev.runtime import DevApp
from yordamchi.core import aio, redact
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.turns.domain import RESTART_TOLD, RESTART_UNTOLD, DeliveryStatus
from yordamchi.dev.turns.domain import TERMINAL as TURN_TERMINAL
from yordamchi.dev.turns.models import Delivery, Turn
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database, Session
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    DeliveryMode,
    RuntimeStatus,
    SessionHandle,
    SessionSpec,
)
from yordamchi.ports.git import Git
from yordamchi.shared.burst.domain import TERMINAL as BURST_TERMINAL
from yordamchi.shared.burst.models import Burst
from yordamchi.shared.inbox.models import InboundUpdate
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.domain import Kind, Op, Status
from yordamchi.shared.outbox.models import OutboxMessage

VERSION: Final = "2026.10.24-fault"
STEP_S: Final = 0.25  # virtual time per step: the outbox sender's idle poll
SETTLE_S: Final = 0.004  # real time per settle round, for aiosqlite threads and ready tasks
SETTLE_ROUNDS: Final = 3
REAP_S: Final = 10.0  # real seconds a killed process's tasks and connections get to wind down
QUIESCE_ROUNDS: Final = 60  # at most ~0.25 s of real time per virtual step while waiting for the bot to go idle
STRANDED_S: Final = 1.0  # real seconds before a stranded connection close is ended (`_closing_connection`)

# Bot API methods that post a new message, and those Telegram's flood limits count (edits included).
MESSAGE_METHODS: Final = frozenset({"sendMessage", "sendPhoto", "sendDocument", "sendVoice", "sendVideo"})
COUNTED_METHODS: Final = frozenset({*MESSAGE_METHODS, "editMessageText"})
PER_CHAT: Final = (1, timedelta(seconds=1))
PER_GROUP: Final = (20, timedelta(minutes=1))
SEND_OPS: Final = frozenset({Op.SEND_TEXT, Op.SEND_DOCUMENT, Op.SEND_PHOTO, Op.SEND_VOICE, Op.SEND_VIDEO})
TOKEN: Final = re.compile(r"#\d{2,3}\b")


# --- a process that can die ----------------------------------------------------------------------------------------
class Mortality:
    """Whether one bot process is alive. `kill()` is `kill -9`: every task but the test's own is cancelled now, and
    `check()` (called before every DB and port call of the process) raises from then on, so the code a cancellation
    still runs (`finally`, `except` blocks) has no effect outside the process."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.driver = asyncio.current_task()
        self.dead = False
        self.killed_at: datetime | None = None
        self._doomed: set[asyncio.Task[Any]] = set()

    def check(self) -> None:
        if self.dead:
            raise asyncio.CancelledError

    def kill(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.killed_at = self.clock.now()
        self._cancel_all()

    def _cancel_all(self) -> int:
        doomed = {task for task in asyncio.all_tasks() if task is not self.driver and not task.done()}
        for task in doomed:
            task.cancel()
        self._doomed |= doomed
        return len(doomed)

    async def reap(self) -> None:
        """Wait until nothing of the process runs any more (a dying task may have spawned another). A task that
        outlives its cancellation by `REAP_S` of real time is a harness bug: its stack is in the failure."""
        while True:
            pending = [task for task in self._doomed if not task.done()]
            if not pending:
                if self._cancel_all() == 0:
                    return
                continue
            _done, stuck = await asyncio.wait(pending, timeout=STRANDED_S)
            if not stuck:
                continue
            for task in stuck:
                if _closing_connection(task):
                    task.cancel()
            _done, stuck = await asyncio.wait(stuck, timeout=REAP_S)
            if stuck:
                stacks = "; ".join(f"{task.get_name()}: {task.get_stack(limit=3)}" for task in stuck)
                msg = f"tasks of a killed process still run: {stacks}"
                raise AssertionError(msg)


def _writable(path: str) -> bool:
    """Whether a new process could begin a write transaction on `path` right now."""
    raw = sqlite3.connect(path, timeout=0.5, isolation_level=None)
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("ROLLBACK")
    except sqlite3.OperationalError:
        return False
    finally:
        raw.close()
    return True


def _closing_connection(task: asyncio.Task[Any]) -> bool:
    """SQLAlchemy closes an invalidated aiosqlite connection in a shielded task of its own. Cancelled together with
    its parent, aiosqlite queues a second stop to a worker thread that already exited, and that task waits forever: an
    in-process artefact (a dead process has no tasks); `FaultDatabase.release` closes the connection anyway."""
    return getattr(task.get_coro(), "__qualname__", "").endswith("_terminate_graceful_close")


class FaultDatabase(Database):
    """One process's handle on the shared `dev.db` file (its own engine). It dies with the process, and it can kill
    the process at its n-th unit of work: `arm(n, before=True)` just before that one begins, `before=False` right
    after it commits (so what the commit made durable stays, and nothing after it happens)."""

    def __init__(self, db: Database, mortality: Mortality) -> None:
        super().__init__(db.engine, readonly=False)
        self.mortality = mortality
        self.units = 0  # units of work begun
        self.commits = 0
        self._kill_at: tuple[int, bool] | None = None
        self._connections: list[object] = []
        event.listen(self.engine.sync_engine, "connect", self._opened)

    def _opened(self, dbapi_connection: object, _record: object) -> None:
        self._connections.append(dbapi_connection)

    async def release(self) -> None:
        """What the OS does for a dead process: every connection it had closes, so an open transaction rolls back
        and its lock on the file goes (a cancelled task's own rollback may have been cancelled in turn)."""
        for adapted in self._connections:
            close = getattr(getattr(adapted, "_connection", None), "close", None)
            if close is None:
                continue
            try:
                async with asyncio.timeout(REAP_S):
                    await close()
            except (sqlite3.Error, ValueError):  # already closed by the pool
                continue
            except TimeoutError:  # its worker thread stopped with the pool's own close: nothing left to release
                continue
        await self.engine.dispose()
        if not await asyncio.to_thread(_writable, str(self.engine.url.database)):
            msg = "a killed process still holds the write lock on dev.db (harness bug, not the bot's)"
            raise AssertionError(msg)

    def arm(self, unit: int, *, before: bool) -> None:
        self._kill_at = (unit, before)

    def disarm(self) -> None:
        self._kill_at = None

    def _die(self) -> None:
        self.mortality.kill()
        raise asyncio.CancelledError

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[Session]:
        self.mortality.check()
        self.units += 1
        if self._kill_at == (self.units, True):
            self._die()
        async with super().uow() as s:
            yield s
        self.commits += 1
        self.mortality.check()  # killed while committing: the commit stands, the code after it never runs
        if self._kill_at == (self.units, False):
            self._die()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Session]:
        self.mortality.check()
        async with super().read() as s:
            yield s


# --- the fake Telegram server --------------------------------------------------------------------------------------
class Fault(StrEnum):
    """How one request fails. Only RESET and TIMEOUT_POSTED post the message (the answer is what got lost)."""

    DOWN = "down"  # no connection at all (Wi-Fi off, DNS): the request never left
    SERVER_ERROR = "server_error"  # Telegram or its proxy answered 502: nothing posted
    TIMEOUT = "timeout"  # no answer within the request's own timeout; nothing posted
    TIMEOUT_POSTED = "timeout_posted"  # posted, but the answer never came back within the timeout
    RESET = "reset"  # posted, then the connection dropped before the answer
    FLOOD = "flood"  # 429 Too Many Requests with `retry_after`


POSTING_FAULTS: Final = frozenset({Fault.RESET, Fault.TIMEOUT_POSTED})

# Which fault, if any, one request meets (it sees the method, so a plan can pick by kind or text); None: it succeeds.
type FaultPlan = Callable[[TelegramMethod[Any]], Fault | None]


@dataclass(frozen=True, slots=True)
class Call:
    """One request as Telegram saw it (or did not: a DOWN call is recorded for the test's sake)."""

    at: datetime
    api_method: str
    chat_id: int | None
    fault: Fault | None
    posted: bool  # a message was created (sends only)


@dataclass(frozen=True, slots=True)
class Post:
    """A message Telegram created, whatever the bot heard back."""

    at: datetime
    api_method: str
    chat_id: int
    message_id: int
    text: str
    thread_id: int | None = None  # the forum topic it went to; None: the chat itself


class FakeTelegram:
    """The Bot API, shared by every process of a test (see the module docstring)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.updates: list[TelegramJson] = []  # not confirmed yet, in update_id order
        self.calls: list[Call] = []
        self.posts: list[Post] = []
        self.edits: list[Post] = []
        self.violations: list[str] = []
        self.message_ids: defaultdict[int, int] = defaultdict(lambda: 1000)  # per chat, owner and bot alike
        self.down = False  # an outage: every request fails as DOWN
        self.plan: FaultPlan | None = None  # faults for the requests that get through
        self.retry_after = 20  # seconds a FLOOD answer asks for; above the adapter's own 10 s, so the outbox parks
        self.flood_until: dict[int | None, datetime] = {}  # per chat (None: getUpdates) while a 429 holds
        self.polls = 0
        self.topics = ForumTopics()  # forum topics outlive every process, as Telegram's do
        self._arrived = asyncio.Event()
        self._counted: defaultdict[int, list[datetime]] = defaultdict(list)

    # --- the owner's side ---
    def new_message_id(self, chat_id: int) -> int:
        self.message_ids[chat_id] += 1
        return self.message_ids[chat_id]

    def deliver(self, raw: TelegramJson) -> None:
        """An update for the bot: held until a `getUpdates` with a higher offset confirms it."""
        self.updates.append(raw)
        self._arrived.set()

    def texts(self) -> list[str]:
        return [post.text for post in self.posts]

    def posted(self, needle: str) -> list[Post]:
        """The posts that hold `needle` as the owner reads them: an HTML post without its tags, a catalog line
        without its markers (the outbox renders markup since the formatting pass)."""
        wanted = outbox_domain.plain_text(needle)
        return [post for post in self.posts if wanted in _visible(post.text)]

    # --- the bot's side ---
    def fault_for(self, method: TelegramMethod[Any]) -> Fault | None:
        if self.down:
            return Fault.DOWN
        return None if self.plan is None else self.plan(method)

    def _honours_floods(self, api_method: str, chat_id: int | None) -> None:
        """A request to a chat Telegram told to wait (429 `retry_after`) before the wait is over."""
        until = self.flood_until.get(chat_id)
        now = self.clock.now()
        if until is not None and now < until:
            self.violations.append(f"{now.isoformat()} {api_method} chat {chat_id}: retry_after not honoured")

    async def get_updates(self, session: "ProcessSession", bot: Bot, method: GetUpdates, timeout: int | None) -> object:
        self.polls += 1
        self._honours_floods("getUpdates", None)
        fault = self.fault_for(method)
        if fault is not None:
            return await self._fail(session, bot, method, fault, timeout, chat_id=None)
        if method.offset is not None:  # Bot API: an offset confirms every update below it
            self.updates = [raw for raw in self.updates if _update_id(raw) >= method.offset]
        wait = float(method.timeout or 0)
        if not self.updates and wait > 0:
            await self._wait_for_updates(wait)
            if self.down:  # the connection died during the long poll
                return await self._fail(session, bot, method, Fault.DOWN, timeout, chat_id=None)
        batch = self.updates[: method.limit or 100]
        self.calls.append(Call(self.clock.now(), "getUpdates", None, None, posted=False))
        content = json.dumps({"ok": True, "result": batch})
        return session.check_response(bot=bot, method=method, status_code=200, content=content).result

    async def _wait_for_updates(self, seconds: float) -> None:
        self._arrived.clear()
        async with asyncio.TaskGroup() as group:
            sleeping = group.create_task(self.clock.sleep(seconds))
            arriving = group.create_task(self._arrived.wait())
            await asyncio.wait({sleeping, arriving}, return_when=asyncio.FIRST_COMPLETED)
            sleeping.cancel()
            arriving.cancel()

    async def request(
        self, session: "ProcessSession", bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None
    ) -> TelegramType:
        api_method = method.__api_method__
        chat_id = _chat_of(method)
        self._honours_floods(api_method, chat_id)
        fault = self.fault_for(method)
        if fault is not None and fault not in POSTING_FAULTS:
            return cast(TelegramType, await self._fail(session, bot, method, fault, timeout, chat_id=chat_id))
        result = await RecordingSession.make_request(session, bot, method, timeout)
        params = session.requests[-1].params
        posted = api_method in MESSAGE_METHODS
        self._account(api_method, chat_id, params, result, posted=posted)  # counted when it arrives
        self.calls.append(Call(self.clock.now(), api_method, chat_id, fault, posted=posted))
        if fault is Fault.RESET:
            raise TelegramNetworkError(method=method, message="ServerDisconnectedError: Server disconnected")
        if fault is Fault.TIMEOUT_POSTED:  # the answer never comes back: the caller waits out its own timeout
            await self.clock.sleep(float(timeout or 10))
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return result

    def _account(
        self, api_method: str, chat_id: int | None, params: Mapping[str, Any], result: object, *, posted: bool
    ) -> None:
        if api_method not in COUNTED_METHODS or chat_id is None:
            return
        now = self.clock.now()
        recent = self._counted[chat_id]
        for count, period, scope in ((*PER_CHAT, "s"), (*PER_GROUP, "min")):
            inside = [at for at in recent if at > now - period]
            if len(inside) >= count and (scope == "s" or chat_id < 0):
                before = ", ".join(at.time().isoformat() for at in inside[-3:])
                what = f"{api_method} chat {chat_id}: over {count}/{scope} (last before: {before})"
                self.violations.append(f"{now.isoformat()} {what}")
        recent.append(now)
        text = str(params.get("text") or params.get("caption") or "")
        message_id = getattr(result, "message_id", None) or int(params.get("message_id", 0))
        thread = params.get("message_thread_id")
        record = Post(now, api_method, chat_id, int(message_id), text, thread if isinstance(thread, int) else None)
        (self.posts if posted else self.edits).append(record)

    async def _fail(
        self,
        session: "ProcessSession",
        bot: Bot,
        method: TelegramMethod[Any],
        fault: Fault,
        timeout: int | None,
        *,
        chat_id: int | None,
    ) -> object:
        api_method = method.__api_method__
        if fault is Fault.TIMEOUT:
            await self.clock.sleep(float(timeout or 10))
        self.calls.append(Call(self.clock.now(), api_method, chat_id, fault, posted=False))
        if fault is Fault.DOWN:
            message = "ClientConnectorError: Cannot connect to host api.telegram.org:443 ssl:default"
            raise TelegramNetworkError(method=method, message=message)
        if fault is Fault.TIMEOUT:
            raise TelegramNetworkError(method=method, message="Request timeout error")
        if fault is Fault.SERVER_ERROR:
            answer = Answer.fixture("bad_gateway")
        else:
            answer = Answer.fixture("too_many_requests", retry_after=self.retry_after)
            self.flood_until[chat_id] = self.clock.now() + timedelta(seconds=self.retry_after)
        return session.check_response(bot=bot, method=method, status_code=answer.status, content=answer.content)


def _update_id(raw: TelegramJson) -> int:
    value = raw["update_id"]
    assert isinstance(value, int)
    return value


def _chat_of(method: TelegramMethod[Any]) -> int | None:
    chat_id = getattr(method, "chat_id", None)
    return chat_id if isinstance(chat_id, int) else None


class ProcessSession(RecordingSession):
    """One process's aiogram session: every request goes to the shared `FakeTelegram`, and none once it is dead."""

    def __init__(self, telegram: FakeTelegram, mortality: Mortality) -> None:
        super().__init__(topics=telegram.topics)
        self.telegram = telegram
        self.mortality = mortality
        self._next_id = telegram.message_ids  # message ids are Telegram's, across restarts

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        self.mortality.check()
        if isinstance(method, GetUpdates):
            return cast(TelegramType, await self.telegram.get_updates(self, bot, method, timeout))
        return await self.telegram.request(self, bot, method, timeout)


# --- the executor, shared across processes -------------------------------------------------------------------------
type Script = Callable[[AgentInput], Sequence[ScriptItem]]


class ProcessAgents:
    """One process's view of the shared `FakeAgentRuntime` (the `claude` sessions outlive the bot): nothing once the
    process is dead. `script` picks what each new CLI turn plays, from the text it was sent; opening a session writes
    its transcript file, as the CLI does, so a restart resumes it by id."""

    def __init__(self, fake: FakeAgentRuntime, mortality: Mortality, *, home: Path, script: Script | None) -> None:
        self.fake = fake
        self.mortality = mortality
        self.home = home
        self.script = script

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        self.mortality.check()
        return await self.fake.probe(timeout=timeout)

    async def open(self, spec: SessionSpec, *, resume_id: str | None, timeout: float) -> SessionHandle:
        self.mortality.check()
        handle = await self.fake.open(spec, resume_id=resume_id, timeout=timeout)
        transcript = self.home / ".claude" / "projects" / spec.cwd.name / f"{handle.session_id}.jsonl"
        await asyncio.to_thread(_touch, transcript)
        return handle

    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: DeliveryMode, timeout: float) -> None:
        self.mortality.check()
        merged = mode == "next" and self.fake.busy(h.session_id)
        if self.script is not None and not merged:
            self.fake.script_turn(*self.script(msg))
        await self.fake.send(h, msg, mode=mode, timeout=timeout)

    async def answer(self, h: SessionHandle, question_id: str, answers: Mapping[str, str], *, timeout: float) -> bool:
        self.mortality.check()
        return await self.fake.answer(h, question_id, answers, timeout=timeout)

    async def interrupt(self, h: SessionHandle, *, timeout: float) -> None:
        self.mortality.check()
        await self.fake.interrupt(h, timeout=timeout)

    async def set_model(self, h: SessionHandle, model: str, *, timeout: float) -> None:
        self.mortality.check()
        await self.fake.set_model(h, model, timeout=timeout)

    async def close(self, h: SessionHandle, *, timeout: float) -> None:
        self.mortality.check()
        await self.fake.close(h, timeout=timeout)

    async def events(self, h: SessionHandle, *, idle_timeout: float) -> AsyncIterator[AgentEvent]:
        self.mortality.check()
        async for item in self.fake.events(h, idle_timeout=idle_timeout):
            self.mortality.check()
            yield item


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


# --- one process and the world it lives in -------------------------------------------------------------------------
@dataclass
class BotProcess:
    app: DevApp
    deps: DevDeps
    db: FaultDatabase
    session: ProcessSession
    mortality: Mortality
    started_at: datetime
    stopped: bool = False

    @property
    def dead(self) -> bool:
        return self.mortality.dead

    async def kill(self) -> None:
        """`kill -9`, now (from the test)."""
        self.mortality.kill()
        await self.reap()

    async def reap(self) -> None:
        """After a kill (here or armed inside the bot): wait for every task to end, then let the engine go, as the OS
        releases a dead process's file locks."""
        await self.mortality.reap()
        await self.db.release()
        self.stopped = True

    async def stop(self) -> None:
        """SIGTERM: the shutdown pass of SPEC §2.6, ending in the clean marker."""
        self.db.disarm()  # a kill the test armed and the process never reached is not part of its shutdown
        await self.app.stop()
        self.stopped = True
        await self.db.engine.dispose()


@dataclass
class World:
    paths: Paths
    root: Path
    fakes: Fakes
    telegram: FakeTelegram
    observer: Database  # the test's own read handle on dev.db
    script: Script | None = None
    repos: Git | None = None  # instead of `fakes.git`, for a test that must hold the repository reads at one point
    processes: list[BotProcess] = field(default_factory=list)
    next_update: int = 100
    sent: dict[int, str] = field(default_factory=dict)  # update_id → the owner's text

    @property
    def clock(self) -> FakeClock:
        return self.fakes.clock

    @property
    def bot(self) -> BotProcess:
        return self.processes[-1]

    # --- processes ---
    async def start(self, *, kill_at: tuple[int, bool] | None = None, retry_base_delay_s: float = 0.005) -> BotProcess:
        """A new process on the same files. Its start pass (recovery, the restart notice) runs as one of its tasks,
        so a kill armed inside it (`kill_at`: unit of work, before it) ends the start, as a crash loop would."""
        mortality = Mortality(self.clock)
        db = FaultDatabase(open_database(self.paths.db_file(BotName.DEV)), mortality)
        if kill_at is not None:
            db.arm(kill_at[0], before=kill_at[1])
        session = ProcessSession(self.telegram, mortality)
        messenger = TelegramMessenger(DEV_BOT_TOKEN, session=session)
        # The adapter's own in-call backoff runs on real time; keep it short (its attempts and deadlines stay).
        messenger.retry_base_delay_s = retry_base_delay_s
        messenger.retry_max_delay_s = retry_base_delay_s * 8
        agents = ProcessAgents(self.fakes.agents, mortality, home=self.paths.home, script=self.script)
        deps = replace(make_dev_deps(db=db, paths=self.paths, fakes=self.fakes), messenger=messenger, agents=agents)
        if self.repos is not None:
            deps = replace(deps, git=self.repos)
        app = runtime.build_app(deps, bot=messenger.bot, version=VERSION, closers=(messenger.close,))
        process = BotProcess(app, deps, db, session, mortality, started_at=self.clock.now())
        self.processes.append(process)
        starting = aio.spawn("fault_bot_start", app.start(polling=True))
        await asyncio.wait({starting})
        db.disarm()  # `kill_at` is a death inside the start pass; one that did not come by now never comes
        if not starting.cancelled() and (error := starting.exception()) is not None:
            raise error
        return process

    async def crash(self) -> None:
        """`kill -9` of the running process, from outside."""
        await self.bot.kill()

    # --- the owner ---
    def say(
        self, text: str, *, chat_id: int = DEV_GROUP_ID, user_id: int = OWNER_ID, thread_id: int | None = None
    ) -> int:
        """The owner posts `text` (in forum topic `thread_id`); Telegram holds it for the bot. Returns its update id."""
        self.next_update += 1
        update_id = self.next_update
        chat = tg_chat(chat_id)
        if self.telegram.topics.forum:
            chat["is_forum"] = True
        message: TelegramJson = {
            "message_id": self.telegram.new_message_id(chat_id),
            "date": int(self.clock.now().timestamp()),
            "chat": chat,
            "from": tg_user(user_id),
            "text": text,
        }
        if thread_id is not None:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True
        self.telegram.deliver({"update_id": update_id, "message": message})
        self.sent[update_id] = text
        return update_id

    # --- time ---
    async def settle(self, rounds: int = SETTLE_ROUNDS) -> None:
        for _ in range(rounds):
            await asyncio.sleep(SETTLE_S)

    async def advance(self, seconds: float, *, step: float = STEP_S) -> None:
        steps = max(1, round(seconds / step))
        for _ in range(steps):
            await self.clock.advance(seconds / steps)
            await self.settle()
            await self._reap_if_killed()

    async def quiesce(self) -> None:
        """Settle until the bot stops reacting: no new unit of work, Telegram request or executor send for two rounds
        in a row (at most `QUIESCE_ROUNDS`), so the next clock step finds it idle however busy the machine is."""

        def mark() -> tuple[int, int, int]:
            units = self.bot.db.units if self.processes else 0
            return units, len(self.telegram.calls), len(self.fakes.agents.sends)

        last, stable = mark(), 0
        for _ in range(QUIESCE_ROUNDS):
            await asyncio.sleep(SETTLE_S)
            seen = mark()
            stable = stable + 1 if seen == last else 0
            if stable >= 2:  # two quiet rounds in a row
                return
            last = seen

    async def within(
        self, seconds: float, check: Callable[[], object], *, what: str, step: float = STEP_S, idle: bool = False
    ) -> float:
        """Advance virtual time until `check` holds; the virtual seconds it took. Fails past `seconds`. `idle`: before
        each step, wait for the bot to stop reacting (`quiesce`), so a latency measures the bot, not the machine."""
        elapsed = 0.0
        while True:
            await (self.quiesce() if idle else self.settle())
            outcome = check()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome:
                return elapsed
            if elapsed >= seconds:
                msg = f"{what}: not within {seconds} s (virtual)"
                raise AssertionError(msg)
            await self.clock.advance(step)
            elapsed += step
            await self._reap_if_killed()

    async def _reap_if_killed(self) -> None:
        if self.processes and self.bot.dead and not self.bot.stopped:
            await self.bot.reap()

    # --- reading the DB the way an operator would ---
    @asynccontextmanager
    async def read(self) -> AsyncIterator[Session]:
        async with self.observer.read() as s:
            yield s

    async def close(self) -> None:
        for process in self.processes:
            if process.stopped:
                continue
            if process.dead:
                await process.reap()
            else:
                await process.stop()
        await self.observer.dispose()
        shutil.rmtree(self.root, ignore_errors=True)


@asynccontextmanager
async def world(*projects: str, script: Script | None = None) -> AsyncIterator[World]:
    """A data dir with `projects` registered (git clean, pushing to GitHub), the Google key in the vault, and nothing
    running yet: the test starts the first process."""
    paths, root = short_paths()
    fakes = Fakes()
    setup = await open_test_db(paths.db_file(BotName.DEV))
    try:
        await store_secret(setup, fakes.keystore, "google_api_key", FAKE_KEYS["google_api_key"])
        await register_projects(make_dev_deps(db=setup, paths=paths, fakes=fakes), fakes, paths, *projects)
    finally:
        await setup.dispose()
    observer = open_database(paths.db_file(BotName.DEV))
    here = World(paths=paths, root=root, fakes=fakes, telegram=FakeTelegram(fakes.clock), observer=observer)
    here.script = script
    try:
        yield here
    finally:
        await here.close()


# --- what reached the owner ------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OutboxLedger:
    """Every outbox row that posts a message, against what Telegram really created (NFR-REL-05/06).

    Texts are compared as sent (scrubbed and rendered as the sender does), as a multiset: `lost` are rows no post
    matches, `duplicates` posts beyond the rows that wanted them."""

    rows: int
    posted: int
    pending: tuple[str, ...]
    lost: tuple[str, ...]
    duplicates: tuple[str, ...]
    failed: tuple[tuple[str, str | None], ...]  # (dedupe_key, last_error_code) of rows that gave up

    @property
    def duplicate_rate(self) -> float:
        return len(self.duplicates) / max(1, self.posted)


_TAG: Final = re.compile(r"<[^>]*>")


def _visible(text: str) -> str:
    """What Telegram shows for a post: HTML tags dropped and entities read (plain text is unchanged by both)."""
    return html.unescape(_TAG.sub("", text))


def _as_sent(op: Op, payload_json: JsonValue) -> str:
    payload = outbox_domain.payload_from_json(op, payload_json)
    if op is Op.SEND_TEXT:
        return redact.scrubbed(outbox_domain.render(redact.scrubbed(payload.text or "")).text)
    return redact.scrubbed(payload.caption) if payload.caption else ""


async def outbox_ledger(here: World) -> OutboxLedger:
    async with here.read() as s:
        rows = await outbox_store.list_outbox_messages(s, BotName.DEV.value)
    wanted: Counter[str] = Counter()
    pending: list[str] = []
    failed: list[tuple[str, str | None]] = []
    counted = 0
    for row in rows:
        op = Op(row.op)
        if op not in SEND_OPS:
            continue
        status = Status(row.status)
        if status in {Status.PENDING, Status.SENDING}:
            pending.append(row.dedupe_key)
            continue
        if status not in {Status.SENT, Status.FAILED}:
            continue
        if status is Status.FAILED:
            failed.append((row.dedupe_key, row.last_error_code))
        counted += 1
        wanted[_as_sent(op, row.payload_json)] += 1
    got = Counter(post.text for post in here.telegram.posts)
    lost = tuple(text for text, n in (wanted - got).items() for _ in range(n))
    duplicates = tuple(text for text, n in (got - wanted).items() for _ in range(n))
    return OutboxLedger(
        rows=counted,
        posted=len(here.telegram.posts),
        pending=tuple(pending),
        lost=lost,
        duplicates=duplicates,
        failed=tuple(failed),
    )


def executor_texts(here: World) -> list[str]:
    """Every text the executor was sent, across processes, in order."""
    return [message.text for _, message, _ in here.fakes.agents.sends]


def tokens_in(text: str) -> list[str]:
    return TOKEN.findall(text)


def delivery_counts(here: World, tokens: Iterable[str]) -> dict[str, int]:
    """How many executor sends carried each owner message (its unique `#NN` token)."""
    sent = [set(tokens_in(text)) for text in executor_texts(here)]
    return {token: sum(1 for found in sent if token in found) for token in tokens}


def delivered_to(here: World) -> dict[str, set[str]]:
    """The projects whose executor session got each owner message (its token)."""
    agents = here.fakes.agents
    found: dict[str, set[str]] = defaultdict(set)
    for session_id, message, _ in agents.sends:
        for token in tokens_in(message.text):
            found[token].add(agents._sessions[session_id].spec.cwd.name)
    return found


async def until(check: Callable[[], object], *, timeout: float = 10.0, what: str = "condition") -> None:
    """Wait in real time (the clock does not move) for background work to reach a state."""
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(SETTLE_S)


# --- settling and the owner's view of the outcome --------------------------------------------------------------------
OFFER_START: Final = "Uzilib qolgan ishlar: "  # `S.HEALTH_RESTARTED`
OFFER_END: Final = " — davom ettiraymi?"
DRAIN_LIMIT_S: Final = 1800.0
_BACKGROUND_KINDS: Final = frozenset({Kind.HEARTBEAT.value})  # "Onlayn" edits recur forever; they are not work
_SETTLED: Final = frozenset({Status.SENT.value, Status.FAILED.value, Status.SUPERSEDED.value, Status.CANCELLED.value})


async def release_paused(here: World, *, chance: float = 1.0, rng: random.Random | None = None) -> None:
    """The agent finishes the turns it was busy with, or (`chance` < 1, drawn from `rng`) some of them."""
    agents = here.fakes.agents
    for session_id in list(agents._sessions):
        if agents.busy(session_id) and (chance >= 1.0 or (rng is not None and rng.random() < chance)):
            await agents.release(session_id)


async def unsettled(here: World) -> list[str]:
    """What is still to do: bursts not handled, turns not ended, inputs and messages not settled."""
    async with here.read() as s:
        bursts = (await s.execute(select(Burst.id, Burst.status))).all()
        turns = (await s.execute(select(Turn.id, Turn.project_id, Turn.status))).all()
        deliveries = (await s.execute(select(Delivery.id, Delivery.turn_id, Delivery.status))).all()
        outbox = (await s.scalars(select(OutboxMessage))).all()
        updates = (await s.execute(select(InboundUpdate.update_id, InboundUpdate.status))).all()
    burst_done = {item.value for item in BURST_TERMINAL}
    turn_done = {item.value for item in TURN_TERMINAL}
    open_deliveries = {DeliveryStatus.PENDING.value, DeliveryStatus.SENDING.value}
    return [
        *(f"burst {row.id} {row.status}" for row in bursts if row.status not in burst_done),
        *(f"turn {row.id} {row.project_id} {row.status}" for row in turns if row.status not in turn_done),
        *(
            f"delivery {row.id} of turn {row.turn_id} {row.status}"
            for row in deliveries
            if row.status in open_deliveries
        ),
        *(
            f"outbox {row.dedupe_key} p{row.priority} {row.status} next={row.next_attempt_at.time()}"
            for row in outbox
            if row.kind not in _BACKGROUND_KINDS and row.status not in _SETTLED
        ),
        *(f"update {row.update_id} {row.status}" for row in updates if row.status == "received"),
    ]


async def drain(here: World, *, limit_s: float = DRAIN_LIMIT_S, step: float = 0.5) -> float:
    """Let the running process finish everything (the agent completes what it was busy with). The virtual seconds it
    took; fails, listing what is left, past `limit_s`."""
    waited = 0.0
    while waited < limit_s:
        await release_paused(here)
        await here.advance(5.0, step=step)
        waited += 5.0
        if not await unsettled(here):
            return waited
    msg = f"the bot did not settle within {limit_s} s (virtual): {await unsettled(here)}"
    raise AssertionError(msg)


async def uncertain_tokens(here: World) -> frozenset[str]:
    """Owner messages whose delivery was told "bajarilmay qoldi — qayta yuboraymi?" (failed, and the notice sent)."""
    async with here.read() as s:
        failed = (await s.scalars(select(Delivery).where(Delivery.status == DeliveryStatus.FAILED.value))).all()
        told = set(
            (
                await s.scalars(
                    select(OutboxMessage.dedupe_key).where(
                        OutboxMessage.dedupe_key.like("notice:uncertain-%"), OutboxMessage.status == Status.SENT.value
                    )
                )
            ).all()
        )
    return frozenset(
        token for row in failed if f"notice:uncertain-{row.id}:0" in told for token in tokens_in(row.text or "")
    )


def offered_in(text: str) -> list[str]:
    """The projects a posted "Uzilib qolgan ishlar: A, B — davom ettiraymi?" lists."""
    listed = text.partition(OFFER_START)[2].partition(OFFER_END)[0]
    return [name.strip() for name in listed.split(",") if name.strip()]


def offered_projects(here: World) -> set[str]:
    """Every project a restart notice the owner received asked "davom ettiraymi?" about."""
    return {name for post in here.telegram.posted(OFFER_END) for name in offered_in(_visible(post.text))}


async def cut_off_turns(here: World) -> list[Turn]:
    """Turns a restart cut off (`turns.recover_turns`)."""
    async with here.read() as s:
        found = await s.scalars(select(Turn).where(Turn.error_code.in_([RESTART_UNTOLD, RESTART_TOLD])))
        return list(found.all())


def record(request: pytest.FixtureRequest, name: str, value: object) -> None:
    """A number of this run, for the JUnit XML (`-o junit_family=legacy --junitxml=…`); nothing else reads it."""
    request.node.user_properties.append((name, value))
