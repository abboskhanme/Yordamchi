"""`tests/eval/thresholds.toml` holds every P1 row of REQUIREMENTS §4 (protected core; never lowered)."""

import tomllib
from pathlib import Path

THRESHOLDS = Path(__file__).with_name("thresholds.toml")
P1_TABLES = {
    "latency",
    "routing",
    "burst",
    "gate",
    "secrets",
    "settings",
    "injection",
    "stt",
    "push_without_asking",
    "fault",
    "load",
}


def test_every_p1_suite_has_its_table_of_numbers() -> None:
    data = tomllib.loads(THRESHOLDS.read_text(encoding="utf-8"))
    assert set(data) >= P1_TABLES
    for table, rows in data.items():
        assert rows, table
        for key, value in rows.items():
            assert isinstance(value, (int, float)), f"{table}.{key}"
            assert not isinstance(value, bool), f"{table}.{key}"


def test_key_acceptance_numbers_match_requirements() -> None:
    data = tomllib.loads(THRESHOLDS.read_text(encoding="utf-8"))
    assert data["routing"]["explicit_correct_min"] == 0.97
    assert data["routing"]["silent_wrong_max"] == 2  # REQUIREMENTS UC-10, owner's decision of 2026-10-24
    assert data["routing"]["cases_min"] == 120
    assert data["latency"]["ack_s"] == 1.0
    assert data["latency"]["interrupt_s"] == 3.0
    assert data["secrets"]["formats_min"] == 10
    assert data["fault"]["kill9_messages"] == 50
