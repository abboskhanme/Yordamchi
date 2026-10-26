"""`0002` topics_threads against the previous release (CONVENTIONS §8; from `test_revision_template.py`).

The generic checks (expand-only, the release DB upgraded one revision at a time with nothing lost, `alembic check`)
run for every revision elsewhere. This file holds what `0002` itself promises: the two topic tables, empty; the
`thread_id` columns of `bursts` and `outbox_messages`, NULL (General) on every release row; and the release that is
still running (probation, a rollback window) keeps writing both tables with its own code, which knows no topic.
"""

from pathlib import Path
from typing import Final

from tests.fakes.clock import DEFAULT_START
from tests.migrations.harness import (
    MIGRATIONS,
    lost_data,
    new_column_problems,
    read_tables,
    release_copy,
    script_directory,
    upgrade,
)
from tests.migrations.revision_rules import expand_only_violations
from yordamchi.persistence.engine import open_database
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.outbox import store as outbox_store

REVISION: Final = "0002"
NEW_TABLES: Final = {"project_topics", "topic_chats"}
THREADED: Final = ("bursts", "outbox_messages")


def test_0002_follows_0001_on_the_one_chain_and_only_expands() -> None:
    scripts = script_directory(MIGRATIONS)
    assert REVISION in {script.revision for script in scripts.walk_revisions()}  # one head: test_migrations
    script = scripts.get_revision(REVISION)
    assert script is not None
    assert script.down_revision == "0001"
    assert expand_only_violations(Path(script.path).read_text(encoding="utf-8")) == []


async def test_0002_upgrades_the_previous_release(tmp_path: Path) -> None:
    copy = release_copy(tmp_path)
    upgrade(copy, "0001")
    before = read_tables(copy)
    upgrade(copy, REVISION)
    after = read_tables(copy, order_like=before)
    assert lost_data(before, after) == []
    assert new_column_problems(copy, before, after) == []

    # The revision's own promises: what it creates, and what the release's rows read in it.
    assert set(after) - set(before) == NEW_TABLES
    for name in NEW_TABLES:
        assert after[name].rows == ()
    for name in THREADED:
        threaded = after[name]
        assert set(threaded.columns) - set(before[name].columns) == {"thread_id"}
        assert threaded.rows, f"the release DB has no {name} row to read"
        assert {row[threaded.columns.index("thread_id")] for row in threaded.rows} == {None}  # General

    # The release that is still running writes the new schema with its own code (it knows no topic).
    db = open_database(copy)
    try:
        async with db.uow() as s:
            burst = await burst_store.add_burst(
                s,
                bot="dev",
                chat_id=-1001234567890,
                status="closed",
                opened_at=DEFAULT_START,
                last_part_at=DEFAULT_START,
                closes_at=DEFAULT_START,
                closed_at=DEFAULT_START,
                close_reason="quiet",
                input_json=[],
            )
            row_id = await outbox_store.add_outbox_message(
                s,
                bot="dev",
                chat_id=-1001234567890,
                op="send_text",
                kind="notice",
                payload={"text": "BonviHR: to'xtatdim."},
                dedupe_key="notice:t0002-release:0",
                coalesce_key=None,
                priority=2,
                next_attempt_at=DEFAULT_START,
                reply_to_message_id=None,
                disable_notification=False,
                project_id="BonviHR",
                turn_id=None,
                created_at=DEFAULT_START,
            )
        async with db.read() as s:
            assert (await burst_store.get_burst(s, bot="dev", burst_id=burst.id)) is not None
            assert row_id is not None
            stored = await outbox_store.get_outbox_message(s, row_id)
            assert stored is not None
            assert stored.thread_id is None
    finally:
        await db.dispose()
    tables = read_tables(copy)
    assert len(tables["bursts"].rows) == len(before["bursts"].rows) + 1
    assert len(tables["outbox_messages"].rows) == len(before["outbox_messages"].rows) + 1
