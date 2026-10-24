"""The replay keys of the eval suites (`tests/eval/recorder.py`): routing keeps `fixture_key`; the settings and
injection suites key on the owner's text and leave the settings table out, and report table drift instead."""

import json
from dataclasses import replace
from pathlib import Path

from tests.eval.recorder import drift_report, owner_text_key, table_keys, table_of, write_fixture
from tests.fakes.assistant import FakeAssistant, fixture_key
from yordamchi.ports.assistant import AssistantReply, AssistantRequest

TABLE_V1 = (
    "Current instructions from the owner:\n| key | type |\n|---|---|\n| voice.gender | str |\n| caps.warn_pct | int |"
)
TABLE_V2 = (
    "Current instructions from the owner:\n| key | type |\n|---|---|\n| voice.gender | str |\n| servers.hosts | list |"
)
ASK = AssistantRequest(
    kind="settings",
    system_prompt="Fixed settings role (test data).",
    prompt="ovozingni ayolga o'zgartir",
    model="haiku",
    settings_version=3,
    output_schema={"type": "object"},
    instructions=TABLE_V1,
    prompt_version=3,
)
REPLY = AssistantReply(text="{}", provider="claude", model="haiku", duration_ms=1, structured={"outcome": "unknown"})


def test_the_owner_text_key_leaves_the_settings_table_and_the_model_out() -> None:
    key = owner_text_key("settings")
    assert key(ASK) == key(replace(ASK, instructions=TABLE_V2, settings_version=9, prompt_version=9, model="sonnet"))


def test_the_owner_text_key_changes_with_what_the_model_answers_to() -> None:
    key = owner_text_key("settings")
    changed = [
        replace(ASK, prompt="ovozingni erkakka o'zgartir"),
        replace(ASK, system_prompt="Fixed settings role, version 2."),
        replace(ASK, output_schema={"type": "object", "required": ["outcome"]}),
        replace(ASK, kind="routing"),
    ]
    assert len({key(ASK), *(key(request) for request in changed), owner_text_key("injection")(ASK)}) == 6


def test_routing_keeps_the_full_key() -> None:
    assert FakeAssistant().key is fixture_key
    assert fixture_key(ASK) != fixture_key(replace(ASK, instructions=TABLE_V2))  # the table is in it


def test_a_fixture_keeps_the_table_it_was_recorded_against(tmp_path: Path) -> None:
    path = write_fixture(tmp_path, ASK, REPLY, case="c1", source="test", key=owner_text_key("settings"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["table_keys"] == ["voice.gender", "caps.warn_pct"]
    assert data["table_sha256"] == table_of(ASK)["table_sha256"]
    assert path.stem == owner_text_key("settings")(ASK)
    routing = write_fixture(tmp_path / "routing", ASK, REPLY, case="c1", source="test")
    assert "table_keys" not in json.loads(routing.read_text(encoding="utf-8"))  # routing's shape stays as it was


def test_table_keys_reads_only_setting_rows() -> None:
    assert table_keys(TABLE_V2) == ["voice.gender", "servers.hosts"]
    assert table_keys("no table here") == []


def test_drift_names_what_was_added_and_removed_and_is_silent_when_nothing_moved(tmp_path: Path) -> None:
    write_fixture(tmp_path, ASK, REPLY, case="c1", source="test", key=owner_text_key("settings"))
    current = ["voice.gender", "servers.hosts"]
    assert drift_report("settings", tmp_path, {"c1": ASK}, current) == []
    newer = replace(ASK, instructions=TABLE_V2)
    assert drift_report("settings", tmp_path, {"c1": newer}, current) == [
        "[settings] c1: recorded against an older settings table; added ['servers.hosts'], removed ['caps.warn_pct']"
    ]
