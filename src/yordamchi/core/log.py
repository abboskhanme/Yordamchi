"""Structured JSON-lines logging; every record passes `core.redact` (CONVENTIONS §7, SPEC §9.2).

    log = get_log(__name__)
    log.info("turn_finished", project="BonviHR", turn_id=17, duration_ms=8123)

Events are snake_case past-tense literals (A8). Fields are ids, codes, counts, durations and versions — never
content (message text, transcripts, prompts, model output, tool I/O, env values). String fields are scrubbed anyway.
Records from libraries are kept as `library_log` with their scrubbed message.

Handlers run on a `QueueListener` thread, so a slow disk never blocks the event loop.
"""

import json
import logging
import queue
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Final, TextIO

from yordamchi.core.paths import ProcessName
from yordamchi.core.redact import LOG_FIELDS_ATTR, scrubbed

type LogScalar = str | int | float | bool | None
type LogValue = LogScalar | Sequence[LogScalar]

# SPEC §9.2: 20 MB × 4 files per bot (the live file plus .1 .2 .3).
BOT_LOG_MAX_BYTES: Final = 20 * 1024 * 1024
BOT_LOG_BACKUPS: Final = 3
_RESERVED: Final = frozenset({"ts", "level", "process", "version", "logger", "event", "exc"})


class Log:
    """A logger whose calls take an event name plus keyword fields."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, event: str, /, **fields: LogValue) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, /, **fields: LogValue) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, /, **fields: LogValue) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, /, **fields: LogValue) -> None:
        self._emit(logging.ERROR, event, fields)

    def exception(self, event: str, /, **fields: LogValue) -> None:
        """ERROR with the current exception's (scrubbed) traceback; call it inside `except`."""
        self._emit(logging.ERROR, event, fields, exc_info=True)

    def _emit(self, level: int, event: str, fields: Mapping[str, LogValue], *, exc_info: bool = False) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, extra={LOG_FIELDS_ATTR: dict(fields)}, exc_info=exc_info)


def get_log(name: str) -> Log:
    return Log(logging.getLogger(name))


def _clean(value: object) -> object:
    if isinstance(value, str):
        return scrubbed(value)
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrubbed(str(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, process, version, logger, event, fields…, exc."""

    def __init__(self, *, process: ProcessName, version: str) -> None:
        super().__init__()
        self._process = process
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, LOG_FIELDS_ATTR, None)
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "process": self._process,
            "version": self._version,
            "logger": record.name,
        }
        if isinstance(fields, dict):
            out["event"] = str(record.msg)
            for key, value in fields.items():
                out[f"field_{key}" if key in _RESERVED else str(key)] = _clean(value)
        else:
            out["event"] = "library_log"
            out["message"] = scrubbed(record.getMessage())
        if record.exc_info:
            out["exc"] = scrubbed(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class LogRuntime:
    """Handle returned by `configure`; `stop()` drains the queue and closes the files (call it on shutdown)."""

    listener: QueueListener
    handler: QueueHandler
    targets: tuple[logging.Handler, ...]

    def stop(self) -> None:
        self.listener.stop()
        logging.getLogger().removeHandler(self.handler)
        for target in self.targets:
            target.close()


def configure(
    *,
    process: ProcessName,
    version: str,
    file: Path | None = None,
    stream: TextIO | None = None,
    level: int = logging.INFO,
    max_bytes: int = BOT_LOG_MAX_BYTES,
    backup_count: int = BOT_LOG_BACKUPS,
) -> LogRuntime:
    """Route the root logger through redaction into a rotating JSONL file and/or a stream. Replaces old handlers."""
    targets: list[logging.Handler] = []
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        targets.append(RotatingFileHandler(file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"))
    if stream is not None:
        targets.append(logging.StreamHandler(stream))
    for target in targets:
        # The record already holds the finished JSON line (QueueHandler.prepare formats it on the caller's thread).
        target.setFormatter(logging.Formatter("%(message)s"))

    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = QueueHandler(records)
    handler.setFormatter(JsonFormatter(process=process, version=version))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    listener = QueueListener(records, *targets, respect_handler_level=True)
    listener.start()
    return LogRuntime(listener, handler, tuple(targets))
