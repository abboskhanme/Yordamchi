"""Vault rules, pure (SPEC §4.5, NFR-SEC-02/03): names, row binding, what a reference shows, the pre-push hash file.

- A secret's **name** is its kind (`google_api_key`); a second active secret of the same kind gets `<kind>_2`, then
  `_3`. Intake never replaces or removes a secret: the feature that uses a kind decides what a newer one means.
- The ciphertext is bound to its row: AAD = `name|kind|id`, so a ciphertext copied onto another row does not open.
- A `SecretRef` is what callers and the owner see: the name and `••••last4`, never the value.
- `secret-hashes.json` (`run/`, 0600, written by the dev bot only) is `{"sha256": [<hex>, …]}`, sorted and unique:
  the sha256 of every UTF-8 value that still has a ciphertext. The pre-push scan matches against it (T18).
"""

import hashlib
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from yordamchi.core import redact
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName

REMOVED_CIPHERTEXT_KEEP: Final = timedelta(days=30)  # SPEC §4.2: "removed: ciphertext wiped 30 d"
# A kind is at most 32 characters, so `<kind>_<n>` always fits the 64-character `name` column.
KIND_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,31}")
_SHOWS_TAIL: Final = {pattern.kind: pattern.show_tail for pattern in redact.PATTERNS}


@dataclass(frozen=True, slots=True)
class NewSecret:
    """A value to store; `kind` is a `core.redact` pattern kind (or a later one, e.g. an OAuth token bundle)."""

    kind: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not KIND_PATTERN.fullmatch(self.kind):
            msg = f"secret kind {self.kind!r} must match {KIND_PATTERN.pattern}"
            raise ValueError(msg)
        if not self.value:
            msg = "a secret value cannot be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A stored secret as the rest of the code sees it: never the value."""

    id: int
    name: str
    kind: str
    last4: str
    bots: tuple[BotName, ...]

    @property
    def masked(self) -> str:
        """`••••abcd`, or `••••` when the tail says nothing (PEM armour, a callback URL, a short value)."""
        return redact.MASK + self.last4


def last4_of(value: str, kind: str) -> str:
    """The last four characters, shown in confirmations; empty when `core.redact` would not show the tail either."""
    shown = redact.mask(value, show_tail=_SHOWS_TAIL.get(kind, True))
    return shown.removeprefix(redact.MASK)


def next_free_name(kind: str, taken: Collection[str]) -> str:
    """`kind`, else the first free `kind_<n>` (n ≥ 2) among the names of active secrets."""
    if kind not in taken:
        return kind
    number = 2
    while f"{kind}_{number}" in taken:
        number += 1
    return f"{kind}_{number}"


def secret_aad(*, name: str, kind: str, secret_id: int) -> bytes:
    """SPEC §4.5: AAD = `f"{name}|{kind}|{id}"` binds a ciphertext to its row."""
    return f"{name}|{kind}|{secret_id}".encode()


def bots_from_json(raw: JsonValue) -> tuple[BotName, ...]:
    """The `bots_json` column as bot names; unknown entries are ignored (a newer release may add a bot)."""
    if not isinstance(raw, list):
        return ()
    known = {bot.value: bot for bot in BotName}
    return tuple(known[item] for item in raw if isinstance(item, str) and item in known)


def visible_to(bots: Iterable[BotName], reader: BotName) -> bool:
    """A peer DB's secret counts for `reader` only if assigned to it (SPEC §2.3)."""
    return reader in set(bots)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hashes_document(values: Iterable[str]) -> JsonObject:
    """The `secret-hashes.json` body for the pre-push scan."""
    digests: list[JsonValue] = []
    digests.extend(sorted({sha256_hex(value) for value in values}))
    return {"sha256": digests}


def wipe_cutoff(now: datetime) -> datetime:
    """A removed secret's ciphertext is wiped once `removed_at` is strictly older than this."""
    return now - REMOVED_CIPHERTEXT_KEEP
