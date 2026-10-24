"""The previous release's DB (`fixtures/p1_release.db`) and what every later revision owes it (CONVENTIONS §8; TASKS
migration plan; SPEC §12 alembic rows; the P2 DoD, T60).

The fixture is a `dev.db` at `RELEASE_REVISION` full of rows (`p1_release.py` builds it). Upgrading a copy of it to
head, one revision at a time, must keep every table, column, row and value byte for byte, leave new columns NULL or at
their server default on old rows, and end at a schema `alembic check` accepts. How a new revision adds its own checks:
`test_revision_template.py`.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests.factories import DEV_BOT_TOKEN, FAKE_KEYS, INTEGRATION_TOKEN, PERSONAL_BOT_TOKEN
from tests.migrations import p1_release
from tests.migrations.harness import (
    P1_RELEASE,
    SYNTHETIC,
    alembic_config,
    foreign_key_violations,
    integrity,
    lost_data,
    new_column_problems,
    read_tables,
    release_at_head,
    release_copy,
    revision_of,
    revisions_after,
    schema_of,
    scratch_chain,
    script_directory,
    side_files,
    synthetic_revision,
    upgrade,
)

REGENERATE = "uv run --locked python -m tests.migrations.p1_release"


def test_the_release_db_is_one_checkpointed_file(tmp_path: Path) -> None:
    """`*.db-wal` is git-ignored, so rows left in a WAL file would silently be missing from the commit."""
    header = P1_RELEASE.read_bytes()[:100]
    assert header.startswith(b"SQLite format 3\x00")
    assert header[18:20] == b"\x01\x01", "the fixture is in WAL mode; rebuild it: " + REGENERATE
    assert side_files(P1_RELEASE) == []
    assert integrity(release_copy(tmp_path)) == "ok"


def test_the_release_db_matches_the_schema_of_its_revision(tmp_path: Path) -> None:
    """Until T43 freezes `0001`, the merge train regenerates it on every merge that touches a model; the fixture must
    follow (MT step 2 + this command), or it would claim a revision whose tables it lacks."""
    copy = release_copy(tmp_path)
    assert revision_of(copy) == (p1_release.RELEASE_REVISION,)
    assert script_directory().get_revision(p1_release.RELEASE_REVISION) is not None
    fresh = tmp_path / "fresh.db"
    upgrade(fresh, p1_release.RELEASE_REVISION)
    assert schema_of(copy) == schema_of(fresh), f"{P1_RELEASE.name} is stale; regenerate it: {REGENERATE}"


def test_every_table_and_column_of_the_release_holds_data(tmp_path: Path) -> None:
    """A table without rows, or a column that is NULL everywhere, could be lost by a migration without notice."""
    assert p1_release.coverage_gaps(release_copy(tmp_path)) == []


def test_the_release_has_finished_and_in_flight_rows_side_by_side(tmp_path: Path) -> None:
    tables = read_tables(release_copy(tmp_path))
    statuses = {
        name: {row[table.columns.index("status")] for row in table.rows}
        for name, table in tables.items()
        if "status" in table.columns
    }
    assert {"routed", "cancelled", "closed", "open"} <= statuses["bursts"]
    assert {"received", "attached"} <= statuses["inbound_updates"]
    assert {"pending", "done", "failed"} <= statuses["transcripts"]
    assert {"pending", "sent", "failed"} <= statuses["outbox_messages"]
    assert {"queued", "waiting_question", "done", "failed"} <= statuses["turns"]
    assert {"pending", "delivered"} <= statuses["deliveries"]
    assert {"open", "answered"} <= statuses["questions"]
    assert {"pending", "consumed", "superseded"} <= statuses["decisions"]
    assert {"open", "resolved"} <= statuses["clarifications"]


def test_the_release_holds_no_plaintext_secret() -> None:
    """The vault and the OAuth grants hold ciphertext only (NFR-SEC-02), so the committed file carries no key."""
    data = P1_RELEASE.read_bytes()
    for value in (*FAKE_KEYS.values(), INTEGRATION_TOKEN, DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN):
        assert value.encode() not in data


async def test_the_builder_reproduces_the_committed_release(tmp_path: Path) -> None:
    """Every seeder still runs, and the committed file is exactly what they write (sealed bytes aside)."""
    if revisions_after(p1_release.RELEASE_REVISION):
        with pytest.raises(p1_release.FixtureFrozen):
            await p1_release.build(tmp_path / "rebuilt.db", workdir=tmp_path / "work")
        return
    rebuilt = tmp_path / "rebuilt.db"
    await p1_release.build(rebuilt, workdir=tmp_path / "work")
    copy = release_copy(tmp_path)
    assert schema_of(rebuilt) == schema_of(copy), f"regenerate the fixture: {REGENERATE}"
    fresh, committed = read_tables(rebuilt), read_tables(copy)
    for name, table in committed.items():
        keep = [column for column in table.columns if (name, column) not in p1_release.RANDOM_COLUMNS]
        assert fresh[name].project(keep) == table.project(keep), f"{name} differs; regenerate: {REGENERATE}"


async def test_upgrading_the_release_to_head_loses_no_data(tmp_path: Path) -> None:
    """One revision at a time, so a loss names the revision that caused it; then head must match the models."""
    copy = release_copy(tmp_path)
    before = read_tables(copy)
    for script in revisions_after(p1_release.RELEASE_REVISION):
        upgrade(copy, script.revision)
        after = read_tables(copy, order_like=before)
        assert lost_data(before, after) == [], f"revision {script.revision} lost data"
        assert new_column_problems(copy, before, after) == [], f"revision {script.revision} backfilled old rows"
    upgrade(copy)
    assert revision_of(copy) == tuple(script_directory().get_heads())
    assert lost_data(before, read_tables(copy, order_like=before)) == []
    command.check(alembic_config(copy))  # raises AutogenerateDiffsDetected if the upgraded release drifts
    assert foreign_key_violations(copy) == []
    assert integrity(copy) == "ok"


@pytest.mark.parametrize(
    ("body", "lost"),
    [
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n    batch_op.drop_column("natija")',
            "turns: columns gone ['natija']",
            id="drop_column",
        ),
        pytest.param(
            "op.execute(\"DELETE FROM questions WHERE status = 'open'\")", "questions: 2 rows before", id="delete"
        ),
        pytest.param("op.execute(\"UPDATE projects SET description = ''\")", "projects: row changed", id="overwrite"),
        pytest.param(
            'with op.batch_alter_table("usage_records", schema=None) as batch_op:\n'
            '    batch_op.alter_column("duration_ms", existing_type=sa.Integer(), type_=sa.Float())',
            "usage_records: row changed",  # 900 → 900.0: equal in Python, a different type on disk
            id="integer_to_real",
        ),
    ],
)
def test_the_no_loss_check_catches_a_lossy_revision(tmp_path: Path, body: str, lost: str) -> None:
    """The check above would pass vacuously if it could not see a loss; here are four it must see. (A parent-table
    rebuild used to be a fifth: since migrations run with foreign_keys=OFF it no longer loses child rows, which
    `test_expand_only.py::test_a_table_rebuild_keeps_every_child_row` now proves.)"""
    location = scratch_chain(tmp_path, {f"{SYNTHETIC}.py": synthetic_revision(body)})
    copy = release_at_head(tmp_path)
    before = read_tables(copy)
    upgrade(copy, SYNTHETIC, script_location=location)
    problems = lost_data(before, read_tables(copy, order_like=before))
    assert any(problem.startswith(lost) for problem in problems), problems


def test_a_backfill_of_old_rows_is_caught(tmp_path: Path) -> None:
    body = (
        'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
        '    batch_op.add_column(sa.Column("t40_reviewed", sa.Integer(), nullable=True))\n'
        'op.execute("UPDATE turns SET t40_reviewed = 1")'
    )
    location = scratch_chain(tmp_path, {f"{SYNTHETIC}.py": synthetic_revision(body)})
    copy = release_at_head(tmp_path)
    before = read_tables(copy)
    upgrade(copy, SYNTHETIC, script_location=location)
    after = read_tables(copy, order_like=before)
    assert lost_data(before, after) == []
    assert new_column_problems(copy, before, after) == [
        f"turns.t40_reviewed: {len(before['turns'].rows)} existing rows were backfilled (expected NULL)"
    ]


def test_the_release_copy_never_touches_the_committed_file(tmp_path: Path) -> None:
    committed = P1_RELEASE.read_bytes()
    copy = release_copy(tmp_path)
    upgrade(copy)
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute("DELETE FROM poll_offsets")
    assert P1_RELEASE.read_bytes() == committed
    assert side_files(P1_RELEASE) == []
