"""`test_registry` (SPEC §4.3; CONVENTIONS §5, §11): every declared setting is sound, every §4.3 key exists."""

from typing import cast

import pytest

from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.shared.settings import defaults, domain
from yordamchi.shared.settings.defaults import REGISTRY
from yordamchi.shared.settings.registry import (
    BOTH,
    AnySetting,
    Check,
    Duration,
    FromBootstrap,
    Group,
    HostList,
    MaxLen,
    OneOf,
    PerBot,
    Range,
    Regex,
    Registry,
    Setting,
    SettingValue,
    SubsetOf,
    TimeOfDay,
    adds_items,
    always,
    decreases,
    disables,
    increases,
    removes_items,
    to_values,
)

SAMPLE_BOOTSTRAP = defaults.bootstrap_values(owner_id=424242, dev_group_id=-100123, personal_group_id=-100456)

# Every key SPEC §4.3 lists in its table or names inline in §2–§5 (copied from the SPEC, not from defaults.py).
# Pasted from SPEC §4.3 as text, so a diff against the SPEC stays readable.
SPEC_KEYS = """
persona.dev_tone persona.personal persona.personal_verbosity prompt.dev_executor_extra prompt.personal_extra
voice.enabled voice.name voice.style voice.gender voice.language_code voice.max_words voice.max_s
speech.stt_provider speech.tts_provider speech.video_provider stt.google_model stt.openrouter_model
stt.openrouter_fallback_model tts.google_model
tts.openrouter_model tts.edge_voice stt.mode stt.split_over_s lang.reply stt.languages stt.extra_terms
models.routing models.report models.report_effort models.settings models.qa models.qa_effort models.coding
models.coding_effort models.investigation models.fallback_text routing.min_confidence routing.min_margin
routing.context_window_min projects.root projects.aliases projects.descriptions projects.archived projects.unarchived
executor.default executor.per_project gate.ask_classes gate.secret_scan_enabled gate.approval_ttl_min
gate.hook_timeout_s gate.readonly_remote_commands gate.jump_only_hosts gate.git_hosts gate.http_write_allow_hosts
gate.temp_roots burst.after_complete_text_s burst.after_text_s burst.after_media_s burst.album_gap_s
burst.after_voice_s burst.hold_max_s burst.max_total_s burst.merge_min_confidence timeouts.routing_s timeouts.report_s
timeouts.stt_s timeouts.tts_s timeouts.integration_validate_s timeouts.session_open_s questions.hold_process_min
park.grace_s schedule.heartbeat_edit_min schedule.digest_active_poll_s schedule.digest_idle_poll_min
schedule.server_check_min schedule.cost_summary_at schedule.quiet_hours schedule.retention_at ui.timezone
caps.google_monthly_usd caps.openrouter_monthly_usd caps.openai_monthly_usd caps.warn_pct turns.max_concurrent
turns.max_wall_min turns.max_steps turns.max_same_failure guard.min_free_disk_gb guard.block_disk_gb text.max_chars
text.max_lines outbox.max_parts outbox.progress_edit_s media.retention_days servers.hosts access.owner_id
access.dev_group_id access.personal_group_id ui.seen_reaction digest.stale_after_min newproject.draft_silence_s
newproject.plan_max_min handoff.scan_s integrations.oauth_redirect_port integrations.oauth_paste_timeout_min
integrations.oauth_client_metadata_url selfupdate.drain_max_min selfupdate.health_timeout_s qa.draft_interval_s
qa.session_idle_rotate_h sessions.idle_close_min turns.result_grace_s
""".split()  # noqa: SIM905
# SPEC §5.7 gives the shim window inline ("in the last 120 s"); the T07 wiring note named its key (T18).
SPEC_KEYS.append("gate.shim_grant_window_s")
# SPEC §5.3 gives the dev Assistant limit as `timeouts.<kind>_s`; the settings kind needs its own (T21). The T12 wiring
# notes name the warm-pool size per kind and the routing effort (orchestrator decision 2026-10-24).
SPEC_KEYS.extend(["timeouts.settings_s", "assistant.warm_clients", "models.routing_effort"])
# SPEC §5.6 (revised after T22) names the scheduler's pressure threshold inline (T24).
SPEC_KEYS.append("guard.max_pressure_level")
# SPEC §3.2.4 server checks (T31): log paths, the "disk ≥ 90 %" style thresholds, and the bounds the T22 wiring note
# asks for (one command; one host's check within UC-15's 3 min).
SPEC_KEYS.extend(
    [
        "servers.log_paths",
        "servers.disk_warn_pct",
        "servers.memory_warn_pct",
        "timeouts.server_command_s",
        "timeouts.server_check_s",
    ]
)
# SPEC §3.2.12 (T56): "No supervisor heartbeat within 60 s → it switches back"; the dev bot is the watcher.
SPEC_KEYS.append("selfupdate.supervisor_switch_s")
# T51 caps (NFR-COST-02): Google STT reports no cost, and T19 left its price to the caps; a lower price lets more real
# spend through the Google cap, so it is a safeguard too.
SPEC_KEYS.append("cost.google_stt_usd_micros_per_min")

# SPEC §3.2.14 gives the artefact limits inline (1280×800, ≤ 50 MB, 49/46 MB, 720/480p, 64 kbps audio); T74 names them.
SPEC_KEYS.extend(
    [
        "web.viewport_width_px",
        "web.viewport_height_px",
        "web.file_max_mb",
        "web.video_send_max_mb",
        "web.video_target_mb",
        "web.video_download_max_mb",
        "web.video_max_height_px",
        "web.video_low_height_px",
        "web.video_audio_kbps",
        "web.video_min_kbps",
        "timeouts.screenshot_s",
        "timeouts.fetch_file_s",
        "timeouts.video_download_s",
        "timeouts.video_encode_s",
    ]
)
# SPEC §3.2.14 gives the media limits inline (video ≤ 20 MB, result ≤ 60 s) and §3.2.11 the video provider; T73 names
# the reader models and the page limits.
SPEC_KEYS.extend(
    [
        "video.google_model",
        "video.openrouter_model",
        "media.video_max_mb",
        "media.video_max_min",
        "media.page_max_chars",
        "media.page_max_mb",
        "timeouts.media_s",
    ]
)

# T71 (voice_tts): the S8 sentence-segment fallback and the voices that follow `voice.gender` (SPEC §3.2.13, §11 S8).
SPEC_KEYS.extend(["tts.segment_words", "tts.female_voice", "tts.male_voice", "tts.edge_male_voice"])
# SPEC §3.2.10 step 5 gives the OAuth refresh rule inline (lead 10 min, loop 2 min, two failures) (T50).
SPEC_KEYS.extend(["integrations.refresh_every_s", "integrations.refresh_lead_min", "integrations.refresh_failures_max"])
# A4 bounds every port call; the GitHub port's calls take theirs from TIMEOUTS (T52, used by the new-project flow).
SPEC_KEYS.append("timeouts.github_s")
# T34b: "kut" never stops anything; it holds the window for the next part, acknowledged with its own reaction.
SPEC_KEYS.append("ui.hold_reaction")
# T34b (owner, 2026-10-24): "typing…" for quick answers only; long work has its reaction and progress line instead.
SPEC_KEYS.append("ack.typing_for_work")
# Owner, 2026-10-24: ✓✓ on his group messages with no reaction left (the Bot API has no read method for groups).
SPEC_KEYS.append("ui.read_mark")
# Owner, 2026-10-24: "Ma'lumot: …" under every answer felt formal; it stays where it tells him something.
SPEC_KEYS.append("report.freshness_line")
# Owner, 2026-10-24: OvozYoz is a code sample only; the dev bot must never register or open it.
SPEC_KEYS.append("projects.excluded")

# T70: the bridge read bound (UC-36 "≤ 4 s"). T72: the personal assistant's bounds (SPEC §3.2.13: whole answer,
# stream silence, the fallback model, one draft) and the WebSearch cap and rotation point SPEC §3.2.13 step 10 and
# STACK §4.8 give inline ("env cap raised to 1000", "at 900 WebSearch calls").
SPEC_KEYS.extend(
    [
        "timeouts.bridge_s",
        "timeouts.qa_s",
        "timeouts.qa_idle_s",
        "timeouts.qa_fallback_s",
        "timeouts.qa_draft_s",
        "qa.web_searches_max",
        "qa.web_search_rotate_at",
    ]
)

# SPEC §4.3 and CONVENTIONS §5: these must never change without one question.
SAFEGUARD_PREFIXES = ("access.", "caps.")
SAFEGUARD_KEYS = (
    "gate.ask_classes",
    "gate.secret_scan_enabled",
    "gate.jump_only_hosts",
    "gate.shim_grant_window_s",
    "turns.max_concurrent",
    "guard.max_pressure_level",
    "cost.google_stt_usd_micros_per_min",
)

SETTINGS = sorted(REGISTRY, key=lambda setting: setting.key)


def test_every_spec_key_is_declared() -> None:
    assert sorted(set(SPEC_KEYS) - {setting.key for setting in REGISTRY}) == []


def test_every_declared_key_comes_from_the_spec() -> None:
    assert sorted({setting.key for setting in REGISTRY} - set(SPEC_KEYS)) == []


def _defaults(setting: AnySetting) -> list[SettingValue]:
    default = setting.default
    if isinstance(default, PerBot):
        return [default.dev, default.personal]
    if isinstance(default, FromBootstrap):
        return [SAMPLE_BOOTSTRAP[setting.key]]
    return [default]


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.key)
def test_each_default_passes_its_own_type_and_check(setting: AnySetting) -> None:
    for value in _defaults(setting):
        assert domain.problem(setting, cast(JsonValue, value)) is None, f"{setting.key} default {value!r}"


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.key)
def test_each_setting_is_documented_and_reachable_by_a_bot(setting: AnySetting) -> None:
    assert setting.doc
    assert domain.in_scope(setting, BotName.DEV) or domain.in_scope(setting, BotName.PERSONAL)


def test_every_safeguard_declares_how_it_weakens() -> None:
    guarded = [
        setting for setting in REGISTRY if setting.key.startswith(SAFEGUARD_PREFIXES) or setting.key in SAFEGUARD_KEYS
    ]
    assert len(guarded) >= len(SAFEGUARD_KEYS) + 3 + 4
    assert [setting.key for setting in guarded if setting.weakens is None] == []


def test_claude_model_choices_that_bill_credits_weaken() -> None:
    for setting in REGISTRY:
        if setting.group is Group.MODELS and isinstance(setting.check, OneOf) and "opus" in setting.check.values:
            assert setting.check.values == defaults.CLAUDE_MODELS
            assert domain.weakens(setting, "sonnet", "fable")
            assert not domain.weakens(setting, "fable", "opus")


def test_per_bot_defaults_are_never_shared() -> None:
    assert not defaults.VOICE_ENABLED.shared
    assert not defaults.ACCESS_OWNER_ID.shared
    assert defaults.BURST_AFTER_MEDIA_S.shared


def test_a_registry_refuses_duplicate_keys() -> None:
    twice = Setting("demo.value_s", float, 1.0, group=Group.BURST, bots=BOTH, doc="d")
    with pytest.raises(ValueError, match="twice"):
        Registry([twice, twice])


@pytest.mark.parametrize("key", ["Burst.after", "nogroup", "a.b-c", "x." + "y" * 100])
def test_keys_follow_group_dot_name(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        Setting(key, int, 1, group=Group.BURST, bots=BOTH, doc="d")


def test_only_json_types_are_accepted() -> None:
    with pytest.raises(TypeError, match="non-JSON"):
        # Deliberately outside SettingValue: the runtime check must refuse what mypy would also refuse.
        Setting("demo.items", list[int], [1], group=Group.BURST, bots=BOTH, doc="d")  # type: ignore[type-var]


def test_units_follow_the_key_suffix_unless_given() -> None:
    assert defaults.BURST_AFTER_MEDIA_S.display_unit == "s"
    assert defaults.CAPS_WARN_PCT.display_unit == "%"
    assert defaults.ROUTING_MIN_CONFIDENCE.display_unit == ""


@pytest.mark.parametrize(
    ("check", "good", "bad"),
    [
        (Range(2, 60), 10.0, 61),
        (OneOf("a", "b"), "a", "c"),
        (SubsetOf(["x", "y"]), ["y"], ["x", "x"]),
        (MaxLen(3), "abc", "abcd"),
        (Regex(r"[a-z]+", description="d"), ["ab", "c"], ["ab", "C"]),
        (HostList(), ["server", "bonvi-srv.local"], ["user@host"]),
        (TimeOfDay(), "04:30", "24:00"),
        (TimeOfDay(allow_empty=True), "", "4:30"),
        (Duration(), "23:00-07:00", "23:00-23:00"),
    ],
)
def test_each_check_accepts_and_refuses(check: Check, good: SettingValue, bad: SettingValue) -> None:
    assert check.problem(good) is None
    assert check.problem(bad) is not None
    assert check.options()


def test_weakening_rules() -> None:
    assert always(1, 2)
    assert (increases(10, 40), increases(40, 10)) == (True, False)
    assert (decreases(10, 3), decreases(3, 10)) == (True, False)
    assert (disables(True, False), disables(False, True)) == (True, False)
    assert (removes_items(["a", "b"], ["a"]), removes_items(["a"], ["a", "b"])) == (True, False)
    assert (adds_items(["a"], ["a", "b"]), adds_items(["a", "b"], ["a"])) == (True, False)
    assert (to_values({"fable"})("opus", "fable"), to_values({"fable"})("fable", "fable")) == (True, False)
