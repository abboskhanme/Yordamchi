"""ClaudeManager's `state.json`, read-only and token-free (UC-06 `holat?`; SPEC §1, §5.6).

ClaudeManager (`../ClaudeManager`, Tauri, identifier `uz.claudemanager.desktop`) persists its app state in
`~/Library/Application Support/uz.claudemanager.desktop/state.json` (`core/state.rs::Persisted`, serde camelCase):
`version`, `activeId`, `autoSwitch`, `settings`, `events`, `initialized`, and `accounts[]` with `id`, `email`,
`status` (`ok | needs_login | error`), `usage.{fiveHour,sevenDay}.{utilization 0–100, resetsAt ISO-8601}` and more.

The file holds no tokens (they live in the Keychain), but some fields are still not ours to pass on: `oauthAccount`,
`fingerprint` (of the tokens), `statusDetail` (free text), `events`, `settings`. Only the fields `ClaudeManagerState`
names are read. A field of the wrong type becomes None (or its serde default) instead of failing the whole state;
an account without an email is skipped.
"""

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState

STATE_FILE: Final = Path("Library") / "Application Support" / "uz.claudemanager.desktop" / "state.json"
STATE_MAX_BYTES: Final = 1024 * 1024
DEFAULT_AUTO_SWITCH: Final = True  # `Persisted::default()`: serde fills a missing field with it
UNKNOWN_STATUS: Final = "unknown"


def _dict(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _pct(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _instant(value: object) -> datetime | None:
    """chrono's `DateTime<Utc>` (RFC 3339, `Z`, up to nanoseconds). A time without an offset is not trusted."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _account(raw: object, *, active_id: str | None) -> ClaudeAccount | None:
    data = _dict(raw)
    email = data.get("email")
    if not isinstance(email, str) or not email:
        return None
    status = data.get("status")
    usage = _dict(data.get("usage"))
    five_hour = _dict(usage.get("fiveHour"))
    seven_day = _dict(usage.get("sevenDay"))
    return ClaudeAccount(
        email=email,
        status=status if isinstance(status, str) and status else UNKNOWN_STATUS,
        five_hour_pct=_pct(five_hour.get("utilization")),
        seven_day_pct=_pct(seven_day.get("utilization")),
        five_hour_resets_at=_instant(five_hour.get("resetsAt")),
        seven_day_resets_at=_instant(seven_day.get("resetsAt")),
        is_active=active_id is not None and data.get("id") == active_id,
    )


def parse_state(raw: bytes) -> ClaudeManagerState:
    """The token-free view of one `state.json`. Raises `ValueError` if it is not a JSON object within the size cap."""
    if len(raw) > STATE_MAX_BYTES:
        msg = "state.json is larger than expected"
        raise ValueError(msg)
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        msg = "state.json is not a JSON object"
        raise ValueError(msg)
    active = data.get("activeId")
    active_id = active if isinstance(active, str) and active else None
    auto_switch = data.get("autoSwitch")
    accounts_raw = data.get("accounts")
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    return ClaudeManagerState(
        active_id=active_id,
        auto_switch=auto_switch if isinstance(auto_switch, bool) else DEFAULT_AUTO_SWITCH,
        accounts=tuple(
            account for raw_account in accounts if (account := _account(raw_account, active_id=active_id)) is not None
        ),
    )
