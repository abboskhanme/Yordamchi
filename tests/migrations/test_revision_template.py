"""TEMPLATE: how the next revision is tested against the previous release (CONVENTIONS §8; TASKS migration plan, P2+).

Every revision already gets the generic checks: the expand-only guard (test_expand_only) and "upgrade the release DB
one revision at a time, lose nothing, end at a schema `alembic check` accepts" (test_previous_release). This file adds
what only the revision's author knows: what it creates, what the release's rows read in the new columns, and that the
release that is still running (probation, or a rollback window) keeps working on the new schema.

For a real revision:
1. copy this file to `tests/migrations/test_<rev>_<feature>_<change>.py` and set `REVISION` to its id;
2. delete `TEMPLATE_SOURCE` and make `_chain` return `MIGRATIONS` (the revision lives in `migrations/versions/`);
3. replace the expectations under "the revision's own promises" with yours.

Here it runs on a synthetic revision stacked on the real head, so the template itself stays green and proven.
"""

from pathlib import Path
from typing import Final

from tests.fakes.clock import DEFAULT_START
from tests.migrations.harness import (
    lost_data,
    new_column_problems,
    read_tables,
    release_copy,
    scratch_chain,
    script_directory,
    synthetic_revision,
    upgrade,
)
from tests.migrations.revision_rules import expand_only_violations
from yordamchi.dev.turns import store as turns_store
from yordamchi.persistence.engine import open_database

REVISION: Final = "t40_template"  # a real revision: its id, e.g. "0002"
TEMPLATE_SOURCE: Final = synthetic_revision(
    """op.create_table(
    "t40_turn_reviews",
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("turn_id", sa.Integer(), nullable=False),
    sa.Column("verdict", sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(
        ["turn_id"], ["turns.id"], name=op.f("fk_t40_turn_reviews_turn_id_turns"), ondelete="CASCADE"
    ),
    sa.PrimaryKeyConstraint("id", name=op.f("pk_t40_turn_reviews")),
)
with op.batch_alter_table("turns", schema=None) as batch_op:
    batch_op.add_column(sa.Column("t40_reviewed_at", sa.DateTime(), nullable=True))
    batch_op.add_column(sa.Column("t40_review_rounds", sa.Integer(), server_default="0", nullable=False))
    batch_op.create_index("ix_turns_t40_reviewed_at", ["t40_reviewed_at"], unique=False)""",
    revision=REVISION,
    message="turns_review: a review table, a nullable column and a counter (template)",
)


def _chain(tmp_path: Path) -> Path:
    return scratch_chain(tmp_path, {f"{REVISION}.py": TEMPLATE_SOURCE})  # a real revision: return MIGRATIONS


def test_the_next_revision_is_the_one_head_and_only_expands(tmp_path: Path) -> None:
    scripts = script_directory(_chain(tmp_path))
    assert scripts.get_heads() == [REVISION]
    script = scripts.get_revision(REVISION)
    assert script is not None
    assert expand_only_violations(Path(script.path).read_text(encoding="utf-8")) == []


async def test_the_next_revision_upgrades_the_previous_release(tmp_path: Path) -> None:
    location = _chain(tmp_path)
    script = script_directory(location).get_revision(REVISION)
    assert script is not None
    assert isinstance(script.down_revision, str)

    copy = release_copy(tmp_path)
    upgrade(copy, script.down_revision, script_location=location)  # the release, brought up to the revision before
    before = read_tables(copy)
    upgrade(copy, REVISION, script_location=location)
    after = read_tables(copy, order_like=before)
    assert lost_data(before, after) == []
    assert new_column_problems(copy, before, after) == []

    # The revision's own promises: what it creates, and what the release's rows read in it.
    assert set(after) - set(before) == {"t40_turn_reviews"}
    assert after["t40_turn_reviews"].rows == ()
    turns = after["turns"]
    assert set(turns.columns) - set(before["turns"].columns) == {"t40_reviewed_at", "t40_review_rounds"}
    assert {row[turns.columns.index("t40_reviewed_at")] for row in turns.rows} == {None}
    assert {row[turns.columns.index("t40_review_rounds")] for row in turns.rows} == {0}

    # The release that is still running writes the new schema with its own code (it knows no new column).
    db = open_database(copy)
    try:
        async with db.uow() as s:
            turn = await turns_store.add_turn(
                s,
                project_id="BonviHR",
                kind="instruct",
                status="queued",
                title="Yangi",
                burst_id=None,
                at=DEFAULT_START,
            )
        async with db.read() as s:
            assert await turns_store.get_turn(s, turn.id) is not None
    finally:
        await db.dispose()
    assert len(read_tables(copy)["turns"].rows) == len(before["turns"].rows) + 1
