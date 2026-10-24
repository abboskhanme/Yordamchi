"""Owner alerts through the Telegram Bot API, stdlib `urllib` only (SPEC §2.2, CONVENTIONS §12).

`DEV_GROUP_ID` and the token's Keychain reference are read from the 0600 `DATA/dev.env` at send time, and the token
itself from the macOS Keychain (`keychain.py`), so a re-install or a rotation is picked up without a restart. A
plaintext token in `dev.env`, or a reference to any other item, is refused (owner requirement 2026-10-24): nothing is
sent until the installer has fixed the file. The token is registered with the log scrubber the moment it is read, and
no URL, request or exception text is ever logged: only the alert kind, an HTTP status or an exception class name.

Alerts are queued in memory. A network drop (the Mac just woke, Wi-Fi is off) keeps the queue and the next tick
retries; a definite refusal from Telegram (a bad token or chat, HTTP 400/401/403/404) drops the alert and logs it.
Messages are plain text: no `parse_mode`, no `reply_markup`.
"""

import http.client
import json
import stat
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.clock import TASHKENT, Clock
from yordamchi_supervisor.keychain import DEV_TOKEN_REFERENCE, REFERENCE_PREFIX, KeychainError, TokenSource
from yordamchi_supervisor.log import JsonLog, Scrubber

API_BASE: Final = "https://api.telegram.org"
SEND_TIMEOUT_S: Final = 10.0
MAX_QUEUE: Final = 20
MAX_TEXT: Final = 4000  # Telegram allows 4096 characters

type Opener = Callable[[urllib.request.Request, float], int]
"""Sends the request and returns the HTTP status. Raises `urllib.error.HTTPError` / `URLError` / `OSError`."""


def urllib_opener(request: urllib.request.Request, timeout: float) -> int:
    # The URL is always API_BASE (https), built in `Alerter._request`, never from input.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return int(response.status)


class AlertConfigError(Exception):
    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class AlertTarget:
    token: str = field(repr=False)
    chat_id: str


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines, the same format `core/config.py` reads: `#` comments, optional `export `, optional matching
    quotes, no escaping and no `$VAR` expansion. Malformed lines are skipped (the app refuses them at startup)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").strip().partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_alert_target(path: Path, keychain: TokenSource) -> AlertTarget:
    """The dev group from `dev.env` and the dev token from the Keychain item it names. Refuses a file that group or
    others can read, a plaintext token, and a reference to any item but `uz.yordamchi/dev-bot-token`."""
    try:
        info = path.stat()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AlertConfigError("env_missing") from None
    except OSError:
        raise AlertConfigError("env_unreadable") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AlertConfigError("env_mode_unsafe")
    values = parse_env_file(text)
    reference, chat = values.get("DEV_BOT_TOKEN", ""), values.get("DEV_GROUP_ID", "")
    if not reference or not chat or reference.startswith("<") or chat.startswith("<"):
        raise AlertConfigError("env_incomplete")
    if not reference.startswith(REFERENCE_PREFIX):
        raise AlertConfigError("env_token_plaintext")
    if reference != DEV_TOKEN_REFERENCE:
        raise AlertConfigError("env_token_reference_refused")
    try:
        token = keychain.dev_token()
    except KeychainError as exc:
        raise AlertConfigError(exc.problem) from None
    return AlertTarget(token=token, chat_id=chat)


@dataclass(frozen=True, slots=True)
class _Pending:
    kind: str
    text: str
    at: datetime


class Alerter:
    def __init__(
        self,
        env_file: Path,
        log: JsonLog,
        scrubber: Scrubber,
        clock: Clock,
        *,
        keychain: TokenSource,
        opener: Opener = urllib_opener,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        self._env_file = env_file
        self._keychain = keychain
        self._log = log
        self._scrub = scrubber
        self._clock = clock
        self._opener = opener
        self._queue: deque[_Pending] = deque(maxlen=max_queue)
        self._config_problem: str | None = None

    @property
    def pending(self) -> int:
        return len(self._queue)

    def send(self, kind: str, text: str) -> None:
        """Queue one alert and try to deliver everything queued. `kind` is a code, logged; `text` is never logged."""
        if len(self._queue) == self._queue.maxlen:
            self._log.warning("alert_dropped_queue_full", kind=self._queue[0].kind)
        self._queue.append(_Pending(kind=kind, text=self._scrub(text), at=self._clock.now()))
        self._log.info("alert_queued", kind=kind)
        self.flush()

    def flush(self) -> None:
        if not self._queue:
            return
        try:
            target = read_alert_target(self._env_file, self._keychain)
        except AlertConfigError as exc:
            if exc.problem != self._config_problem:
                self._log.error("alert_config_problem", problem=exc.problem, queued=len(self._queue))
            self._config_problem = exc.problem
            return
        self._config_problem = None
        self._scrub.add(target.token)
        while self._queue:
            item = self._queue[0]
            outcome = self._deliver(target, item)
            if outcome == "retry":
                return
            self._queue.popleft()

    def _deliver(self, target: AlertTarget, item: _Pending) -> str:
        try:
            status = self._opener(self._request(target, item), SEND_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            self._log.warning("alert_deferred", kind=item.kind, error=type(exc).__name__, queued=len(self._queue))
            return "retry"
        if 200 <= status < 300:
            self._log.info("alert_sent", kind=item.kind)
            return "done"
        if status == 429 or status >= 500:
            self._log.warning("alert_deferred", kind=item.kind, status=status, queued=len(self._queue))
            return "retry"
        self._log.error("alert_rejected", kind=item.kind, status=status)
        return "dropped"

    def _request(self, target: AlertTarget, item: _Pending) -> urllib.request.Request:
        when = item.at.astimezone(TASHKENT).strftime("%H:%M")
        text = S.ALERT(time=when, text=item.text)[:MAX_TEXT]
        body = json.dumps({"chat_id": target.chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(  # noqa: S310 - fixed https base URL
            f"{API_BASE}/bot{target.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
