"""SQL for `setting_versions`. Append-only: there is no update and no delete. Never commits."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.shared.settings.models import SettingVersion


async def list_latest_versions(s: AsyncSession, keys: Collection[str] | None = None) -> list[SettingVersion]:
    """The newest row of each key (all keys, or only `keys`)."""
    newest = select(SettingVersion.key, func.max(SettingVersion.version).label("version")).group_by(SettingVersion.key)
    if keys is not None:
        newest = newest.where(SettingVersion.key.in_(list(keys)))
    latest = newest.subquery()
    query = select(SettingVersion).join(
        latest, and_(SettingVersion.key == latest.c.key, SettingVersion.version == latest.c.version)
    )
    return list((await s.scalars(query)).all())


async def get_max_setting_version_id(s: AsyncSession) -> int:
    return (await s.scalar(select(func.max(SettingVersion.id)))) or 0


async def get_latest_version_number(s: AsyncSession, key: str) -> int:
    return (await s.scalar(select(func.max(SettingVersion.version)).where(SettingVersion.key == key))) or 0


async def find_newest_setting_version(s: AsyncSession) -> SettingVersion | None:
    """The row written last in this DB, whatever its key ("oldingi holatiga qaytar" reverts it)."""
    return await s.scalar(select(SettingVersion).order_by(SettingVersion.id.desc()).limit(1))


async def list_setting_history(s: AsyncSession, key: str) -> list[SettingVersion]:
    """Every row of one key, newest first (revert reads the one before the latest)."""
    query = select(SettingVersion).where(SettingVersion.key == key).order_by(SettingVersion.version.desc())
    return list((await s.scalars(query)).all())


async def add_setting_version(
    s: AsyncSession,
    *,
    key: str,
    value: JsonValue,
    version: int,
    reason: str,
    actor: str,
    origin_update_id: int | None,
    created_at: datetime,
) -> SettingVersion:
    row = SettingVersion(
        key=key,
        value_json=value,
        version=version,
        reason=reason,
        actor=actor,
        origin_update_id=origin_update_id,
        created_at=created_at,
    )
    s.add(row)
    await s.flush()
    return row
