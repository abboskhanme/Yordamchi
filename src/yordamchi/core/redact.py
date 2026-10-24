"""Secret detection and masking for every log line, outbound message and audit row (SPEC §4.5, STACK §4.3).

Three jobs:
1. `find_secrets(text)` detects keys for intake (`shared/inbox` moves each hit into the vault).
2. `scrubbed(text)` masks the §4.5 patterns, Telegram bot URLs and every value the vault currently holds
   (`set_known_secrets`, rebuilt by the vault on change) as `••••last4`.
3. `install_process_hooks()` wraps stdout/stderr and replaces `sys.excepthook`, `threading.excepthook` and
   `sys.unraisablehook`; `install_asyncio_handler(loop)` does the same for the event loop. aiohttp/aiogram tracebacks
   carry `api.telegram.org/bot<token>/…`, so these run before aiogram or the Agent SDK is imported (SPEC §2.5).

Writes that bypass Python (a child process inheriting fd 2) are not covered; children get their own pipes.
"""

import asyncio
import logging
import math
import re
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final, TextIO, cast

from yordamchi.core.text import BotText

MASK: Final = "••••"
# The JSON log formatter (core.log) reads structured fields from this LogRecord attribute.
LOG_FIELDS_ATTR: Final = "yd_fields"
# Vault values shorter than this are not masked by value (they would mask ordinary words); patterns still apply.
MIN_KNOWN_SECRET_LEN: Final = 6
_TAIL_MIN_LEN: Final = 12


@dataclass(frozen=True, slots=True)
class SecretPattern:
    """One detector. Earlier patterns win where matches overlap, so specific formats come before generic ones."""

    kind: str
    regex: re.Pattern[str]
    group: int = 0
    refused: bool = False  # a Claude login token: replaced by "[rad etildi]" and never stored (SPEC §3.2.1)
    vaultable: bool = True  # False: masked in output, but not a key to store (a bot URL path)
    show_tail: bool = True  # False: the last four characters say nothing (PEM armour, a callback URL)
    accept: Callable[[str], bool] | None = None


@dataclass(frozen=True, slots=True)
class SecretHit:
    kind: str
    start: int
    end: int
    value: str = field(repr=False)
    refused: bool = False
    vaultable: bool = True


def shannon_entropy(value: str) -> float:
    """Bits per character."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _looks_random(value: str) -> bool:
    """SPEC §4.5 generic rule: ≥ 24 chars, Shannon ≥ 4.0, at least two character classes."""
    classes = sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(not c.isalnum() for c in value),
        )
    )
    return len(value) >= 24 and classes >= 2 and shannon_entropy(value) >= 4.0


_GENERIC_KEYWORD = r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|parol|kalit|key)[a-z]{0,5}"

PATTERNS: Final[tuple[SecretPattern, ...]] = (
    SecretPattern(
        "pem_private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        show_tail=False,
    ),
    SecretPattern("claude_login_token", re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_-]{20,}"), refused=True),
    SecretPattern("anthropic_api_key", re.compile(r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{20,}")),
    SecretPattern("openrouter_api_key", re.compile(r"sk-or-v1-[0-9a-f]{64}")),
    SecretPattern("openai_api_key", re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    SecretPattern("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    SecretPattern("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{22,255}")),
    SecretPattern("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}")),
    SecretPattern("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    SecretPattern("aws_access_key_id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    SecretPattern(
        "aws_secret_access_key",
        re.compile(
            r"(?i:aws_secret_access_key|aws_secret_key|aws_secret)[\s\"':=]{1,6}([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
        ),
        group=1,
    ),
    SecretPattern("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    SecretPattern("telegram_bot_token", re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    SecretPattern(
        "oauth_callback_url",
        re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?/[^\s?#]*\?[^\s#]*\bcode=[^\s]+"),
        show_tail=False,
    ),
    SecretPattern(
        "telegram_bot_url",
        # `[` and `]` are excluded so an already-masked marker after "bot" is never re-matched (T27-B1).
        re.compile(r"(?<=api\.telegram\.org/)(?:file/)?bot([^/\s\"'\[\]]+)"),
        group=1,
        vaultable=False,
        accept=lambda value: MASK not in value,
    ),
    SecretPattern(
        "generic_secret",
        # The value stops at `&`, `#` and brackets: a URL query keeps its next parameter, and a masked marker
        # is never swallowed (T27-B2). A value holding MASK is already masked and is left alone (T27-B1).
        re.compile(rf"(?i)(?<![A-Za-z0-9]){_GENERIC_KEYWORD}(?![A-Za-z0-9])[\s\"'`:=]{{1,4}}([^\s\"'`&#\[\]]{{24,}})"),
        group=1,
        accept=lambda value: MASK not in value and _looks_random(value),
    ),
)


def find_secrets(text: str) -> list[SecretHit]:
    """Every non-overlapping secret in `text`, in text order. Where two patterns overlap, the earlier one wins."""
    taken: list[SecretHit] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            start, end = match.span(pattern.group)
            value = match.group(pattern.group)
            if pattern.accept is not None and not pattern.accept(value):
                continue
            if any(start < hit.end and hit.start < end for hit in taken):
                continue
            taken.append(
                SecretHit(pattern.kind, start, end, value, refused=pattern.refused, vaultable=pattern.vaultable)
            )
    return sorted(taken, key=lambda hit: hit.start)


def mask(value: str, *, show_tail: bool = True) -> str:
    """`••••last4`, or just `••••` when the value is short or its tail is meaningless."""
    if show_tail and len(value) >= _TAIL_MIN_LEN:
        return MASK + value[-4:]
    return MASK


@dataclass(frozen=True, slots=True)
class _KnownSecrets:
    values: frozenset[str]
    regex: re.Pattern[str] | None


_NO_KNOWN: Final = _KnownSecrets(frozenset(), None)
# Swapped atomically by set_known_secrets(); readers (any thread, the log listener included) never lock.
_known: _KnownSecrets = _NO_KNOWN
_SHOW_TAIL: Final = {pattern.kind: pattern.show_tail for pattern in PATTERNS}


def set_known_secrets(values: Iterable[str]) -> None:
    """Replace the set of decrypted vault values masked by value (SPEC §4.5 "in-memory set, rebuilt on change")."""
    global _known  # noqa: PLW0603  # one process-wide masking set, read by every log and outbound path
    kept = frozenset(v for v in values if len(v) >= MIN_KNOWN_SECRET_LEN)
    if not kept:
        _known = _NO_KNOWN
        return
    alternation = "|".join(re.escape(v) for v in sorted(kept, key=len, reverse=True))
    _known = _KnownSecrets(kept, re.compile(alternation))


def add_known_secrets(values: Iterable[str]) -> None:
    """Add values (e.g. the bot token read from the env file at startup) to the masking set."""
    set_known_secrets(_known.values | frozenset(values))


def known_secret_count() -> int:
    return len(_known.values)


def scrubbed(text: str) -> BotText:
    """`text` with every known value and pattern hit masked. Safe for logs, audit rows and the owner's chat."""
    known = _known
    if known.regex is not None:
        text = known.regex.sub(lambda m: mask(m.group(0)), text)
    hits = find_secrets(text)
    if not hits:
        return BotText(text)
    parts: list[str] = []
    cursor = 0
    for hit in hits:
        parts.append(text[cursor : hit.start])
        parts.append(mask(hit.value, show_tail=_SHOW_TAIL.get(hit.kind, True)))
        cursor = hit.end
    parts.append(text[cursor:])
    return BotText("".join(parts))


# --- process hooks -------------------------------------------------------------------------------------------------
class ScrubbingStream:
    """A text stream that scrubs whole lines before they reach the real stream.

    Text is held until a newline or `flush()`, so a token split across two `write()` calls is still caught.
    """

    def __init__(self, target: TextIO) -> None:
        self._target = target
        self._pending = ""
        self._lock = threading.Lock()
        self.encoding = getattr(target, "encoding", "utf-8")
        self.errors = getattr(target, "errors", "strict")

    @property
    def target(self) -> TextIO:
        return self._target

    def write(self, text: str) -> int:
        with self._lock:
            self._pending += text
            if "\n" in self._pending:
                complete, _, self._pending = self._pending.rpartition("\n")
                self._target.write(scrubbed(complete) + "\n")
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._target.write(scrubbed(self._pending))
                self._pending = ""
            self._target.flush()

    def fileno(self) -> int:
        return self._target.fileno()

    def isatty(self) -> bool:
        return self._target.isatty()

    def writable(self) -> bool:
        return True


def _write_scrubbed_traceback(
    exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType | None
) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(scrubbed(text))
    sys.stderr.flush()


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    _write_scrubbed_traceback(args.exc_type, args.exc_value, args.exc_traceback)


def _unraisablehook(unraisable: "sys.UnraisableHookArgs") -> None:
    message = unraisable.err_msg or "Exception ignored in"
    sys.stderr.write(scrubbed(f"{message}: {unraisable.object!r}\n"))
    _write_scrubbed_traceback(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)


def install_process_hooks() -> None:
    """Scrub stdout, stderr and every uncaught-exception path of the process. Idempotent."""
    if not isinstance(sys.stderr, ScrubbingStream):
        sys.stderr = cast(TextIO, ScrubbingStream(sys.stderr))
    if not isinstance(sys.stdout, ScrubbingStream):
        sys.stdout = cast(TextIO, ScrubbingStream(sys.stdout))
    sys.excepthook = _write_scrubbed_traceback
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook


def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
    """Event-loop handler: the message is scrubbed here, the traceback by the log formatter (or stderr)."""
    del loop
    exception = context.get("exception")
    logging.getLogger("yordamchi.asyncio").error(
        "asyncio_unhandled",
        extra={LOG_FIELDS_ATTR: {"message": scrubbed(str(context.get("message", "")))}},
        exc_info=exception if isinstance(exception, BaseException) else None,
    )


def install_asyncio_handler(loop: asyncio.AbstractEventLoop) -> None:
    loop.set_exception_handler(asyncio_exception_handler)
