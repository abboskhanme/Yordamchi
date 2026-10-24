"""The whole dev bot on fakes, for the T34 acceptance (`test_dev_bot.py`).

`runtime.build_app` gets `DevDeps` built from `tests.factories.Fakes`, except the Messenger: the real
`TelegramMessenger` on the recording aiogram session, so every Bot API request is recorded as Telegram would get it
(and one carrying `reply_markup` raises). Updates go in through `Dispatcher.feed_update`, exactly as the poller feeds
them; the poller itself is left out. Every loop runs, on virtual time: `advance` moves the `FakeClock` in small steps
and lets the real event loop (aiosqlite threads included) catch up between them, so a latency is measured in virtual
seconds from the update to the request that answers it.

The data dir is short (`/tmp/yd…`): the control socket's path must fit a unix socket's 104 bytes.
"""

import asyncio
import inspect
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from aiogram.types import Update

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    FAKE_KEYS,
    OWNER_ID,
    TELEGRAM_DATE,
    Fakes,
    TelegramJson,
    make_dev_deps,
    make_project,
    make_project_folder,
    open_test_db,
    projects_root_of,
    store_secret,
    tg_chat,
    tg_user,
)
from tests.fakes.clock import FakeClock
from tests.fakes.telegram_session import RecordingSession, WireRequest
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.bots.dev import runtime
from yordamchi.bots.dev.runtime import DevApp
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.uow import Database, Session

STEP_S = 0.25  # virtual time per step: the outbox sender's idle poll
SETTLE_S = 0.01  # real time per step for the threads and tasks to catch up
START_HEAD = "0000000aaaa"


async def until(check: Callable[[], object], *, timeout: float = 5.0, what: str = "condition") -> None:
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


@dataclass
class DevBot:
    app: DevApp
    deps: DevDeps
    fakes: Fakes
    session: RecordingSession
    paths: Paths
    next_update: int = 100

    @property
    def clock(self) -> FakeClock:
        return self.fakes.clock

    # --- updates in -----------------------------------------------------------------------------------------------
    async def feed(self, raw: TelegramJson) -> None:
        bot = self.app.bot
        assert bot is not None
        await self.app.dispatcher.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    def _message(
        self, body: TelegramJson, *, user_id: int = OWNER_ID, chat_id: int = DEV_GROUP_ID, reply_to: int | None = None
    ) -> tuple[int, TelegramJson]:
        self.next_update += 1
        update_id = self.next_update
        message: TelegramJson = {
            "message_id": 5000 + update_id,
            "date": TELEGRAM_DATE,
            "chat": tg_chat(chat_id),
            "from": tg_user(user_id),
            **body,
        }
        if reply_to is not None:
            message["reply_to_message"] = {
                "message_id": reply_to,
                "date": TELEGRAM_DATE,
                "chat": tg_chat(chat_id),
                "text": "…",
            }
        return update_id, {"update_id": update_id, "message": message}

    async def say(
        self,
        text: str,
        *,
        user_id: int = OWNER_ID,
        chat_id: int = DEV_GROUP_ID,
        reply_to: int | None = None,
        forwarded_from: int | None = None,
    ) -> int:
        body: TelegramJson = {"text": text}
        if forwarded_from is not None:  # the owner forwards someone else's message
            body["forward_origin"] = {"type": "user", "date": TELEGRAM_DATE, "sender_user": tg_user(forwarded_from)}
        update_id, raw = self._message(body, user_id=user_id, chat_id=chat_id, reply_to=reply_to)
        await self.feed(raw)
        return update_id

    async def photo(self, data: bytes, *, caption: str | None = None) -> int:
        file_id = f"photo-{self.next_update + 1}"
        self.session.add_file(file_id, data, file_path=f"photos/{file_id}.jpg")
        sizes = [{"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 1280, "height": 800}]
        body: TelegramJson = {"photo": sizes}
        if caption is not None:
            body["caption"] = caption
        update_id, raw = self._message(body)
        await self.feed(raw)
        return update_id

    async def voice(self, data: bytes, *, duration: int = 3) -> int:
        file_id = f"voice-{self.next_update + 1}"
        self.session.add_file(file_id, data, file_path=f"voice/{file_id}.oga")
        voice = {"file_id": file_id, "file_unique_id": f"u{file_id}", "duration": duration, "mime_type": "audio/ogg"}
        update_id, raw = self._message({"voice": voice})
        await self.feed(raw)
        return update_id

    # --- requests out ---------------------------------------------------------------------------------------------
    def requests(self, *methods: str) -> list[WireRequest]:
        return [r for r in self.session.requests if not methods or r.api_method in methods]

    def texts(self) -> list[str]:
        """Every text posted or edited into, in order."""
        return [str(r.params.get("text", "")) for r in self.requests("sendMessage", "editMessageText")]

    def posted(self, needle: str) -> list[str]:
        return [text for text in self.texts() if needle in text]

    # --- time -----------------------------------------------------------------------------------------------------
    async def settle(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            await asyncio.sleep(SETTLE_S)

    async def advance(self, seconds: float, *, step: float = STEP_S) -> None:
        steps = max(1, round(seconds / step))
        for _ in range(steps):
            await self.clock.advance(seconds / steps)
            await self.settle()

    async def quiet(self) -> None:
        """A quiet minute: the outbox's 20-per-minute group window empties, so the next latency is the bot's own
        (NFR-CAP-02 would otherwise hold a burst of replies back)."""
        await self.advance(61, step=1.0)

    async def within(self, seconds: float, check: Callable[[], object], *, what: str) -> float:
        """Advance virtual time step by step until `check` holds; returns the virtual seconds it took. Fails past
        `seconds`."""
        elapsed = 0.0
        while True:
            await self.settle()
            outcome = check()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome:
                return elapsed
            if elapsed >= seconds:
                msg = f"{what}: not within {seconds} s (virtual)"
                raise AssertionError(msg)
            await self.clock.advance(STEP_S)
            elapsed += STEP_S


def short_paths() -> tuple[Paths, Path]:
    root = Path(tempfile.mkdtemp(prefix="yd", dir="/tmp"))
    home = root / "home"
    return Paths.defaults(home, data=root / "d", config=root / "c", logs=root / "l"), root


async def register_projects(deps: DevDeps, fakes: Fakes, paths: Paths, *names: str) -> dict[str, Path]:
    """Registered projects whose git starts clean at `START_HEAD`, pushing to GitHub."""
    found: dict[str, Path] = {}
    root = projects_root_of(paths)
    for name in names:
        folder = make_project_folder(root, name, git_dir=True, readme=f"# {name}\n\n{name} loyihasi.\n")
        async with deps.db.uow() as s:
            await make_project(s, name, at=fakes.clock.now(), path=folder)
        fakes.git.heads[folder] = START_HEAD
        fakes.git.remotes[(folder, "origin")] = f"git@github.com:abboskhanme/{name}.git"
        found[name] = folder
    return found


class Killed(Exception):
    """The process is gone: nothing it would still do reaches the DB."""


class MortalDatabase(Database):
    """The dying process's handle on the shared DB file. Once `dead`, every unit of work and read fails, so the
    clean-up code a task cancellation runs (a `finally`) writes nothing — as with `kill -9`, where none runs."""

    def __init__(self, db: Database) -> None:
        super().__init__(db.engine, readonly=False)
        self.dead = False

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[Session]:
        if self.dead:
            raise Killed
        async with super().uow() as s:
            yield s

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Session]:
        if self.dead:
            raise Killed
        async with super().read() as s:
            yield s


async def crash(app: DevApp) -> None:
    """`kill -9`: every task of the process is gone at once; no shutdown pass, no clean marker, no last write."""
    if isinstance(app.deps.db, MortalDatabase):
        app.deps.db.dead = True
    tasks = [*app.tasks.values(), *app.turns.tasks]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    app.tasks.clear()
    await asyncio.to_thread(app.socket.remove)


@asynccontextmanager
async def dev_bot(
    *, fakes: Fakes | None = None, paths: Paths | None = None, db: Database | None = None, crashes: bool = False
) -> AsyncIterator[DevBot]:
    """A started dev bot on fakes; on exit it stops as on SIGTERM, or (`crashes`) dies as on `kill -9`. A `db` the
    caller passes stays open (a restart reuses it); one made here is disposed, with its data dir."""
    fakes = fakes or Fakes()
    own_root: Path | None = None
    if paths is None:
        paths, own_root = short_paths()
    own_db = db is None
    db = db or await open_test_db(paths.db_file(BotName.DEV))
    handle = MortalDatabase(db) if crashes else db
    session = RecordingSession()
    messenger = TelegramMessenger(DEV_BOT_TOKEN, session=session)
    deps = replace(make_dev_deps(db=handle, paths=paths, fakes=fakes), messenger=messenger)
    if own_db:
        await store_secret(db, fakes.keystore, "google_api_key", FAKE_KEYS["google_api_key"])
    app = runtime.build_app(deps, bot=messenger.bot, version="2026.10.24-test", closers=(messenger.close,))
    bot = DevBot(app=app, deps=deps, fakes=fakes, session=session, paths=paths)
    try:
        await app.start(polling=False)
        yield bot
    finally:
        await (crash(app) if crashes else app.stop())
        if own_db:
            await db.dispose()
        if own_root is not None:
            shutil.rmtree(own_root, ignore_errors=True)
