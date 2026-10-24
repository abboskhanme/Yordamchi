"""Secrets eval (UC-04, NFR-SEC-02/03; SPEC §4.5, §3.2.1 step 3). One runner for `cases.yaml`.

Each case is one owner message, sent on each bot through the real intake path with fakes and a real SQLite file in
`tmp_path`: `inbox.accept_update` (scan, vault, redacted payload, ack rows), the outbox sender, and a burst pass that
copies the redacted content on. Then, as after a restart, the masking set is rebuilt from the vault
(`vault.publish_secrets`) and a raw "model output" echoing every detected key goes through the outbox (the outbound
filter, UC-04 AC).

For a key the scanner knows (stored or refused) the plaintext must appear 0 times in:
- every log record, as `JsonFormatter` writes it and as the call site passed it (our loggers at DEBUG, libraries at
  the production INFO);
- every Messenger request (the ack, the masked reply, the echo);
- the stored inbox payload, the DB files (`db_file_bytes`) and every other file under `tmp_path`;
- the vault rows, which hold its ciphertext only.
A key in an unknown format, typed outside any command, is not detectable, so it stays in the stored text; UC-04 asks
that it never reach a reply or a log, and that is what is checked for it.

"Appears" is searched as every 16-character window of the value (of each line, for a PEM block's body), so a partial
copy counts as a leak too; the masked `••••last4` never holds one.
"""

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

import pytest

from tests.eval.casefile import CaseFileError, as_bool, as_list, as_str, fields, load_cases, thresholds
from tests.factories import (
    Fakes,
    db_file_bytes,
    drain_outbox,
    make_dev_deps,
    make_paths,
    make_update,
    open_test_db,
    origin_of,
    send_outbox,
)
from tests.fakes.messenger import Request
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import JsonFormatter
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.redact import LOG_FIELDS_ATTR
from yordamchi.core.text import BotText, join
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.burst import service as burst
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import Accepted, Attachment, IncomingUpdate, UpdateKind, UpdateStatus
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Delivery, Kind, Op, Status, op_key
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault import store as vault_store

CASES_FILE: Final = Path(__file__).with_name("cases.yaml")
THRESHOLDS: Final = thresholds("secrets")
DELETE_WITHIN_S: Final = thresholds("latency")["secret_message_delete_s"]
CONTEXTS: Final = frozenset({"prose", "code", "url", "caption", "edited", "free_text"})
WINDOW: Final = 16  # a copy of this many characters of a key counts as a leak
_PLACEHOLDER: Final = re.compile(r"<<(\d+)>>")
_ESCAPED: Final = frozenset('"\\<>&')  # characters JSON or HTML would rewrite: a copy would hide from the search
_NO_TAIL_KINDS: Final = frozenset(pattern.kind for pattern in redact.PATTERNS if not pattern.show_tail)
_STORED_KINDS: Final = frozenset(p.kind for p in redact.PATTERNS if p.vaultable and not p.refused)
_REFUSED_KINDS: Final = frozenset(p.kind for p in redact.PATTERNS if p.refused)
ECHO_HEAD: Final = "Natija:"
BOTS: Final = (BotName.DEV, BotName.PERSONAL)


# --- the case file --------------------------------------------------------------------------------------------------
class Outcome(StrEnum):
    STORED = "stored"
    REFUSED = "refused"
    UNDETECTED = "undetected"


class Field(StrEnum):
    TEXT = "text"
    CAPTION = "caption"
    EDITED = "edited"


@dataclass(frozen=True, slots=True)
class Key:
    kind: str
    outcome: Outcome
    value: str

    @property
    def detected(self) -> bool:
        return self.outcome is not Outcome.UNDETECTED

    @property
    def masked(self) -> str:
        """What the owner may see of it: `••••` and the last four characters, unless they say nothing."""
        shows_tail = self.kind not in _NO_TAIL_KINDS and len(self.value) >= 12
        return redact.MASK + (self.value[-4:] if shows_tail else "")


@dataclass(frozen=True, slots=True)
class KnownBug:
    """A reported, unfixed bug this case fails on (`cases.yaml` lists them at its top)."""

    id: str
    leaks: bool  # it puts plaintext somewhere: the suite misses plaintext_hits_max until it is fixed
    note: str


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    context: str
    field: Field
    message: str
    keys: tuple[Key, ...]
    known_bug: KnownBug | None = None

    def text(self, replace: Mapping[int, str] | None = None) -> str:
        """The message with each `<<i>>` replaced by key i's value, or by `replace[i]` where given."""
        chosen = replace or {}
        return _PLACEHOLDER.sub(lambda m: chosen.get(int(m.group(1)), self.keys[int(m.group(1))].value), self.message)

    def with_outcome(self, outcome: Outcome) -> list[Key]:
        """The distinct keys with this outcome, in the order the message first names them."""
        seen: dict[str, Key] = {}
        for match in _PLACEHOLDER.finditer(self.message):
            key = self.keys[int(match.group(1))]
            if key.outcome is outcome:
                seen.setdefault(key.value, key)
        return list(seen.values())


def _key(raw: object, where: str) -> Key:
    data = fields(raw, where, required={"kind", "value"}, optional={"outcome"})
    kind = as_str(data["kind"], f"{where} kind")
    outcome = Outcome(as_str(data.get("outcome", Outcome.STORED.value), f"{where} outcome"))
    value = "".join(as_str(item, f"{where} value") for item in as_list(data["value"], f"{where} value"))
    allowed = {Outcome.STORED: _STORED_KINDS, Outcome.REFUSED: _REFUSED_KINDS, Outcome.UNDETECTED: {"unknown"}}
    if kind not in allowed[outcome]:
        msg = f"{where}: kind {kind!r} cannot have outcome {outcome.value!r}"
        raise CaseFileError(msg)
    if set(value) & _ESCAPED or any(ord(char) < 32 and char != "\n" for char in value):
        msg = f"{where}: a value may not hold characters JSON or HTML escape"
        raise CaseFileError(msg)
    if len(value) < WINDOW:
        msg = f"{where}: a value shorter than {WINDOW} characters cannot be searched for reliably"
        raise CaseFileError(msg)
    return Key(kind=kind, outcome=outcome, value=value)


def _known_bug(raw: object, where: str) -> KnownBug:
    data = fields(raw, where, required={"id", "leaks", "note"})
    return KnownBug(
        id=as_str(data["id"], f"{where} id"),
        leaks=as_bool(data["leaks"], f"{where} leaks"),
        note=as_str(data["note"], f"{where} note"),
    )


def _case(raw: Mapping[str, object]) -> Case:
    case_id = as_str(raw["id"], "case id")
    data = fields(raw, case_id, required={"id", "context", "message", "keys"}, optional={"field", "known_bug"})
    context = as_str(data["context"], f"{case_id} context")
    if context not in CONTEXTS:
        msg = f"{case_id}: unknown context {context!r}"
        raise CaseFileError(msg)
    keys = tuple(_key(item, f"{case_id} key #{i}") for i, item in enumerate(as_list(data["keys"], case_id)))
    message = as_str(data["message"], f"{case_id} message")
    used = {int(index) for index in _PLACEHOLDER.findall(message)}
    if not keys or used != set(range(len(keys))):
        msg = f"{case_id}: the message must name every key (<<0>> … <<{len(keys) - 1}>>) and no other"
        raise CaseFileError(msg)
    field = Field(as_str(data.get("field", Field.TEXT.value), f"{case_id} field"))
    bug = None if data.get("known_bug") is None else _known_bug(data["known_bug"], f"{case_id} known_bug")
    return Case(id=case_id, context=context, field=field, message=message, keys=keys, known_bug=bug)


CASES: Final = tuple(_case(raw) for raw in load_cases(CASES_FILE))
# "<case> (<bug>)" for each case whose reported bug leaks plaintext.
KNOWN_LEAKS: Final = tuple(
    f"{case.id} ({case.known_bug.id})" for case in CASES if case.known_bug is not None and case.known_bug.leaks
)


def _param(case: Case) -> object:
    """A case as a pytest param: one that fails on a reported bug is a strict xfail, so its fix shows at once."""
    if case.known_bug is None:
        return pytest.param(case, id=case.id)
    reason = f"{case.known_bug.id}: {case.known_bug.note} (reported by T27; see cases.yaml)"
    return pytest.param(case, id=case.id, marks=pytest.mark.xfail(strict=True, reason=reason))


# --- what one message leaves behind ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VaultRow:
    name: str
    kind: str
    last4: str
    ciphertext: bytes | None
    nonce: bytes | None
    bots: JsonValue


@dataclass(frozen=True, slots=True)
class Observation:
    update: IncomingUpdate
    received_at: datetime
    accepted: Accepted
    requests: tuple[Request, ...]
    log_lines: tuple[str, ...]  # as JsonFormatter writes them
    log_calls: tuple[str, ...]  # what each call site passed, before any scrubbing
    log_events: frozenset[str]
    stored_text: object  # the inbox payload's text or caption
    payload_json: str  # the whole inbox payload
    db: bytes  # this bot's DB file, WAL and shm
    files: Mapping[str, bytes]  # every file under the test root
    vault_rows: tuple[VaultRow, ...]
    opened: Mapping[str, str]  # vault name → plaintext, read back the way an adapter would
    hashes: frozenset[str] | None  # secret-hashes.json, if the bot wrote one
    masked_after_restart: int
    known_before_restart: int
    delete: Delivery | None


def _update(case: Case, bot: BotName) -> IncomingUpdate:
    text = case.text()
    if case.field is Field.CAPTION:
        photo = Attachment(file_id="AgAD-photo", mime="image/jpeg", size_bytes=2048)
        return make_update(1, text=None, caption=text, attachment=photo, kind=UpdateKind.PHOTO, bot=bot)
    kind = UpdateKind.EDITED if case.field is Field.EDITED else UpdateKind.TEXT
    return make_update(1, text=text, kind=kind, bot=bot)


def _log_call(record: logging.LogRecord) -> str:
    fields_ = getattr(record, LOG_FIELDS_ATTR, None)
    return " ".join((record.getMessage(), repr(fields_), repr(record.args), str(record.exc_info or "")))


@contextmanager
def _capturing(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """The production level for libraries (INFO, `core.log.configure`), and everything our own code logs."""
    caplog.clear()
    with caplog.at_level(logging.INFO), caplog.at_level(logging.DEBUG, logger="yordamchi"):
        yield


async def _echo(deps: SharedDeps, case: Case, chat_id: int) -> None:
    """Raw model output that repeats every detected key, queued unscrubbed: the outbox alone must mask it."""
    detected = [key.value for key in case.keys if key.detected]
    if not detected:
        return
    snap = await settings.snapshot(deps)
    text = BotText("\n".join((ECHO_HEAD, *detected, "Tayyor.")))
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=Kind.RESULT,
            ref="eval-echo",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )


async def observe(
    case: Case, deps: SharedDeps, fakes: Fakes, paths: Paths, caplog: pytest.LogCaptureFixture
) -> Observation:
    update = _update(case, deps.bot)
    formatter = JsonFormatter(process=deps.bot.value, version="eval")
    with _capturing(caplog):
        accepted = await inbox.accept_update(deps, origin_of(update), update)
        await send_outbox(deps)  # the sender's first pass: the delete, then the masked reply, typing, the reaction
        delete = await outbox.get_delivery(deps, op_key(Kind.ACK, f"update-{update.update_id}", Op.DELETE))
        await burst.attach_received_updates(deps)  # the next stage copies the redacted content on
        known_before_restart = redact.known_secret_count()
        redact.set_known_secrets([])  # a new process: the masking set is rebuilt from the vault alone
        masked_after_restart = await vault.publish_secrets(deps)
        await _echo(deps, case, update.chat_id)
        await drain_outbox(deps, fakes.clock, seconds=3)
        records = list(caplog.records)
    async with deps.db.read() as s:
        [row] = await inbox_store.list_inbound_updates(s, deps.bot.value)
        secrets = await vault_store.list_sealed_secrets(s)
    payload = row.payload_json
    assert isinstance(payload, dict)
    opened = {secret.name: await vault.open_secret(deps, secret.name) for secret in secrets}
    hashes_file = paths.secret_hashes
    hashes = None
    if hashes_file.exists():
        document = json.loads(hashes_file.read_text(encoding="utf-8"))
        hashes = frozenset(str(item) for item in document["sha256"])
    root = paths.data.parent
    return Observation(
        update=update,
        received_at=row.received_at,
        accepted=accepted,
        requests=tuple(fakes.messenger.requests),
        log_lines=tuple(formatter.format(record) for record in records),
        log_calls=tuple(_log_call(record) for record in records),
        log_events=frozenset(str(record.msg) for record in records),
        stored_text=payload["caption" if case.field is Field.CAPTION else "text"],
        payload_json=json.dumps(payload, ensure_ascii=False),
        db=db_file_bytes(paths.db_file(deps.bot)),
        files={str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()},
        vault_rows=tuple(
            VaultRow(
                name=secret.name,
                kind=secret.kind,
                last4=secret.last4,
                ciphertext=secret.ciphertext,
                nonce=secret.nonce,
                bots=secret.bots_json,
            )
            for secret in secrets
        ),
        opened=opened,
        hashes=hashes,
        masked_after_restart=masked_after_restart,
        known_before_restart=known_before_restart,
        delete=delete,
    )


# --- leak search ----------------------------------------------------------------------------------------------------
def windows(value: str) -> frozenset[str]:
    """Every `WINDOW`-character piece of the value, line by line. PEM armour lines are public and left out."""
    pieces: set[str] = set()
    for line in (line.strip() for line in value.splitlines()):
        if not line or line.startswith("-----"):
            continue
        if len(line) <= WINDOW:
            pieces.add(line)
        else:
            pieces.update(line[start : start + WINDOW] for start in range(len(line) - WINDOW + 1))
    return frozenset(pieces)


def found_in(value: str, haystacks: Sequence[str | bytes]) -> bool:
    pieces = windows(value)
    encoded = [piece.encode() for piece in pieces]
    for haystack in haystacks:
        if isinstance(haystack, bytes):
            if any(piece in haystack for piece in encoded):
                return True
        elif any(piece in haystack for piece in pieces):
            return True
    return False


def _sent(observation: Observation) -> list[str]:
    return [json.dumps(request.payload, default=str, ensure_ascii=False) for request in observation.requests]


def leaks(case: Case, observation: Observation) -> Counter[str]:
    """Where a key's plaintext showed up, counted per (key, place). Unknown formats count under `unknown:*`."""
    found: Counter[str] = Counter()
    ciphertexts = [blob for row in observation.vault_rows for blob in (row.ciphertext, row.nonce) if blob is not None]
    for key in {key.value: key for key in case.keys}.values():
        places: dict[str, Sequence[str | bytes]] = {
            "log_lines": observation.log_lines,
            "log_calls": observation.log_calls,
            "messenger_requests": _sent(observation),
        }
        if key.detected:
            places |= {
                "inbox_payload": [observation.payload_json],
                "db_file_bytes": [observation.db],
                "files_under_tmp_path": list(observation.files.values()),
                "vault_ciphertext": ciphertexts,
            }
        prefix = "known" if key.detected else "unknown"
        for place, haystacks in places.items():
            if found_in(key.value, haystacks):
                found[f"{prefix}:{place}"] += 1
    return found


# --- the runner -----------------------------------------------------------------------------------------------------
def _marker(key: Key) -> str:
    if key.outcome is Outcome.REFUSED:
        return S.SECRET_REFUSED_MARKER()
    if key.outcome is Outcome.UNDETECTED:
        return key.value
    return S.SECRET_MARKER(name=key.kind, masked=key.masked)


def _expected_reply(case: Case) -> BotText | None:
    stored, refused = case.with_outcome(Outcome.STORED), case.with_outcome(Outcome.REFUSED)
    notices: list[BotText] = []
    if stored:
        notices.append(S.SECRETS_STORED(keys=", ".join(f"{key.kind} {key.masked}" for key in stored)))
    if refused:
        notices.append(S.CLAUDE_TOKEN_REFUSED())
    return join(notices) if notices else None


def _texts_sent(observation: Observation, *, echo: bool) -> list[Request]:
    """The `send_text` requests: the echo of model output, or every other (the masked reply)."""
    return [
        request
        for request in observation.requests
        if request.method == "send_text" and str(request.payload["text"]).startswith(ECHO_HEAD) is echo
    ]


def _check_nothing_leaked(case: Case, seen: Observation, bot: BotName) -> None:
    """0 plaintext hits for a known format, 0 leaks for an unknown one, and the search had something to search."""
    hits = leaks(case, seen)
    known = sum(count for place, count in hits.items() if place.startswith("known:"))
    unknown = sum(count for place, count in hits.items() if place.startswith("unknown:"))
    assert (known, unknown) == (0, 0), dict(hits)
    assert known <= THRESHOLDS["plaintext_hits_max"]
    assert unknown <= THRESHOLDS["unknown_format_leaks_max"]
    assert {"update_accepted", "outbox_message_sent", "secrets_published"} <= seen.log_events
    assert seen.requests
    assert f"{bot.value}.db" in {Path(name).name for name in seen.files}


def _check_stored_payload(case: Case, seen: Observation) -> None:
    """Each key's marker in its place, never the key; a refused token counted once per mention."""
    refused = case.with_outcome(Outcome.REFUSED)
    assert seen.stored_text == case.text({i: _marker(key) for i, key in enumerate(case.keys)})
    assert seen.accepted.status is UpdateStatus.RECEIVED
    mentions = sum(case.message.count(f"<<{i}>>") for i, key in enumerate(case.keys) if key in refused)
    assert seen.accepted.refused == mentions


def _check_vault(case: Case, seen: Observation, bot: BotName) -> None:
    """Exactly the stored keys, as ciphertext that opens to the very key; a refused token is nowhere."""
    stored = case.with_outcome(Outcome.STORED)
    assert sorted((row.name, row.kind, row.last4) for row in seen.vault_rows) == sorted(
        (key.kind, key.kind, key.masked.removeprefix(redact.MASK)) for key in stored
    )
    assert all(row.ciphertext and row.nonce is not None and len(row.nonce) == 12 for row in seen.vault_rows)
    assert all(row.bots == [bot.value] for row in seen.vault_rows)
    assert seen.opened == {key.kind: key.value for key in stored}
    assert seen.known_before_restart == len(stored)  # what intake masks at once; never a refused token
    assert seen.masked_after_restart == len(stored)
    digests = frozenset(hashlib.sha256(key.value.encode()).hexdigest() for key in stored)
    # Only the dev bot writes the pre-push hash file: the stored keys' hashes, nothing refused.
    assert seen.hashes == (digests if bot is BotName.DEV else None)


def _check_ack(case: Case, seen: Observation) -> None:
    """A message holding a key is deleted at once, before the one masked reply; any other message is only seen."""
    methods = [request.method for request in seen.requests]
    replies = _texts_sent(seen, echo=False)
    if not case.with_outcome(Outcome.STORED) and not case.with_outcome(Outcome.REFUSED):
        assert ("delete" in methods, replies) == (False, [])
        [react] = [request for request in seen.requests if request.method == "react"]
        assert react.payload["message_id"] == seen.update.message_id
        return
    [delete] = [request for request in seen.requests if request.method == "delete"]
    assert (delete.chat_id, delete.payload["message_id"]) == (seen.update.chat_id, seen.update.message_id)
    assert seen.delete is not None
    assert seen.delete.status is Status.SENT
    assert seen.delete.sent_at is not None
    assert (seen.delete.sent_at - seen.received_at).total_seconds() <= DELETE_WITHIN_S
    [reply] = replies
    assert reply.payload["text"] == _expected_reply(case)
    assert methods.index("delete") < seen.requests.index(reply)
    assert "react" not in methods


def _check_outbound_filter(case: Case, seen: Observation) -> None:
    """Model output repeating every detected key reaches the owner with each one masked (UC-04 AC)."""
    echoes = _texts_sent(seen, echo=True)
    detected = [key for key in case.keys if key.detected]
    if not detected:
        assert echoes == []
        return
    [echo] = echoes
    lines = str(echo.payload["text"]).split("\n")
    assert (lines[0], lines[-1], len(lines)) == (ECHO_HEAD, "Tayyor.", len(detected) + 2)
    assert all(line.startswith(redact.MASK) for line in lines[1:-1]), lines


@pytest.mark.parametrize("bot", BOTS, ids=[bot.value for bot in BOTS])
@pytest.mark.parametrize("case", [_param(case) for case in CASES])
async def test_case(
    case: Case,
    bot: BotName,
    dev_deps: DevDeps,
    personal_deps: PersonalDeps,
    fakes: Fakes,
    paths: Paths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both bots' DBs exist (the personal bot reads dev.db); only `bot` takes the message in."""
    deps: SharedDeps = dev_deps if bot is BotName.DEV else personal_deps
    seen = await observe(case, deps, fakes, paths, caplog)

    _check_nothing_leaked(case, seen, bot)
    _check_stored_payload(case, seen)
    _check_vault(case, seen, bot)
    _check_ack(case, seen)
    _check_outbound_filter(case, seen)


async def _observe_in(root: Path, case: Case, caplog: pytest.LogCaptureFixture) -> Observation:
    """One case on its own dev DB and fakes under `root`, with an empty masking set, as a fresh process would."""
    redact.set_known_secrets([])
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    try:
        return await observe(case, make_dev_deps(db=db, paths=paths, fakes=fakes), fakes, paths, caplog)
    finally:
        await db.dispose()


def test_the_cases_cover_the_uc04_fixture() -> None:
    """UC-04: >= 10 key formats, the refused Claude login token, an unknown format typed outside any command, and keys
    in prose, code blocks and URLs."""
    detected = [key for case in CASES for key in case.keys if key.detected]
    assert len({key.kind for key in detected}) >= THRESHOLDS["formats_min"]
    assert any(key.outcome is Outcome.REFUSED and key.kind == "claude_login_token" for key in detected)
    assert any(key.kind == "anthropic_api_key" and key.outcome is Outcome.STORED for key in detected)
    free_text = [case for case in CASES if case.context == "free_text"]
    assert any(key.outcome is Outcome.UNDETECTED for case in free_text for key in case.keys)
    assert {"prose", "code", "url"} <= {case.context for case in CASES}


@pytest.mark.xfail(
    condition=bool(KNOWN_LEAKS),
    strict=True,
    reason=f"reported bugs leak plaintext: {', '.join(KNOWN_LEAKS)} (see cases.yaml)",
)
async def test_the_suite_meets_the_leak_thresholds(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Every case once, on the dev bot: plaintext hits across all cases <= plaintext_hits_max, and unknown-format
    leaks <= unknown_format_leaks_max. A strict xfail while a `known_bug` with `leaks: true` is listed."""
    total: Counter[str] = Counter()
    for case in CASES:
        total += leaks(case, await _observe_in(tmp_path / case.id, case, caplog))
    known = sum(count for place, count in total.items() if place.startswith("known:"))
    unknown = sum(count for place, count in total.items() if place.startswith("unknown:"))
    assert known <= THRESHOLDS["plaintext_hits_max"], dict(total)
    assert unknown <= THRESHOLDS["unknown_format_leaks_max"], dict(total)
