"""SQL for `integrations` and `oauth_clients`. Ciphertext in, ciphertext out; never commits."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.shared.integrations.models import Integration, OAuthClient


async def list_active_integrations(s: AsyncSession) -> list[Integration]:
    query = select(Integration).where(Integration.removed_at.is_(None)).order_by(Integration.id)
    return list((await s.scalars(query)).all())


async def find_active_integration(s: AsyncSession, slug: str) -> Integration | None:
    return await s.scalar(select(Integration).where(Integration.slug == slug, Integration.removed_at.is_(None)))


async def get_integration(s: AsyncSession, integration_id: int) -> Integration | None:
    return await s.get(Integration, integration_id)


async def list_removed_integrations(s: AsyncSession, *, since: datetime) -> list[Integration]:
    """Removed at or after `since`, newest first: the ones a revert may still bring back."""
    query = (
        select(Integration)
        .where(Integration.removed_at.is_not(None), Integration.removed_at >= since)
        .order_by(Integration.removed_at.desc())
    )
    return list((await s.scalars(query)).all())


async def add_integration(
    s: AsyncSession,
    *,
    slug: str,
    display_name: str,
    kind: str,
    url: str,
    auth: str,
    bots: JsonValue,
    tool_policy: JsonValue,
    tools_cache: JsonValue,
    status: str,
    created_at: datetime,
    description: str = "",
    header_name: str | None = None,
    secret_id: int | None = None,
    last_validated_at: datetime | None = None,
) -> Integration:
    row = Integration(
        slug=slug,
        display_name=display_name,
        kind=kind,
        url=url,
        description=description,
        auth=auth,
        header_name=header_name,
        secret_id=secret_id,
        bots_json=bots,
        tool_policy_json=tool_policy,
        tools_cache_json=tools_cache,
        status=status,
        status_code=None,
        last_validated_at=last_validated_at,
        created_at=created_at,
        removed_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def set_status(
    s: AsyncSession,
    row: Integration,
    *,
    status: str,
    status_code: str | None,
    validated_at: datetime | None,
    tools_cache: JsonValue | None = None,
) -> None:
    row.status = status
    row.status_code = status_code
    if validated_at is not None:
        row.last_validated_at = validated_at
    if tools_cache is not None:
        row.tools_cache_json = tools_cache
    await s.flush()


async def set_credentials(s: AsyncSession, row: Integration, *, auth: str, secret_id: int | None) -> None:
    row.auth = auth
    row.secret_id = secret_id
    await s.flush()


async def set_names(s: AsyncSession, row: Integration, *, slug: str, display_name: str) -> None:
    row.slug = slug
    row.display_name = display_name
    await s.flush()


async def set_bots(s: AsyncSession, row: Integration, *, bots: JsonValue) -> None:
    row.bots_json = bots
    await s.flush()


async def mark_integration_removed(s: AsyncSession, row: Integration, *, at: datetime | None) -> None:
    """`at=None` restores it."""
    row.removed_at = at
    await s.flush()


# --- oauth_clients --------------------------------------------------------------------------------------------------
async def get_oauth_client(s: AsyncSession, integration_id: int) -> OAuthClient | None:
    return await s.get(OAuthClient, integration_id)


async def list_oauth_clients(s: AsyncSession) -> list[tuple[Integration, OAuthClient]]:
    """Every sealed grant of an active OAuth integration, by integration id."""
    query = (
        select(Integration, OAuthClient)
        .join(OAuthClient, OAuthClient.integration_id == Integration.id)
        .where(Integration.removed_at.is_(None), OAuthClient.ciphertext.is_not(None))
        .order_by(Integration.id)
    )
    return [(integration, client) for integration, client in (await s.execute(query)).all()]


async def put_oauth_client(
    s: AsyncSession, *, integration_id: int, issuer: str | None, client_id: str, key_fp: str
) -> OAuthClient:
    """The row for this integration (created without a ciphertext, or the existing one), flushed."""
    row = await s.get(OAuthClient, integration_id)
    if row is None:
        row = OAuthClient(
            integration_id=integration_id,
            issuer=issuer,
            client_id=client_id,
            ciphertext=None,
            nonce=None,
            key_fp=key_fp,
            expires_at=None,
            last_refresh_at=None,
            refresh_failures=0,
            reauth_told_at=None,
        )
        s.add(row)
    else:
        row.issuer = issuer
        row.client_id = client_id
    await s.flush()
    return row


async def set_grant(
    s: AsyncSession,
    row: OAuthClient,
    *,
    ciphertext: bytes,
    nonce: bytes,
    key_fp: str,
    expires_at: datetime | None,
    refreshed_at: datetime | None,
) -> None:
    """A new grant: sealed, its expiry, and every failure mark cleared."""
    row.ciphertext = ciphertext
    row.nonce = nonce
    row.key_fp = key_fp
    row.expires_at = expires_at
    row.last_refresh_at = refreshed_at
    row.refresh_failures = 0
    row.reauth_told_at = None
    await s.flush()


async def set_refresh_failures(s: AsyncSession, row: OAuthClient, *, failures: int) -> None:
    row.refresh_failures = failures
    await s.flush()


async def mark_reauth_told(s: AsyncSession, row: OAuthClient, *, at: datetime) -> None:
    row.reauth_told_at = at
    await s.flush()


async def delete_removed_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Delete up to `limit` integrations removed before `cutoff`, their sealed grant first. Returns how many."""
    due = list(
        (
            await s.scalars(
                select(Integration.id)
                .where(Integration.removed_at.is_not(None), Integration.removed_at < cutoff)
                .order_by(Integration.removed_at)
                .limit(limit)
            )
        ).all()
    )
    if not due:
        return 0
    await bulk.affected_rows(s, delete(OAuthClient).where(OAuthClient.integration_id.in_(due)))
    return await bulk.affected_rows(s, delete(Integration).where(Integration.id.in_(due)))
