"""audit.domain: kinds and actors (SPEC §4.2), scrubbed `data_json`, the 90-day cutoff (NFR-SEC-10)."""

from datetime import UTC, datetime, timedelta

from yordamchi.core.json_value import JsonObject
from yordamchi.shared.audit import domain
from yordamchi.shared.audit.domain import AuditActor, AuditKind

# Copied from SPEC §4.2 `audit_events.kind`.
SPEC_KINDS = (
    "executor_input gate_verdict decision_asked decision_resolved grant_consumed setting_changed integration_changed "
    "secret_added secret_removed session_opened session_closed self_update access_dropped"
)


def test_kinds_and_actors_are_exactly_the_spec_sets() -> None:
    assert {kind.value for kind in AuditKind} == set(SPEC_KINDS.split())
    assert {actor.value for actor in AuditActor} == {"owner", "executor", "system", "supervisor"}


def test_clean_data_passes_every_string_and_key_through_the_scrubber() -> None:
    data: JsonObject = {"command": "curl -H x", "nested": {"argv": ["a", "b"]}, "count": 2, "ok": True}
    assert domain.clean_data(data, str.upper) == {
        "COMMAND": "CURL -H X",
        "NESTED": {"ARGV": ["A", "B"]},
        "COUNT": 2,
        "OK": True,
    }


def test_events_are_kept_90_days() -> None:
    now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    assert domain.retention_cutoff(now) == now - timedelta(days=90)
