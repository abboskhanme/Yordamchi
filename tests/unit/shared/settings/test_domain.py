"""settings.domain: resolution own row → dev row (shared BOTH keys, personal bot) → default (SPEC §2.3); the change
plan, the pending change, the chat interpretation and the owner's words (SPEC §3.2.15; UC-09)."""

import pytest

from yordamchi.core import strings as S
from yordamchi.core.errors import SettingDevOnly, SettingInvalid, SettingPersonalOnly, SettingUnknown
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.shared.settings import defaults, domain
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.domain import Source, StoredValue
from yordamchi.shared.settings.registry import AnySetting, DictOf, Group, SettingValue

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


# --- revert ---------------------------------------------------------------------------------------------------------
def test_value_before_is_the_previous_row_then_the_dev_row_then_the_default() -> None:
    key = defaults.BURST_AFTER_MEDIA_S.key
    newest, older = StoredValue(key, 8.0, 2), StoredValue(key, 6.0, 1)
    setting = defaults.BURST_AFTER_MEDIA_S
    assert domain.value_before(setting, BotName.DEV, history=[newest, older], dev=None, bootstrap=DEV_BOOT) == 6.0
    assert domain.value_before(setting, BotName.DEV, history=[newest], dev=None, bootstrap=DEV_BOOT) == 10.0
    dev_row = StoredValue(key, 4.0, 9)
    assert domain.value_before(setting, BotName.PERSONAL, history=[newest], dev=dev_row, bootstrap=PERSONAL_BOOT) == 4.0


# --- changes --------------------------------------------------------------------------------------------------------
DEV_NOW = _dev().values


def test_a_plan_drops_unchanged_values_and_flags_weakening() -> None:
    planned = domain.plan_changes(
        REGISTRY,
        BotName.DEV,
        DEV_NOW,
        [
            domain.Proposal(defaults.BURST_AFTER_MEDIA_S.key, 10),  # already so
            domain.Proposal(defaults.CAPS_GOOGLE_MONTHLY_USD.key, 20),
            domain.Proposal(defaults.CAPS_GOOGLE_MONTHLY_USD.key, 100),  # the last value of a key wins
            domain.Proposal(defaults.GATE_SECRET_SCAN_ENABLED.key, False),
            domain.Proposal(defaults.ROUTING_MIN_CONFIDENCE.key, 0.8),
        ],
    )
    assert [(change.setting.key, change.old, change.new, change.weakens) for change in planned] == [
        (defaults.CAPS_GOOGLE_MONTHLY_USD.key, 40, 100, True),
        (defaults.GATE_SECRET_SCAN_ENABLED.key, True, False, True),
        (defaults.ROUTING_MIN_CONFIDENCE.key, 0.75, 0.8, False),
    ]


@pytest.mark.parametrize(
    ("proposal", "error"),
    [
        (domain.Proposal("burst.nonexistent_s", 1), SettingUnknown),
        (domain.Proposal(defaults.PERSONA_PERSONAL.key, "x"), SettingPersonalOnly),
        (domain.Proposal(defaults.BURST_AFTER_MEDIA_S.key, 999), SettingInvalid),
    ],
)
def test_one_refused_proposal_refuses_the_whole_plan(proposal: domain.Proposal, error: type[Exception]) -> None:
    fine = domain.Proposal(defaults.ROUTING_MIN_CONFIDENCE.key, 0.8)
    with pytest.raises(error):
        domain.plan_changes(REGISTRY, BotName.DEV, DEV_NOW, [fine, proposal])


@pytest.mark.parametrize(
    ("setting", "value", "shown"),
    [
        (defaults.GATE_SECRET_SCAN_ENABLED, False, "o'chirilgan"),
        (defaults.VOICE_ENABLED, True, "yoqilgan"),
        (defaults.BURST_AFTER_MEDIA_S, 15.0, "15 s"),
        (defaults.TIMEOUTS_ROUTING_S, 2.5, "2.5 s"),
        (defaults.ROUTING_MIN_CONFIDENCE, 0.75, "0.75"),
        (defaults.CAPS_GOOGLE_MONTHLY_USD, 40, "40 USD"),
        (defaults.SCHEDULE_QUIET_HOURS, "", "bo'sh"),
        (defaults.SERVERS_HOSTS, [], "bo'sh"),
        (defaults.GATE_JUMP_ONLY_HOSTS, ["server", "bonvi-srv"], "server, bonvi-srv"),
        (defaults.PROJECTS_ALIASES, {"hr": "BonviHR"}, "hr → BonviHR"),
        (defaults.PROMPT_DEV_EXECUTOR_EXTRA, "bir\n  ikki", "bir ikki"),
    ],
)
def test_values_read_in_the_owners_words(setting: AnySetting, value: SettingValue, shown: str) -> None:
    assert domain.show_value(setting, value) == shown


def test_a_key_cut_at_the_limit_never_leaks_its_first_part() -> None:
    secret = "sk-ant-api03-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe"  # tests.factories.FAKE_KEYS, split as there
    text = "x" * 40 + " kalit: " + secret
    shown = domain.show_value(defaults.PROMPT_DEV_EXECUTOR_EXTRA, text, limit=60)
    assert secret[:8] not in shown
    assert shown.endswith("••••g0Fe")


def test_a_long_value_is_cut_and_a_change_is_restated_in_one_line() -> None:
    assert domain.show_value(defaults.PERSONA_DEV_TONE, "x" * 200, limit=10) == "x" * 9 + "…"
    planned = domain.plan_changes(
        REGISTRY,
        BotName.DEV,
        DEV_NOW,
        [domain.Proposal(defaults.BURST_AFTER_MEDIA_S.key, 15), domain.Proposal(defaults.MODELS_ROUTING.key, "sonnet")],
    )
    assert domain.restate(planned) == "burst.after_media_s: 10 s → 15 s; models.routing: haiku → sonnet"


def _pending(*proposals: domain.Proposal, reason: domain.ChangeReason = "change") -> domain.PendingChange:
    planned = domain.plan_changes(REGISTRY, BotName.DEV, DEV_NOW, proposals)
    return domain.pending_change(planned, reason=reason, restated=domain.restate(planned), origin_update_id=77)


def test_a_pending_change_survives_its_json_and_names_its_action() -> None:
    pending = _pending(domain.Proposal(defaults.GATE_ASK_CLASSES.key, ["deploy"]))
    assert domain.PendingChange.from_json(pending.to_json()) == pending
    assert pending.proposals() == (domain.Proposal(defaults.GATE_ASK_CLASSES.key, ["deploy"]),)
    assert pending.items[0].old == list(defaults.GATE_CLASSES)
    same = _pending(domain.Proposal(defaults.GATE_ASK_CLASSES.key, ["deploy"]))
    other = _pending(domain.Proposal(defaults.GATE_ASK_CLASSES.key, ["spend"]))
    reverted = _pending(domain.Proposal(defaults.GATE_ASK_CLASSES.key, ["deploy"]), reason="revert")
    assert pending.action_sha256 == same.action_sha256
    assert len({pending.action_sha256, other.action_sha256, reverted.action_sha256}) == 3


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"reason": "drop", "items": [{"key": "a.b", "value": 1}], "restated": "", "origin_update_id": 1},
        {"reason": "change", "items": [], "restated": "", "origin_update_id": 1},
        {"reason": "change", "items": [{"key": "a.b"}], "restated": "", "origin_update_id": 1},
        {"reason": "change", "items": [{"key": "a.b", "value": 1}], "restated": "", "origin_update_id": True},
    ],
)
def test_a_pending_change_refuses_what_it_did_not_write(raw: JsonValue) -> None:
    assert domain.PendingChange.from_json(raw) is None


# --- the chat interpretation ----------------------------------------------------------------------------------------
def _answer(outcome: str, changes: list[JsonValue] | None = None, keys: list[JsonValue] | None = None) -> JsonValue:
    return {"outcome": outcome, "changes": changes or [], "keys": keys or []}


def test_a_change_answer_names_registry_keys_with_values_in_their_type() -> None:
    parsed = domain.parse_interpretation(
        REGISTRY,
        _answer(
            "change",
            [
                {"key": "burst.after_media_s", "value": "15"},
                {"key": "gate.secret_scan_enabled", "value": "false"},
                {"key": "caps.warn_pct", "value": "90"},
            ],
        ),
    )
    assert parsed == domain.Interpretation(
        domain.Intent.CHANGE,
        proposals=(
            domain.Proposal("burst.after_media_s", 15.0),
            domain.Proposal("gate.secret_scan_enabled", False),
            domain.Proposal("caps.warn_pct", 90),
        ),
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "change",
        {"outcome": "delete_everything"},
        _answer("change"),  # nothing to change
        _answer("change", [{"key": "burst.after_media_s", "value": 15}, {"key": "invented.key", "value": 1}]),
        _answer("change", [{"key": "burst.after_media_s"}]),
        _answer("clarify", [{"key": "invented.key", "value": 1}]),
        _answer("revert", keys=["invented.key"]),
    ],
)
def test_an_answer_that_is_not_about_known_settings_is_unknown(raw: JsonValue) -> None:
    assert domain.parse_interpretation(REGISTRY, raw).intent is domain.Intent.UNKNOWN


def test_clarify_keeps_known_candidates_and_revert_keeps_named_keys() -> None:
    clarify = domain.parse_interpretation(
        REGISTRY,
        _answer("clarify", [{"key": "models.report", "value": "haiku"}, {"key": "invented.key", "value": 1}]),
    )
    assert clarify.proposals == (domain.Proposal("models.report", "haiku"),)
    revert = domain.parse_interpretation(REGISTRY, _answer("revert", keys=["voice.gender", "voice.gender"]))
    assert (revert.intent, revert.keys) == (domain.Intent.REVERT, ("voice.gender",))
    assert domain.parse_interpretation(REGISTRY, _answer("revert")).keys == ()
    assert domain.parse_interpretation(REGISTRY, _answer("needs_code")).intent is domain.Intent.NEEDS_CODE
    assert domain.parse_interpretation(REGISTRY, _answer("show")).intent is domain.Intent.SHOW


def test_clarify_options_drop_what_this_bot_cannot_apply() -> None:
    options = domain.clarify_options(
        REGISTRY,
        DEV_NOW,
        [
            domain.Proposal("models.report", "sonnet"),
            domain.Proposal("timeouts.report_s", 99),  # out of range
            domain.Proposal("persona.personal", "x"),  # the personal bot's
            domain.Proposal("models.routing", "haiku"),  # already so
            domain.Proposal("timeouts.report_s", 2),
        ],
    )
    assert options == ("1) models.report: haiku → sonnet", "2) timeouts.report_s: 3.5 s → 2 s")


def test_the_schema_offers_every_intent() -> None:
    properties = domain.INTERPRETATION_SCHEMA["properties"]
    assert isinstance(properties, dict)
    outcome = properties["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["enum"] == [intent.value for intent in domain.Intent]


def test_the_models_table_lists_every_setting_and_only_this_bots_current_values() -> None:
    table = domain.context_listing(REGISTRY, BotName.DEV, DEV_NOW, DEV_BOOT, lambda text: text.replace("Kore", "••"))
    rows = table.splitlines()
    assert len(rows) == len(REGISTRY)
    assert "| burst.after_media_s | float | both | 10.0 | 10.0 | 2–60 | Quiet window" in table
    assert "| persona.personal | str | personal | - |" in table  # listed, so the model can name the right bot
    assert "| access.personal_group_id | int | personal | - | 0 |" in table
    assert "Kore" not in table  # every row passes the scrubber


# --- the listing ----------------------------------------------------------------------------------------------------
def test_every_group_has_an_uzbek_label() -> None:
    assert sorted(S.SETTINGS_GROUPS) == sorted(group.value for group in Group)
    assert sorted(S.SETTINGS_BOTS) == sorted(bot.value for bot in BotName)


def test_dict_of_checks_keys_and_values() -> None:
    check = DictOf(["routing", "report"], ["0", "1", "2"])
    assert check.problem({"routing": "2"}) is None
    assert check.problem({"routing": "9"}) == "not_an_option"
    assert check.problem({"qa": "1"}) == "unknown_key"
    assert check.problem(["routing"]) == "not_a_dict"
    assert check.options() == "routing, report → 0 | 1 | 2"
    assert domain.problem(defaults.ASSISTANT_WARM_CLIENTS, {"routing": "3"}) == "not_an_option"
