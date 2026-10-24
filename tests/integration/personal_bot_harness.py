"""The whole personal bot on fakes, for the T77 acceptance (`test_personal_bot.py`), in the shape of
`dev_bot_harness.py`.

`runtime.build_app` gets `PersonalDeps` built from `tests.factories.Fakes`, except the Messenger: the real
`TelegramMessenger` on the recording aiogram session, so every Bot API request is recorded as Telegram would get it
(and one carrying `reply_markup` raises). dev.db is a real file the test writes digests into, opened by the bot
read-only (`mode=ro`, `query_only`), as `bots/personal/app.py` opens it. Updates go in through
`Dispatcher.feed_update` in the owner's DM, exactly as the poller feeds them; the poller itself is left out. Every
loop runs, on virtual time: `advance` moves the `FakeClock` in small steps and lets the real event loop (aiosqlite
threads included) catch up between them.

The data dir is short (`/tmp/yp…`): the control socket's path must fit a unix socket's 104 bytes.
"""

import asyncio
import inspect
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from aiogram.types import Update

from tests.factories import (
    FAKE_KEYS,
    OWNER_ID,
    PERSONAL_BOT_TOKEN,
    TELEGRAM_DATE,
    Fakes,
    TelegramJson,
    make_personal_deps,
    open_test_db,
    store_secret,
    tg_chat,
    tg_user,
)
from tests.fakes.clock import FakeClock
from tests.fakes.telegram_session import RecordingSession, WireRequest
from tests.integration.dev_bot_harness import visible
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.bots.personal import runtime
from yordamchi.bots.personal.runtime import PersonalApp
from yordamchi.core.paths import BotName, Paths
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.outbox.domain import plain_text

STEP_S = 0.25  # virtual time per step: the outbox sender's idle poll
SETTLE_S = 0.01  # real time per step for the threads and tasks to catch up
VERSION = "2026.10.24-test"


@dataclass
class PersonalBot:
    app: PersonalApp
    deps: PersonalDeps
    fakes: Fakes
    session: RecordingSession
    paths: Paths
    dev_db: Database  # the dev bot's own read-write handle: what the test writes digests with
    next_update: int = 100

    @property
    def clock(self) -> FakeClock:
        return self.fakes.clock

    # --- updates in -----------------------------------------------------------------------------------------------
    async def feed(self, raw: TelegramJson) -> None:
        bot = self.app.bot
        assert bot is not None
        await self.app.dispatcher.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    def _message(self, body: TelegramJson, *, user_id: int = OWNER_ID, chat_id: int = OWNER_ID) -> TelegramJson:
        self.next_update += 1
        update_id = self.next_update
        message: TelegramJson = {
            "message_id": 5000 + update_id,
            "date": TELEGRAM_DATE,
            "chat": tg_chat(chat_id),
            "from": tg_user(user_id),
            **body,
        }
        return {"update_id": update_id, "message": message}

    async def say(self, text: str, *, user_id: int = OWNER_ID, chat_id: int = OWNER_ID) -> int:
        """A text in the owner's DM (a private chat's id is the user's id)."""
        raw = self._message({"text": text}, user_id=user_id, chat_id=chat_id)
        await self.feed(raw)
        return self.next_update

    async def photo(self, data: bytes, *, caption: str | None = None) -> int:
        file_id = f"photo-{self.next_update + 1}"
        self.session.add_file(file_id, data, file_path=f"photos/{file_id}.jpg")
        sizes = [{"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 1280, "height": 800}]
        body: TelegramJson = {"photo": sizes}
        if caption is not None:
            body["caption"] = caption
        await self.feed(self._message(body))
        return self.next_update

    async def voice(self, data: bytes, *, duration: int = 3) -> int:
        file_id = f"voice-{self.next_update + 1}"
        self.session.add_file(file_id, data, file_path=f"voice/{file_id}.oga")
        voice = {"file_id": file_id, "file_unique_id": f"u{file_id}", "duration": duration, "mime_type": "audio/ogg"}
        await self.feed(self._message({"voice": voice}))
        return self.next_update

    # --- requests out ---------------------------------------------------------------------------------------------
    def requests(self, *methods: str) -> list[WireRequest]:
        return [r for r in self.session.requests if not methods or r.api_method in methods]

    def texts(self) -> list[str]:
        """Every text posted, in order (captions of files included), as the owner reads it: an HTML text without its
        tags (the outbox renders catalog markup since the formatting pass)."""
        sent = self.requests("sendMessage", "editMessageText", "sendPhoto", "sendDocument", "sendVideo")
        return [visible(r) if r.params.get("text") else str(r.params.get("caption") or "") for r in sent]

    def posted(self, needle: str) -> list[str]:
        """The texts that hold `needle`, read the same way (a catalog line's markers stripped)."""
        wanted = plain_text(needle)
        return [text for text in self.texts() if wanted in text]

    def methods(self) -> list[str]:
        return [r.api_method for r in self.session.requests]

    # --- time -----------------------------------------------------------------------------------------------------
    async def started(self) -> None:
        """The start's own message is out (the restart notice: the harness writes no clean marker) and the chat's
        send window is free again, so what follows is the owner's."""
        await self.within(5, lambda: bool(self.posted("Qayta ishga tushdim")), what="the start notice")
        await self.advance(2)

    async def settle(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            await asyncio.sleep(SETTLE_S)

    async def advance(self, seconds: float, *, step: float = STEP_S) -> None:
        steps = max(1, round(seconds / step))
        for _ in range(steps):
            await self.clock.advance(seconds / steps)
            await self.settle()

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
    root = Path(tempfile.mkdtemp(prefix="yp", dir="/tmp"))
    home = root / "home"
    return Paths.defaults(home, data=root / "d", config=root / "c", logs=root / "l"), root


@asynccontextmanager
async def personal_bot(*, fakes: Fakes | None = None, speech_key: bool = True) -> AsyncIterator[PersonalBot]:
    """A started personal bot on fakes, stopped as on SIGTERM on exit. `speech_key`: a Google key is in its vault
    (the owner pasted it once), so STT, TTS and the video reader have a provider."""
    fakes = fakes or Fakes()
    paths, root = short_paths()
    db = await open_test_db(paths.db_file(BotName.PERSONAL))
    dev_db = await open_test_db(paths.db_file(BotName.DEV))
    bridge_db = open_database(paths.db_file(BotName.DEV), readonly=True)
    session = RecordingSession()
    messenger = TelegramMessenger(PERSONAL_BOT_TOKEN, session=session)
    deps = replace(make_personal_deps(db=db, paths=paths, fakes=fakes, peer_db=bridge_db), messenger=messenger)
    if speech_key:
        await store_secret(db, fakes.keystore, "google_api_key", FAKE_KEYS["google_api_key"], bot=BotName.PERSONAL)
    app = runtime.build_app(deps, bot=messenger.bot, version=VERSION, closers=(messenger.close,))
    bot = PersonalBot(app=app, deps=deps, fakes=fakes, session=session, paths=paths, dev_db=dev_db)
    try:
        await app.start(polling=False)
        yield bot
    finally:
        await app.stop()
        for handle in (bridge_db, dev_db, db):
            await handle.dispose()
        shutil.rmtree(root, ignore_errors=True)
