"""core/log: JSON lines, every field and traceback scrubbed (CONVENTIONS §7)."""

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories import DEV_BOT_TOKEN
from yordamchi.core import log as ylog


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


@pytest.mark.usefixtures("restore_root_logger")
def test_events_are_json_lines_with_every_field_scrubbed(tmp_path: Path) -> None:
    buffer = io.StringIO()
    log_file = tmp_path / "logs" / "dev.jsonl"
    runtime = ylog.configure(process="dev", version="2026.10.23-abc1234", stream=buffer, file=log_file)
    log = ylog.get_log("yordamchi.test")
    log.info("turn_finished", project="BonviHR", turn_id=17, url=f"https://api.telegram.org/bot{DEV_BOT_TOKEN}/x")
    log.warning("odd_fields", level="shadow", ids=[1, 2])
    logging.getLogger("aiohttp.client").warning("request to %s failed", f"bot{DEV_BOT_TOKEN}")
    try:
        raise RuntimeError(DEV_BOT_TOKEN)
    except RuntimeError:
        log.exception("loop_failed", loop="poller")
    runtime.stop()  # drains the queue listener

    lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
    assert [line["event"] for line in lines] == ["turn_finished", "odd_fields", "library_log", "loop_failed"]
    assert (lines[0]["project"], lines[0]["turn_id"], lines[0]["process"]) == ("BonviHR", 17, "dev")
    assert lines[1]["field_level"] == "shadow"  # a field may not overwrite a reserved key
    assert lines[1]["ids"] == [1, 2]
    assert "RuntimeError" in str(lines[3]["exc"])
    assert DEV_BOT_TOKEN not in buffer.getvalue()
    assert DEV_BOT_TOKEN not in log_file.read_text(encoding="utf-8")


@pytest.mark.usefixtures("restore_root_logger")
def test_disabled_levels_cost_nothing() -> None:
    buffer = io.StringIO()
    runtime = ylog.configure(process="personal", version="v", stream=buffer, level=logging.WARNING)
    ylog.get_log("yordamchi.test").info("not_written")
    runtime.stop()
    assert buffer.getvalue() == ""
