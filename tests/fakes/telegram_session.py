"""`RecordingSession`: an aiogram `BaseSession` that records the wire request and never opens a socket.

It builds each request exactly as `AiohttpSession.build_form_data` does (`method.model_dump()` → `prepare_value`,
empty values dropped), so `params` is what Telegram would receive. Any request carrying `reply_markup` raises
`ReplyMarkupSent` at once: buttons, keyboards and menus are a test failure at the outbound API layer (D8, UC-12).

Answers go through aiogram's own `check_response`, so status codes and bodies from `tests/fixtures/telegram/`
become the same exceptions a live server would cause. Unscripted calls get a well-formed success built from the
request (increasing message ids per chat, as Telegram does).

Forum topics (D4 revised 2026-10-26) are Telegram's side in `ForumTopics`, which several sessions may share (the fault
harness's processes restart): `createForumTopic` opens one in a forum and is refused in a group that is none, or
without the "Manage topics" right; a send into a deleted topic is "message thread not found", into a closed one
`TOPIC_CLOSED`; `reopenForumTopic` of an open topic is `TOPIC_NOT_MODIFIED`. A message posted into a topic carries its
`message_thread_id`, as Telegram's does.
"""

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InputFile

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
BOT_USER: Final[dict[str, object]] = {"id": 7000000001, "is_bot": True, "first_name": "Yordamchi Dev"}
FIXED_DATE: Final = 1761292800  # 2025-10-24T08:00:00Z; Telegram dates are Unix seconds

type Body = dict[str, Any]


class ReplyMarkupSent(AssertionError):
    """A request carried `reply_markup` (D8, UC-12)."""


@dataclass(frozen=True, slots=True)
class Answer:
    """One HTTP answer: the status and the raw body text (JSON, or not, as a broken proxy might send)."""

    status: int
    content: str

    @classmethod
    def json(cls, status: int, body: Body) -> "Answer":
        return cls(status, json.dumps(body))

    @classmethod
    def fixture(cls, name: str, **overrides: object) -> "Answer":
        """`tests/fixtures/telegram/responses/<name>.json`: `{"status": …, "body": {…}}` or `{"status", "raw"}`.
        `overrides` replace keys of `body.parameters` (e.g. a shorter `retry_after`)."""
        data = json.loads((FIXTURES / "responses" / f"{name}.json").read_text(encoding="utf-8"))
        if "raw" in data:
            return cls(int(data["status"]), str(data["raw"]))
        body: Body = data["body"]
        if overrides:
            body = {**body, "parameters": {**body.get("parameters", {}), **overrides}}
        return cls.json(int(data["status"]), body)


# A scripted answer, or a factory for an exception aiogram itself would raise (e.g. `TelegramNetworkError`).
type Reply = Answer | Callable[[TelegramMethod[Any]], BaseException]

POSTING_METHODS: Final = frozenset({"sendMessage", "sendPhoto", "sendDocument", "sendVoice", "sendVideo"})
TOPIC_SEND_METHODS: Final = frozenset({*POSTING_METHODS, "sendChatAction"})


def bad_request(description: str) -> Answer:
    """Telegram's 400 with its own description (aiogram raises `TelegramBadRequest`)."""
    return Answer.json(400, {"ok": False, "error_code": 400, "description": f"Bad Request: {description}"})


@dataclass(slots=True)
class ForumTopics:
    """Telegram's side of the forum topics: is the group a forum, may the bot manage topics, which topics are open,
    closed or deleted. Thread ids increase from 500, as in a group whose first messages came before its topics."""

    forum: bool = False
    can_manage: bool = True
    next_thread: int = 500
    open: dict[tuple[int, int], str] = field(default_factory=dict)  # (chat, thread) → name
    closed: set[tuple[int, int]] = field(default_factory=set)
    deleted: set[tuple[int, int]] = field(default_factory=set)
    created: list[tuple[int, int, str, int]] = field(default_factory=list)  # (chat, thread, name, icon colour)

    def delete(self, chat_id: int, thread_id: int) -> None:
        """The owner deleted the topic."""
        self.open.pop((chat_id, thread_id), None)
        self.closed.discard((chat_id, thread_id))
        self.deleted.add((chat_id, thread_id))

    def close(self, chat_id: int, thread_id: int) -> None:
        """The owner closed the topic."""
        self.closed.add((chat_id, thread_id))

    def owner_topic(self, chat_id: int, name: str) -> int:
        """A topic the owner opened himself: the bot never made it."""
        self.next_thread += 1
        self.open[(chat_id, self.next_thread)] = name
        return self.next_thread

    def refusal(self, api_method: str, params: Mapping[str, Any]) -> Answer | None:  # noqa: PLR0911  # one per rule
        """The 400 Telegram gives this request, or None when it goes through."""
        chat_id = params.get("chat_id")
        thread_id = params.get("message_thread_id")
        key = (int(chat_id), int(thread_id)) if isinstance(chat_id, int) and isinstance(thread_id, int) else None
        if api_method == "createForumTopic":
            if not self.forum:
                return bad_request("the chat is not a forum")
            return None if self.can_manage else bad_request("not enough rights to create a topic")
        if api_method == "reopenForumTopic":
            if key is None or key in self.deleted or key not in self.open:
                return bad_request("TOPIC_ID_INVALID")
            return None if key in self.closed else bad_request("TOPIC_NOT_MODIFIED")
        if api_method in TOPIC_SEND_METHODS and key is not None:
            if key in self.deleted or (self.forum and key not in self.open):
                return bad_request("message thread not found")
            if key in self.closed and api_method in POSTING_METHODS:
                return bad_request("TOPIC_CLOSED")
        return None

    def open_topic(self, params: Mapping[str, Any]) -> dict[str, object]:
        chat_id, name, color = int(params["chat_id"]), str(params["name"]), int(params.get("icon_color", 0))
        self.next_thread += 1
        self.open[(chat_id, self.next_thread)] = name
        self.created.append((chat_id, self.next_thread, name, color))
        return {"message_thread_id": self.next_thread, "name": name, "icon_color": color}

    def reopen(self, params: Mapping[str, Any]) -> bool:
        self.closed.discard((int(params["chat_id"]), int(params["message_thread_id"])))
        return True


@dataclass(frozen=True, slots=True)
class WireRequest:
    api_method: str
    params: dict[str, Any]
    uploads: dict[str, str]  # attach key → file name
    timeout: int | None


@dataclass(slots=True)
class StoredFile:
    data: bytes
    file_path: str
    reported_size: int | None


class RecordingSession(BaseSession):
    def __init__(self, *, topics: ForumTopics | None = None) -> None:
        super().__init__()
        self.topics = topics if topics is not None else ForumTopics()
        self.requests: list[WireRequest] = []
        self.streamed: list[str] = []  # file paths fetched from /file/bot<token>/…
        self.hang = False  # every request waits forever (the caller's timeout must end it)
        self._scripted: defaultdict[str, deque[Reply]] = defaultdict(deque)
        self._stream_failures: deque[BaseException] = deque()
        self._files: dict[str, StoredFile] = {}
        self._by_path: dict[str, StoredFile] = {}
        self._next_id: defaultdict[int, int] = defaultdict(lambda: 1000)

    # --- scripting ---
    def script(self, api_method: str, *replies: Reply) -> None:
        """Answers for the next calls of `api_method`, in order; later calls get the default success."""
        self._scripted[api_method].extend(replies)

    def add_file(self, file_id: str, data: bytes, *, file_path: str, reported_size: int | None = -1) -> None:
        """A file getFile can describe and the file endpoint can serve. `reported_size=None` omits `file_size`."""
        stored = StoredFile(data, file_path, len(data) if reported_size == -1 else reported_size)
        self._files[file_id] = stored
        self._by_path[file_path] = stored

    def fail_stream(self, *errors: BaseException) -> None:
        self._stream_failures.extend(errors)

    def calls(self, api_method: str | None = None) -> list[WireRequest]:
        return [r for r in self.requests if api_method is None or r.api_method == api_method]

    def assert_no_reply_markup(self) -> None:
        offenders = [r.api_method for r in self.requests if "reply_markup" in r.params]
        assert not offenders, f"reply_markup sent by {offenders}"

    # --- BaseSession ---
    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        files: dict[str, InputFile] = {}
        params: dict[str, Any] = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=files)
            if prepared:
                params[key] = _decoded(prepared)
        api_method = method.__api_method__
        if "reply_markup" in params:
            raise ReplyMarkupSent(f"{api_method} carried reply_markup (UC-12)")
        uploads = {key: file.filename or key for key, file in files.items()}
        self.requests.append(WireRequest(api_method, params, uploads, timeout))
        if self.hang:
            await asyncio.Event().wait()

        queue = self._scripted[api_method]
        if queue:
            reply: Reply = queue.popleft()
        elif (refused := self.topics.refusal(api_method, params)) is not None:
            reply = refused
        else:
            reply = Answer.json(200, {"ok": True, "result": self._success(params, api_method)})
        if not isinstance(reply, Answer):
            raise reply(method)
        response = self.check_response(bot=bot, method=method, status_code=reply.status, content=reply.content)
        return cast(TelegramType, response.result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        path = url.split("/", 5)[-1]  # https://api.telegram.org/file/bot<token>/<file_path>
        self.streamed.append(path)
        if self._stream_failures:
            raise self._stream_failures.popleft()
        data = self._by_path[path].data
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    # --- default answers, shaped like the Bot API objects ---
    def _success(self, params: dict[str, Any], api_method: str) -> object:
        if api_method == "getFile":
            return self._file_object(str(params["file_id"]))
        if api_method == "createForumTopic":
            return self.topics.open_topic(params)
        if api_method == "reopenForumTopic":
            return self.topics.reopen(params)
        if api_method in POSTING_METHODS:
            chat_id = int(params["chat_id"])
            self._next_id[chat_id] += 1
            return self._message(chat_id, self._next_id[chat_id], api_method, params)
        if api_method == "editMessageText":
            chat_id = int(params["chat_id"])
            return {**self._message(chat_id, int(params["message_id"]), "sendMessage", params), "edit_date": FIXED_DATE}
        return True

    def _file_object(self, file_id: str) -> dict[str, object]:
        stored = self._files[file_id]
        result: dict[str, object] = {"file_id": file_id, "file_unique_id": f"u-{file_id[-8:]}"}
        if stored.reported_size is not None:
            result["file_size"] = stored.reported_size
        result["file_path"] = stored.file_path
        return result

    @staticmethod
    def _message(chat_id: int, message_id: int, api_method: str, params: dict[str, Any]) -> dict[str, object]:
        chat: dict[str, object] = (
            {"id": chat_id, "type": "supergroup", "title": "Yordamchi Dev"}
            if chat_id < 0
            else {"id": chat_id, "type": "private", "first_name": "Owner"}
        )
        message: dict[str, object] = {"message_id": message_id, "from": BOT_USER, "chat": chat, "date": FIXED_DATE}
        if (thread_id := params.get("message_thread_id")) is not None:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True
        unique = f"{chat_id}-{message_id}"
        if api_method == "sendMessage":
            message["text"] = params["text"]
        elif api_method == "sendPhoto":
            message["photo"] = [
                {"file_id": f"AgAD-{unique}-s", "file_unique_id": f"s{unique}", "width": 90, "height": 60},
                {"file_id": f"AgAD-{unique}-m", "file_unique_id": f"m{unique}", "width": 1280, "height": 853},
            ]
        elif api_method == "sendDocument":
            message["document"] = {"file_id": f"BQAD-{unique}", "file_unique_id": f"d{unique}", "file_name": "f"}
        elif api_method == "sendVoice":
            message["voice"] = {"file_id": f"AwAD-{unique}", "file_unique_id": f"v{unique}", "duration": 3}
        elif api_method == "sendVideo":
            message["video"] = {
                "file_id": f"BAAD-{unique}",
                "file_unique_id": f"w{unique}",
                "width": 1280,
                "height": 720,
                "duration": 5,
            }
        if "caption" in params:
            message["caption"] = params["caption"]
        return message


def _decoded(value: object) -> object:
    """`prepare_value` JSON-encodes non-strings; decode them back so tests compare structures."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
