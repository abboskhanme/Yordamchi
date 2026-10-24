"""settings.domain: resolution own row → dev row (shared BOTH keys, personal bot) → default (SPEC §2.3)."""

import pytest

from yordamchi.core.errors import SettingDevOnly, SettingInvalid, SettingPersonalOnly
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.shared.settings import defaults, domain
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import Source, StoredValue

DEV_BOOT = defaults.bootstrap_values(owner_id=424242, dev_group_id=-100123)
PERSONAL_BOOT = defaults.bootstrap_values(owner_id=424242, personal_group_id=-100456)


def _rows(*pairs: tuple[str, JsonValue]) -> dict[str, StoredValue]:
    return {key: StoredValue(key=key, value=value, row_id=n) for n, (key, value) in enumerate(pairs, start=1)}


def _dev(*own: tuple[str, JsonValue]) -> domain.Resolution:
    return domain.resolve(REGISTRY, BotName.DEV, own=_rows(*own), dev={}, bootstrap=DEV_BOOT)


def _personal(
    own: tuple[tuple[str, JsonValue], ...] = (), dev: tuple[tuple[str, JsonValue], ...] = ()
) -> domain.Resolution:
    return domain.resolve(REGISTRY, BotName.PERSONAL, own=_rows(*own), dev=_rows(*dev), bootstrap=PERSONAL_BOOT)


def test_dev_without_rows_uses_defaults_and_bootstrap() -> None:
    resolved = _dev()
    assert resolved.values[defaults.BURST_AFTER_MEDIA_S.key] == 10.0
    assert resolved.values[defaults.ACCESS_OWNER_ID.key] == 424242
    assert resolved.sources[defaults.ACCESS_OWNER_ID.key] is Source.BOOTSTRAP
    assert resolved.values[defaults.VOICE_ENABLED.key] is False
    assert defaults.PERSONA_PERSONAL.key not in resolved.values  # PERSONAL keys are outside the dev bot's scope


def test_dev_own_row_wins() -> None:
    resolved = _dev((defaults.BURST_AFTER_MEDIA_S.key, 6))
    assert resolved.values[defaults.BURST_AFTER_MEDIA_S.key] == 6.0  # an int row is a valid float value
    assert isinstance(resolved.values[defaults.BURST_AFTER_MEDIA_S.key], float)
    assert resolved.sources[defaults.BURST_AFTER_MEDIA_S.key] is Source.OWN


def test_personal_falls_back_to_the_dev_row_for_shared_keys() -> None:
    resolved = _personal(dev=((defaults.BURST_AFTER_MEDIA_S.key, 7.0), (defaults.CAPS_WARN_PCT.key, 90)))
    assert resolved.values[defaults.BURST_AFTER_MEDIA_S.key] == 7.0
    assert resolved.sources[defaults.BURST_AFTER_MEDIA_S.key] is Source.DEV
    assert resolved.values[defaults.CAPS_WARN_PCT.key] == 90


def test_personal_own_row_beats_the_dev_row() -> None:
    resolved = _personal(own=((defaults.BURST_AFTER_MEDIA_S.key, 3.0),), dev=((defaults.BURST_AFTER_MEDIA_S.key, 7.0),))
    assert resolved.values[defaults.BURST_AFTER_MEDIA_S.key] == 3.0
    assert resolved.sources[defaults.BURST_AFTER_MEDIA_S.key] is Source.OWN


def test_per_bot_and_dev_only_values_never_cross_to_the_personal_bot() -> None:
    resolved = _personal(
        dev=(
            (defaults.VOICE_ENABLED.key, False),
            (defaults.ACCESS_OWNER_ID.key, 1),
            (defaults.ROUTING_MIN_CONFIDENCE.key, 0.9),
        )
    )
    assert resolved.values[defaults.VOICE_ENABLED.key] is True  # its own PerBot default
    assert resolved.values[defaults.ACCESS_OWNER_ID.key] == 424242  # its own env file
    assert defaults.ROUTING_MIN_CONFIDENCE.key not in resolved.values


def test_a_stored_value_that_no_longer_validates_is_skipped() -> None:
    resolved = _personal(own=((defaults.BURST_AFTER_MEDIA_S.key, 999),), dev=((defaults.BURST_AFTER_MEDIA_S.key, "x"),))
    assert resolved.values[defaults.BURST_AFTER_MEDIA_S.key] == 10.0
    assert resolved.sources[defaults.BURST_AFTER_MEDIA_S.key] is Source.DEFAULT
    assert set(resolved.skipped) == {
        f"own:{defaults.BURST_AFTER_MEDIA_S.key}",
        f"dev:{defaults.BURST_AFTER_MEDIA_S.key}",
    }


def test_a_missing_bootstrap_value_is_a_wiring_bug() -> None:
    with pytest.raises(LookupError, match=defaults.ACCESS_DEV_GROUP_ID.key):
        domain.resolve(REGISTRY, BotName.DEV, own={}, dev={}, bootstrap={defaults.ACCESS_OWNER_ID.key: 1})


def test_validate_coerces_or_names_the_options() -> None:
    assert domain.validate(defaults.BURST_AFTER_MEDIA_S, 12) == 12.0
    with pytest.raises(SettingInvalid) as caught:
        domain.validate(defaults.BURST_AFTER_MEDIA_S, 120)
    assert (caught.value.reason, caught.value.params["options"]) == ("out_of_range", "2–60")
    with pytest.raises(SettingInvalid) as wrong_type:
        domain.validate(defaults.GATE_SECRET_SCAN_ENABLED, "yes")
    assert wrong_type.value.reason == "wrong_type"
    with pytest.raises(SettingInvalid):
        domain.validate(defaults.ACCESS_OWNER_ID, True)  # a bool is not an int setting value


def test_each_bot_writes_only_keys_in_its_scope() -> None:
    domain.require_writable_by(defaults.BURST_AFTER_MEDIA_S, BotName.PERSONAL)
    with pytest.raises(SettingPersonalOnly):
        domain.require_writable_by(defaults.PERSONA_PERSONAL, BotName.DEV)
    with pytest.raises(SettingDevOnly):
        domain.require_writable_by(defaults.GATE_ASK_CLASSES, BotName.PERSONAL)


def test_weakening_is_judged_per_setting() -> None:
    assert domain.weakens(defaults.CAPS_GOOGLE_MONTHLY_USD, 40, 60)
    assert not domain.weakens(defaults.CAPS_GOOGLE_MONTHLY_USD, 40, 20)
    assert domain.weakens(defaults.GATE_ASK_CLASSES, list(defaults.GATE_CLASSES), list(defaults.GATE_CLASSES[1:]))
    assert domain.weakens(defaults.ACCESS_OWNER_ID, 1, 2)
    assert not domain.weakens(defaults.BURST_AFTER_MEDIA_S, 10.0, 30.0)  # not a safeguard
    assert not domain.weakens(defaults.CAPS_WARN_PCT, 80, 80)


def test_a_snapshot_is_typed_and_scoped() -> None:
    resolved = _dev()
    snap = domain.Snapshot(bot=BotName.DEV, version=0, values=resolved.values, sources=resolved.sources)
    media_s: float = snap[defaults.BURST_AFTER_MEDIA_S]
    classes: list[str] = snap[defaults.GATE_ASK_CLASSES]
    assert (media_s, len(classes)) == (10.0, 12)
    assert snap.source(defaults.BURST_AFTER_MEDIA_S) is Source.DEFAULT
    with pytest.raises(KeyError, match="scope"):
        snap[defaults.PERSONA_PERSONAL]
