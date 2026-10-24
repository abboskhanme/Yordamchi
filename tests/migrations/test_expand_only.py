"""The expand-only guard for every revision after `0001` (CONVENTIONS §8, R-33; TASKS migration plan, P2+).

An update can roll back to the previous release by restoring the pre-update DB copy, and during probation the old code
may still run against the new schema. So a revision only adds: a new table, a nullable column (or NOT NULL with a
literal server default), an index. A rename or drop ships one release after the code stopped using it.

The guard reads `upgrade()` of each later revision (`revision_rules.expand_only_violations`); the tests below prove it
on synthetic revisions, since the chain has none yet. The last tests prove, on the release DB, that a table rebuild
keeps every child row now that `migrations/env.py` migrates with `foreign_keys=OFF`.
"""

from pathlib import Path
from typing import Final

import pytest

from tests.migrations import inventory
from tests.migrations.harness import (
    SYNTHETIC,
    lost_data,
    read_tables,
    release_at_head,
    revisions_after,
    scratch_chain,
    synthetic_revision,
    upgrade,
)
from tests.migrations.revision_rules import expand_only_violations

# A contraction (drop or rename) ships one release after the code stopped using what it removes. List its revision
# here with that release, e.g. {"0007": "turns.old_x unused since release 2026.12.01-abc1234"}. Safe only while
# env.py migrates with foreign_keys=OFF: SQLite drops or renames a column by rebuilding the table, and with foreign
# keys on, the rebuild empties or nulls every child table (test_a_table_rebuild_keeps_every_child_row below).
CONTRACTIONS: Final[dict[str, str]] = {}


def _source(body: str) -> str:
    return synthetic_revision(body)


def _rebuild_loss(tmp_path: Path, table: str) -> list[str]:
    """Upgrade a release copy with a revision that rebuilds `table` (any batch op but add_column/create_index does),
    through the real env.py. Returns what was lost."""
    body = (
        f'with op.batch_alter_table("{table}", schema=None) as batch_op:\n'
        '    batch_op.create_unique_constraint("uq_t40_rebuild", ["id"])'
    )
    location = scratch_chain(tmp_path, {f"{SYNTHETIC}.py": synthetic_revision(body)})
    copy = release_at_head(tmp_path)
    before = read_tables(copy)
    upgrade(copy, SYNTHETIC, script_location=location)
    return lost_data(before, read_tables(copy, order_like=before))


def test_every_revision_after_0001_only_expands(tmp_path: Path) -> None:
    problems: dict[str, list[str]] = {}
    for script in revisions_after("0001"):
        found = expand_only_violations(Path(script.path).read_text(encoding="utf-8"))
        if script.revision in CONTRACTIONS:
            found = [problem for problem in found if "drops or renames" not in problem]
        if found:
            problems[f"{script.revision} ({Path(script.path).name})"] = found
    assert problems == {}
    if CONTRACTIONS:
        assert _rebuild_loss(tmp_path, "turns") == [], "a contraction needs env.py to migrate with foreign_keys=OFF"


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        pytest.param('op.drop_table("turns")', "drop_table() drops or renames", id="drop_table"),
        pytest.param('op.drop_column("turns", "natija")', "drop_column() drops or renames", id="drop_column"),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n    batch_op.drop_column("natija")',
            "drop_column() drops or renames",
            id="batch_drop_column",
        ),
        pytest.param('op.rename_table("turns", "jobs")', "rename_table() drops or renames", id="rename_table"),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as b:\n'
            '    b.alter_column("title", new_column_name="name")',
            "alter_column() is not an expand step",
            id="rename_column",
        ),
        pytest.param(
            'op.add_column("turns", sa.Column("attempts", sa.Integer(), nullable=False))',
            "NOT NULL without a server default",
            id="not_null",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.add_column(sa.Column("attempts", sa.Integer(), server_default=None, nullable=False))',
            "NOT NULL without a server default",
            id="batch_not_null_default_none",
        ),
        pytest.param(
            'op.add_column("turns", sa.Column("code", sa.Integer(), primary_key=True))',
            "NOT NULL without a server default",
            id="primary_key",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.add_column(sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False))',
            "a SQL-expression server default rebuilds the table",
            id="expression_default",
        ),
        pytest.param(
            'with op.batch_alter_table("outbox_messages", schema=None) as batch_op:\n'
            '    batch_op.create_foreign_key("fk_x", "turns", ["turn_id"], ["id"], ondelete="SET NULL")',
            "create_foreign_key() is not an expand step",
            id="add_foreign_key",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.create_unique_constraint("uq_x", ["title"])',
            "create_unique_constraint() is not an expand step",
            id="add_unique",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.alter_column("title", existing_type=sa.String(120), type_=sa.String(50))',
            "alter_column() is not an expand step",
            id="narrow_type",
        ),
        pytest.param('op.execute("UPDATE turns SET steps = 0")', "execute() is not an expand step", id="execute"),
        pytest.param('op.bulk_insert(sa.table("t"), [{"a": 1}])', "bulk_insert() is not an expand step", id="bulk"),
        pytest.param(
            'op.get_bind().exec_driver_sql("DROP TABLE turns")', "get_bind() is not an expand step", id="raw_sql"
        ),
        pytest.param(
            'with op.batch_alter_table("turns", recreate="always") as batch_op:\n'
            '    batch_op.create_index("ix", ["kind"])',
            "batch_alter_table(recreate=…) shapes or forces a table rebuild",
            id="recreate_always",
        ),
        pytest.param(
            'column = sa.Column("x", sa.Integer(), nullable=False)\nop.add_column("turns", column)',
            "without a literal sa.Column",
            id="column_from_a_variable",
        ),
        pytest.param(
            'op.add_column("turns", sa.Column("x", sa.Integer(), nullable=NOT_NULL))',
            "nullable= is not a literal",
            id="nullable_from_a_variable",
        ),
        pytest.param(
            'from alembic.op import drop_table\ndrop_table("turns")', "drop_table() drops or renames", id="bare_name"
        ),
    ],
)
def test_the_guard_refuses(body: str, problem: str) -> None:
    found = expand_only_violations(_source(body))
    assert any(problem in line for line in found), found


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'op.create_table("project_drafts", sa.Column("id", sa.Integer(), nullable=False),'
            ' sa.Column("status", sa.String(16), nullable=False), sa.PrimaryKeyConstraint("id"))',
            id="new_table",
        ),
        pytest.param('op.add_column("turns", sa.Column("reviewed_at", sa.DateTime()))', id="nullable_column"),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(), nullable=True))',
            id="batch_nullable_column",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.add_column(sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))',
            id="not_null_with_literal_default",
        ),
        pytest.param(
            'with op.batch_alter_table("turns", schema=None) as batch_op:\n'
            '    batch_op.create_index(batch_op.f("ix_turns_kind"), ["kind"], unique=False)',
            id="batch_index",
        ),
        pytest.param('op.create_index("ix_turns_kind", "turns", ["kind"])', id="index"),
    ],
)
def test_the_guard_accepts(body: str) -> None:
    assert expand_only_violations(_source(body)) == []


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("turns", id="cascade_children_kept"),  # with foreign keys on: deliveries, questions emptied
        pytest.param("bursts", id="set_null_children_kept"),  # with foreign keys on: every burst_id pointer nulled
    ],
)
def test_a_table_rebuild_keeps_every_child_row(tmp_path: Path, table: str) -> None:
    children = {child for (child, _), (parent, _) in inventory.FOREIGN_KEYS.items() if parent == table}
    problems = _rebuild_loss(tmp_path, table)
    other = [problem for problem in problems if problem.split(":")[0] not in children]
    if other:  # not the cascade this guards against: report it as what it is
        pytest.fail(f"rebuilding {table} lost data outside its child tables: {other}")
    assert problems == [], f"rebuilding {table} emptied or nulled its child tables {sorted(children)}"
