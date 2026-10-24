"""Settings use cases: the snapshot every job reads, and the versioned write (SPEC §2.3, §4.3; CONVENTIONS §5).

T21 builds change / revert / list, the weakening question and the chat interpretation on top of these two.
"""

from collections.abc import Iterable
from typing import Literal

from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.settings import domain, store
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import Snapshot, StoredValue
from yordamchi.shared.settings.models import SettingVersion
from yordamchi.shared.settings.registry import AnySetting, SettingValue

log = get_log(__name__)

type ChangeReason = Literal["change", "revert", "import"]


def _stored(rows: Iterable[SettingVersion]) -> dict[str, StoredValue]:
    return {row.key: StoredValue(key=row.key, value=row.value_json, row_id=row.id) for row in rows}


async def snapshot(deps: SharedDeps) -> Snapshot:
    """Every setting of this bot as of now. Read it once per turn or job; never keep it in a global."""
    async with deps.db.read() as s:
        own_rows = await store.list_latest_versions(s)
        own_version = await store.get_max_setting_version_id(s)
    dev_rows: list[SettingVersion] = []
    if deps.bot is BotName.PERSONAL and deps.peer_db is not None:
        async with deps.peer_db.read() as ro:
            dev_rows = await store.list_latest_versions(ro, domain.shared_keys(REGISTRY))
    resolution = domain.resolve(
        REGISTRY, deps.bot, own=_stored(own_rows), dev=_stored(dev_rows), bootstrap=deps.bootstrap
    )
    for skipped in resolution.skipped:
        log.warning("setting_row_skipped", key=skipped)
    dev_version = max((row.id for row in dev_rows), default=0)
    return Snapshot(
        bot=deps.bot,
        version=own_version + dev_version,
        values=resolution.values,
        sources=resolution.sources,
    )


def as_json(value: SettingValue) -> JsonValue:
    if isinstance(value, list):
        items: list[JsonValue] = [item for item in value]  # noqa: C416  # widens list[str] to list[JsonValue]
        return items
    if isinstance(value, dict):
        entries: dict[str, JsonValue] = {key: item for key, item in value.items()}  # noqa: C416
        return entries
    return value


async def record_version(
    deps: SharedDeps, origin: OwnerOrigin, setting: AnySetting, raw: JsonValue, *, reason: ChangeReason
) -> int:
    """Validate `raw` and append it as the next version of `setting`; returns that version.

    Only an `OwnerOrigin` can change a setting (A5). The caller has already asked the owner when `domain.weakens`
    is true. A key outside this bot's scope raises `SettingPersonalOnly` / `SettingDevOnly`; a bad value
    `SettingInvalid`, and nothing is stored.
    """
    if origin.bot is not deps.bot:
        msg = f"an owner origin of the {origin.bot} bot cannot change {deps.bot} settings"
        raise ValueError(msg)
    domain.require_writable_by(setting, deps.bot)
    value = domain.validate(setting, raw)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        version = await store.get_latest_version_number(s, setting.key) + 1
        await store.add_setting_version(
            s,
            key=setting.key,
            value=as_json(value),
            version=version,
            reason=reason,
            actor="owner",
            origin_update_id=origin.update_id,
            created_at=now,
        )
    log.info("setting_version_recorded", key=setting.key, version=version, reason=reason)
    return version
