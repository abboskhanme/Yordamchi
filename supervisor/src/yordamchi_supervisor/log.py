"""JSON-lines log at `~/Library/Logs/Yordamchi/supervisor.jsonl`, rotated at 10 MB × 2 (SPEC §9.2), always scrubbed.

One line is `{"ts", "level", "event", ...fields}`. Fields carry ids, codes, counts and durations, never message
content. `Scrubber` masks every Telegram bot token, whether it matches the pattern or is the value the supervisor has
read from the Keychain item `dev.env` names. It runs on every line and on uncaught tracebacks (`install_excepthook`).
Uncaught tracebacks go to stderr, which is the launchd log.
"""

import json
import logging
import logging.handlers
import re
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, TextIO

MASK: Final = "••••"
MAX_BYTES: Final = 10 * 1024 * 1024
BACKUP_COUNT: Final = 1  # supervisor.jsonl + supervisor.jsonl.1 = 10 MB × 2
_BOT_TOKEN: Final = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_MIN_KNOWN: Final = 6


class Scrubber:
    """Masks bot tokens: anything shaped like one, plus every exact value registered with `add`."""

    def __init__(self) -> None:
        self._known: set[str] = set()

    def add(self, secret: str) -> None:
        if len(secret) >= _MIN_KNOWN:
            self._known.add(secret)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._known, key=len, reverse=True):
            text = text.replace(secret, MASK)
        return _BOT_TOKEN.sub(MASK, text)


type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JsonLog:
    def __init__(
        self,
        path: Path | None,
        scrubber: Scrubber,
        *,
        stream: TextIO | None = None,
        clock: Clock = _utc_now,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
    ) -> None:
        self._scrub = scrubber
        self._clock = clock
        # A private Logger, not registered with logging.getLogger: tests build many and none of them leak handlers.
        self._logger = logging.Logger("yordamchi_supervisor", logging.DEBUG)  # noqa: LOG001
        self._handlers: list[logging.Handler] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._handlers.append(
                logging.handlers.RotatingFileHandler(
                    path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
                )
            )
        if stream is not None:
            self._handlers.append(logging.StreamHandler(stream))
        for handler in self._handlers:
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def line(self, level: str, event: str, fields: dict[str, object]) -> str:
        record: dict[str, object] = {"ts": self._clock().isoformat(timespec="seconds"), "level": level, "event": event}
        record.update(fields)
        return self._scrub(json.dumps(record, ensure_ascii=False, default=str, sort_keys=False))

    def _emit(self, level: int, name: str, event: str, fields: dict[str, object]) -> None:
        self._logger.log(level, "%s", self.line(name, event, fields))

    def debug(self, event: str, **fields: object) -> None:
        self._emit(logging.DEBUG, "debug", event, fields)

    def info(self, event: str, **fields: object) -> None:
        self._emit(logging.INFO, "info", event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._emit(logging.WARNING, "warning", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._emit(logging.ERROR, "error", event, fields)

    def close(self) -> None:
        for handler in self._handlers:
            self._logger.removeHandler(handler)
            handler.close()
        self._handlers.clear()


def install_excepthook(scrubber: Scrubber, stream: TextIO | None = None) -> None:
    """An uncaught exception is printed scrubbed: urllib errors can carry `api.telegram.org/bot<token>/…`."""

    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        out = stream if stream is not None else sys.stderr
        out.write(scrubber("".join(traceback.format_exception(kind, value, tb))))
        out.flush()

    sys.excepthook = hook
