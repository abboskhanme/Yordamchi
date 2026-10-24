"""The "sozlamalaring qanday?" format (UC-09; CONVENTIONS §11 golden output): a fixed slice of the real registry with
a few changes, one per value kind, and a key pasted into a text setting (masked).

The slice keeps the golden stable while other features add settings; `test_service` checks the full registry. After a
reviewed format change, regenerate the files with
`uv run python -c "from tests.unit.shared.settings.test_listing import write_goldens; write_goldens()"`.
"""

from pathlib import Path
from typing import Final

from tests.factories import FAKE_KEYS
from yordamchi.core import redact
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.shared.settings import defaults, domain
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import SettingsListing, StoredValue
from yordamchi.shared.settings.registry import Registry

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = ROOT / "tests" / "golden" / "settings"
BOOTSTRAP = defaults.bootstrap_values(owner_id=424242, dev_group_id=-1001234567890)
KEYS: Final = (
    "access.owner_id",
    "access.dev_group_id",
    "persona.dev_tone",
    "prompt.dev_executor_extra",
    "voice.enabled",
    "models.routing",
    "projects.aliases",
    "gate.ask_classes",
    "gate.secret_scan_enabled",
    "burst.after_media_s",
    "timeouts.routing_s",
    "schedule.quiet_hours",
    "caps.google_monthly_usd",
    "assistant.warm_clients",
    "servers.hosts",
)
CHANGES: Final[dict[str, JsonValue]] = {
    "persona.dev_tone": "qisqa va iliq",
    "prompt.dev_executor_extra": f"Testlarni ishga tushir. kalit: {FAKE_KEYS['anthropic_api_key']}",
    "models.routing": "sonnet",
    "projects.aliases": {"hr": "BonviHR"},
    "gate.ask_classes": ["deploy", "server_write", "spend"],
    "burst.after_media_s": 15.0,
    "schedule.quiet_hours": "23:00-07:00",
    "servers.hosts": ["bonvi-srv"],
}


def golden_listing() -> SettingsListing:
    registry = Registry(REGISTRY[key] for key in KEYS)
    own = {key: StoredValue(key=key, value=value, row_id=n) for n, (key, value) in enumerate(CHANGES.items(), 1)}
    values = domain.resolve(registry, BotName.DEV, own=own, dev={}, bootstrap=BOOTSTRAP).values
    default_values = {key: domain.default_for(REGISTRY[key], BotName.DEV, BOOTSTRAP) for key in values}
    return domain.listing(registry, BotName.DEV, values, default_values, redact.scrubbed)


def write_goldens() -> None:
    listing = golden_listing()
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / "summary_dev.txt").write_text(listing.summary + "\n", encoding="utf-8")
    (GOLDEN / "full_dev.txt").write_text(listing.full + "\n", encoding="utf-8")


def test_the_grouped_summary_matches_the_golden_file() -> None:
    expected = (GOLDEN / "summary_dev.txt").read_text(encoding="utf-8")
    assert golden_listing().summary + "\n" == expected, "see the module docstring to regenerate"


def test_the_full_list_matches_the_golden_file() -> None:
    expected = (GOLDEN / "full_dev.txt").read_text(encoding="utf-8")
    assert golden_listing().full + "\n" == expected, "see the module docstring to regenerate"


def test_the_golden_slice_masks_the_pasted_key() -> None:
    listing = golden_listing()
    assert FAKE_KEYS["anthropic_api_key"] not in listing.summary + listing.full
    assert (listing.total, listing.changed) == (len(KEYS), len(CHANGES))
