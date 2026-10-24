"""SQL for `vault_secrets`. Ciphertext in, ciphertext out: this module never sees a plaintext. Never commits."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.shared.vault.models import VaultSecret


async def list_active_secrets(s: AsyncSession) -> list[VaultSecret]:
    query = select(VaultSecret).where(VaultSecret.removed_at.is_(None)).order_by(VaultSecret.id)
    return list((await s.scalars(query)).all())


async def list_sealed_secrets(s: AsyncSession) -> list[VaultSecret]:
    """Every row that still holds a ciphertext: active ones and removed ones not yet wiped."""
    query = select(VaultSecret).where(VaultSecret.ciphertext.is_not(None)).order_by(VaultSecret.id)
    return list((await s.scalars(query)).all())


async def find_active_secret(s: AsyncSession, name: str) -> VaultSecret | None:
    query = select(VaultSecret).where(VaultSecret.name == name, VaultSecret.removed_at.is_(None))
    return await s.scalar(query)


async def add_secret(
    s: AsyncSession,
    *,
    name: str,
    kind: str,
    key_fp: str,
    last4: str,
    bots: JsonValue,
    origin_update_id: int | None,
    created_at: datetime,
) -> VaultSecret:
    """Insert a row without its ciphertext and flush, so the caller can seal against the row id."""
    row = VaultSecret(
        name=name,
        kind=kind,
        ciphertext=None,
        nonce=None,
        key_fp=key_fp,
        last4=last4,
        bots_json=bots,
        origin_update_id=origin_update_id,
        created_at=created_at,
        removed_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def set_ciphertext(s: AsyncSession, row: VaultSecret, *, ciphertext: bytes, nonce: bytes, key_fp: str) -> None:
    row.ciphertext = ciphertext
    row.nonce = nonce
    row.key_fp = key_fp
    await s.flush()


async def mark_secret_removed(s: AsyncSession, row: VaultSecret, *, at: datetime) -> None:
    row.removed_at = at
    await s.flush()


async def wipe_removed_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Null the ciphertext and nonce of up to `limit` secrets removed before `cutoff`."""
    due = (
        select(VaultSecret.id)
        .where(VaultSecret.removed_at < cutoff, VaultSecret.ciphertext.is_not(None))
        .order_by(VaultSecret.removed_at)
        .limit(limit)
    )
    statement = (
        update(VaultSecret)
        .where(VaultSecret.id.in_(due))
        .values(ciphertext=None, nonce=None)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)
