"""core/money, core/timefmt, core/paths, core/json_value, prompts."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from yordamchi import prompts
from yordamchi.core import json_value, money, timefmt
from yordamchi.core.paths import BotName, Paths


def test_money_is_decimal_in_code_and_micros_in_the_db() -> None:
    assert money.usd_to_micros(Decimal("1.2345675")) == 1_234_568
    assert money.micros_to_usd(1_500_000) == Decimal("1.5")
    assert money.usd_float_to_micros(0.1) == 100_000
    assert money.format_usd(1_234_567) == "$1.23"


def test_time_is_shown_in_the_owner_zone() -> None:
    moment = datetime(2026, 10, 23, 9, 5, tzinfo=UTC)
    assert timefmt.hhmm(moment, "Asia/Tashkent") == "14:05"
    assert timefmt.whole_minutes(179) == 3
    assert timefmt.whole_minutes(-5) == 0
    with pytest.raises(ValueError, match="naive"):
        timefmt.hhmm(datetime(2026, 10, 23, 9, 5), "Asia/Tashkent")  # noqa: DTZ001  # the point of the test
    assert timefmt.local(moment, "UTC").utcoffset() == timedelta(0)
    assert timefmt.require_aware(moment.astimezone(timezone(timedelta(hours=5)))) == moment


def test_paths_follow_the_spec_layout(tmp_path: Path) -> None:
    paths = Paths.defaults(tmp_path)
    data = tmp_path / "Library" / "Application Support" / "Yordamchi"
    assert paths.db_file(BotName.DEV) == data / "db" / "dev.db"
    assert paths.env_file(BotName.PERSONAL) == data / "personal.env"
    assert paths.socket(BotName.DEV) == data / "run" / "dev.sock"
    assert paths.heartbeat(BotName.DEV) == data / "run" / "dev.heartbeat.json"
    assert paths.shims == data / "releases" / "current" / "shims"
    assert paths.probation_flag == data / "run" / "flags" / "probation.json"
    assert paths.session_dir("smoke") == data / "sessions" / "smoke"
    assert paths.log_file("supervisor") == tmp_path / "Library" / "Logs" / "Yordamchi" / "supervisor.jsonl"
    assert paths.launchd_log("dev").name == "uz.yordamchi.dev.launchd.log"
    assert paths.master_key == tmp_path / ".config" / "yordamchi" / "master.key"


def test_json_encoding_is_canonical_and_keeps_uzbek() -> None:
    text = json_value.dumps({"b": [1, 2], "a": "o'zbek — ʻ"})
    assert text == '{"a":"o\'zbek — ʻ","b":[1,2]}'
    assert json_value.loads(text) == {"a": "o'zbek — ʻ", "b": [1, 2]}
    with pytest.raises(ValueError, match="JSON"):
        json_value.dumps(float("nan"))


def test_prompt_slots_must_match_exactly() -> None:
    template = "Persona: {{ persona }}. Reply in {{lang}}."
    assert prompts.fill(template, {"persona": "qisqa", "lang": "uz"}) == "Persona: qisqa. Reply in uz."
    with pytest.raises(prompts.PromptSlotError, match="missing"):
        prompts.fill(template, {"persona": "qisqa"})
    with pytest.raises(prompts.PromptSlotError, match="unused"):
        prompts.fill(template, {"persona": "q", "lang": "uz", "extra": "x"})


def test_prompt_names_are_checked_before_any_file_access() -> None:
    with pytest.raises(ValueError, match="prompt name"):
        prompts.load("../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        prompts.load("dev/no_such_prompt")
