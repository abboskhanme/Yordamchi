"""Vault use cases (SPEC §4.5; UC-04; NFR-SEC-02/03/10): store, open, list and remove secrets; keep the masking set
and `secret-hashes.json` current; wipe old ciphertexts.

Plaintext exists only in memory: in the argument of `add_secrets`, the return value of `open_secret`, and the
process-wide masking set of `core.redact`. The DB and every log line hold names, kinds and `••••last4` only.

The masking set only grows while the process runs (`redact.add_known_secrets`), so it never drops the bot token that
`core.config` put there, and a removed or rotated key stays masked. It is rebuilt from the DB at the next start.
"""

import asyncio
import os
import tempfile
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from yordamchi.core import json_value, redact
from yordamchi.core.errors import VaultDecryptFailed, VaultKeyInsecure, VaultKeyMissing, VaultSecretNotFound
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.keystore import Keystore, Sealed
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.vault import domain, store
from yordamchi.shared.vault.domain import NewSecret, SecretRef
from yordamchi.shared.vault.models import VaultSecret

log = get_log(__name__)

# The master key cannot be used at all: `add_secrets` raises one of these before it writes anything.
KEY_UNAVAILABLE: Final = (VaultKeyMissing, VaultKeyInsecure)
AUDIT_REF_TYPE: Final = "vault_secret"


def _ref(row: VaultSecret) -> SecretRef:
    return SecretRef(
        id=row.id, name=row.name, kind=row.kind, last4=row.last4, bots=domain.bots_from_json(row.bots_json)
    )


def _unseal(keystore: Keystore, row: VaultSecret) -> str:
    """The plaintext of one row. Raises `VaultDecryptFailed` if it holds no ciphertext or the ciphertext won't open."""
    if row.ciphertext is None or row.nonce is None:
        raise VaultDecryptFailed(name=row.name)
    sealed = Sealed(ciphertext=row.ciphertext, nonce=row.nonce, key_fp=row.key_fp)
    aad = domain.secret_aad(name=row.name, kind=row.kind, secret_id=row.id)
    return keystore.unseal(sealed, aad=aad, name=row.name).decode()


def _readable_values(keystore: Keystore, rows: Iterable[VaultSecret]) -> dict[int, str]:
    """Row id → plaintext for every row that opens. A row that does not open is logged and skipped, so one damaged
    row never blocks intake or the masking set. A missing or insecure key still raises."""
    values: dict[int, str] = {}
    for row in rows:
        try:
            values[row.id] = _unseal(keystore, row)
        except VaultDecryptFailed:
            log.warning("secret_unreadable", secret_id=row.id, name=row.name)
    return values


async def _insert(
    s: Session, keystore: Keystore, *, origin: OwnerOrigin, secret: NewSecret, name: str, key_fp: str, at: datetime
) -> VaultSecret:
    row = await store.add_secret(
        s,
        name=name,
        kind=secret.kind,
        key_fp=key_fp,
        last4=domain.last4_of(secret.value, secret.kind),
        bots=[origin.bot.value],
        origin_update_id=origin.update_id,
        created_at=at,
    )
    # The AAD names the row id, so the row exists (flushed, not committed) before its value is sealed.
    sealed = keystore.seal(secret.value.encode(), aad=domain.secret_aad(name=row.name, kind=row.kind, secret_id=row.id))
    await store.set_ciphertext(s, row, ciphertext=sealed.ciphertext, nonce=sealed.nonce, key_fp=sealed.key_fp)
    await audit.record_event(
        s,
        bot=origin.bot,
        at=at,
        actor=AuditActor.OWNER,
        kind=AuditKind.SECRET_ADDED,
        ref_type=AUDIT_REF_TYPE,
        ref_id=str(row.id),
        data={"name": row.name, "kind": row.kind, "origin_update_id": origin.update_id},
    )
    log.info("secret_added", secret_id=row.id, name=row.name, kind=row.kind)
    return row


async def add_secrets(
    s: Session, keystore: Keystore, *, origin: OwnerOrigin, secrets: Sequence[NewSecret], at: datetime
) -> tuple[SecretRef, ...]:
    """Seal and store each value in the caller's transaction; one ref per input, in input order.

    - Only an owner's message adds a secret (`OwnerOrigin`); it is assigned to the bot it was said in.
    - The key is checked first: `VaultKeyMissing` / `VaultKeyInsecure` is raised before anything is written.
    - A value equal to an active secret of the same kind returns that secret instead of storing a copy.
    - One `secret_added` audit event per new row, by name only (NFR-SEC-10).
    - Every value joins the masking set at once; masking a value whose transaction later rolls back is harmless.

    After the caller's transaction commits it runs `publish_secrets(deps)`, which refreshes `secret-hashes.json`.
    Anything but an `OwnerOrigin` raises `TypeError` before anything is written.
    """
    origin = require_owner_origin(origin)
    if not secrets:
        return ()
    key_fp = keystore.fingerprint()
    active = await store.list_active_secrets(s)
    kinds = {secret.kind for secret in secrets}
    opened = _readable_values(keystore, (row for row in active if row.kind in kinds))
    stored: dict[tuple[str, str], VaultSecret] = {(row.kind, opened[row.id]): row for row in active if row.id in opened}
    taken = {row.name for row in active}
    refs: list[SecretRef] = []
    for secret in secrets:
        row = stored.get((secret.kind, secret.value))
        if row is None:
            name = domain.next_free_name(secret.kind, taken)
            row = await _insert(s, keystore, origin=origin, secret=secret, name=name, key_fp=key_fp, at=at)
            taken.add(row.name)
            stored[(secret.kind, secret.value)] = row
        refs.append(_ref(row))
    redact.add_known_secrets(secret.value for secret in secrets)
    return tuple(refs)


async def open_secret(deps: SharedDeps, name: str) -> str:
    """The plaintext of an active secret of this bot, for an adapter call. Never log or store the result.

    Raises `VaultSecretNotFound` (no active secret by that name) or `VaultDecryptFailed` (it does not open).
    """
    async with deps.db.read() as s:
        row = await store.find_active_secret(s, name)
    if row is None:
        raise VaultSecretNotFound(name=name)
    return _unseal(deps.keystore, row)


async def list_secrets(deps: SharedDeps) -> tuple[SecretRef, ...]:
    """The active secrets of this bot, oldest first: names and masks only."""
    async with deps.db.read() as s:
        rows = await store.list_active_secrets(s)
    return tuple(_ref(row) for row in rows)


async def remove_secret(deps: SharedDeps, origin: OwnerOrigin, name: str) -> SecretRef:
    """Mark an active secret removed (its ciphertext is wiped 30 days later) and audit it. The value stays in the
    masking set and in `secret-hashes.json` until then: a key the owner retired must still never leak.

    Raises `VaultSecretNotFound`, and nothing changes.
    """
    origin = require_owner_origin(origin, deps.bot)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.find_active_secret(s, name)
        if row is None:
            raise VaultSecretNotFound(name=name)
        await store.mark_secret_removed(s, row, at=now)
        await audit.record_event(
            s,
            bot=deps.bot,
            at=now,
            actor=AuditActor.OWNER,
            kind=AuditKind.SECRET_REMOVED,
            ref_type=AUDIT_REF_TYPE,
            ref_id=str(row.id),
            data={"name": row.name, "kind": row.kind, "origin_update_id": origin.update_id},
        )
    log.info("secret_removed", secret_id=row.id, name=row.name, kind=row.kind)
    return _ref(row)


def _visible_peer_rows(rows: Collection[VaultSecret], reader: BotName) -> list[VaultSecret]:
    # The dev bot masks every personal secret; the personal bot reads only dev rows assigned to it (SPEC §2.3).
    if reader is BotName.DEV:
        return list(rows)
    return [row for row in rows if domain.visible_to(domain.bots_from_json(row.bots_json), reader)]


def _write_private_file(path: Path, data: bytes) -> None:
    """Atomic write: a 0600 temp file in the same 0700 directory, fsync, rename. A reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # mode 0600
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


async def publish_secrets(deps: SharedDeps) -> int:
    """Load every stored value into the masking set and, on the dev bot, rewrite `run/secret-hashes.json`.

    Run at startup and after a transaction that added secrets. It covers this bot's sealed rows (active, and removed
    but not yet wiped) plus the peer DB's rows this bot may see. Only the dev bot writes the hash file: the pre-push
    scan runs in dev sessions, and one writer keeps it whole. Returns how many values are masked.
    Raises `VaultKeyMissing` / `VaultKeyInsecure` when nothing can be opened.
    """
    async with deps.db.read() as s:
        rows = await store.list_sealed_secrets(s)
    if deps.peer_db is not None:
        async with deps.peer_db.read() as ro:
            rows += _visible_peer_rows(await store.list_sealed_secrets(ro), deps.bot)
    values = list(_readable_values(deps.keystore, rows).values())
    redact.add_known_secrets(values)
    if deps.bot is BotName.DEV:
        document = json_value.dumps(domain.hashes_document(values)).encode()
        await asyncio.to_thread(_write_private_file, deps.paths.secret_hashes, document)
    log.info("secrets_published", rows=len(rows), masked=len(values))
    return len(values)


async def apply_retention(deps: SharedDeps) -> int:
    """Wipe the ciphertext of secrets removed more than 30 days ago, in batches (SPEC §4.2). Returns how many."""
    cutoff = domain.wipe_cutoff(deps.clock.now())
    wiped = await bulk.run_in_batches(deps.db, lambda s, limit: store.wipe_removed_before(s, cutoff, limit=limit))
    log.info("vault_retention_applied", wiped=wiped)
    return wiped
