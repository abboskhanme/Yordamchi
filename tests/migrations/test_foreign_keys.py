"""Foreign keys into tables that retention prunes (SPEC §4.1–4.2; decisions in `inventory.py` with their sources).

The ON DELETE action is checked in three places that must agree: the generated `0001` file, the models, and a DB that
Alembic migrated (what SQLite will enforce). Then each decided action is exercised on the release DB's real rows.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from tests.migrations import inventory, revision_rules
from tests.migrations.harness import MIGRATIONS, connect, quoted, release_copy, table_names, upgrade
from yordamchi.persistence.base import import_all_models

type FkMap = dict[tuple[str, str], tuple[str, str | None]]


def _watched(keys: set[revision_rules.ForeignKey]) -> FkMap:
    found: FkMap = {}
    for key in keys:
        if key.referred in inventory.WATCHED_PARENTS:
            assert len(key.columns) == 1, key
            found[(key.table, key.columns[0])] = (key.referred, key.ondelete)
    return found


def _from_revision() -> set[revision_rules.ForeignKey]:
    script = ScriptDirectory(str(MIGRATIONS)).get_revision("0001")
    assert script is not None
    return revision_rules.created_foreign_keys(Path(script.path).read_text(encoding="utf-8"))


def _from_models() -> set[revision_rules.ForeignKey]:
    return {
        revision_rules.ForeignKey(
            table.name,
            tuple(constraint.column_keys),
            constraint.referred_table.name,
            constraint.ondelete.upper() if constraint.ondelete else None,
        )
        for table in import_all_models().tables.values()
        for constraint in table.foreign_key_constraints
    }


def _from_database(db_path: Path) -> set[revision_rules.ForeignKey]:
    keys: set[revision_rules.ForeignKey] = set()
    with closing(connect(db_path)) as connection:
        for table in table_names(db_path):
            for row in connection.execute(f"PRAGMA foreign_key_list({quoted(table)})").fetchall():
                ondelete = str(row[6]).upper()
                keys.add(
                    revision_rules.ForeignKey(
                        table, (str(row[3]),), str(row[2]), None if ondelete == "NO ACTION" else ondelete
                    )
                )
    return keys


@pytest.fixture(scope="module")
def migrated_keys(tmp_path_factory: pytest.TempPathFactory) -> set[revision_rules.ForeignKey]:
    db_path = tmp_path_factory.mktemp("fk") / "dev.db"
    upgrade(db_path)
    return _from_database(db_path)


def test_foreign_keys_to_bursts_turns_and_questions_are_as_decided_in_0001() -> None:
    assert _watched(_from_revision()) == inventory.FOREIGN_KEYS


def test_foreign_keys_to_bursts_turns_and_questions_are_as_decided_in_the_models() -> None:
    assert _watched(_from_models()) == inventory.FOREIGN_KEYS


def test_foreign_keys_to_bursts_turns_and_questions_are_as_decided_in_a_migrated_db(
    migrated_keys: set[revision_rules.ForeignKey],
) -> None:
    assert _watched(migrated_keys) == inventory.FOREIGN_KEYS


def test_the_revision_the_models_and_the_db_agree_on_every_foreign_key(
    migrated_keys: set[revision_rules.ForeignKey],
) -> None:
    assert _from_revision() == _from_models() == migrated_keys


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "PENDING_WIRING From T15/T24/T29: outbox_messages.turn_id, message_links.turn_id and message_links.question_id "
        "are decided as FKs ON DELETE SET NULL but are plain integers. Add them before T43 freezes 0001 (later it is a "
        "table rebuild), then move them to inventory.FOREIGN_KEYS and drop this marker."
    ),
)
def test_the_decided_outbox_foreign_keys_are_in_the_schema() -> None:
    watched = _watched(_from_models())
    assert {key: watched.get(key) for key in inventory.PENDING_FOREIGN_KEYS} == inventory.PENDING_FOREIGN_KEYS


def test_every_foreign_key_into_a_pruned_table_has_an_on_delete_action() -> None:
    """With `foreign_keys=ON`, deleting a parent that a NO ACTION child still points at raises: that retention run
    would fail every night. Either the key says what happens to the child, or retention deletes the child first."""
    pruned = {table for table, rule in inventory.RETENTION.items() if rule.deletes_rows}
    bare = {
        (key.table, key.columns[0])
        for key in _from_models()
        if key.referred in pruned and key.ondelete not in {"SET NULL", "CASCADE"}
    }
    assert bare == set(inventory.GUARDED_BY_RETENTION)


@pytest.mark.parametrize(
    ("child", "column", "parent", "action"),
    [(child, column, parent, action) for (child, column), (parent, action) in inventory.FOREIGN_KEYS.items()],
    ids=[f"{child}.{column}" for child, column in inventory.FOREIGN_KEYS],
)
def test_deleting_a_parent_row_does_what_was_decided(
    tmp_path: Path, child: str, column: str, parent: str, action: str
) -> None:
    db_path = release_copy(tmp_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            f"SELECT rowid, {quoted(column)} FROM {quoted(child)} WHERE {quoted(column)} IS NOT NULL LIMIT 1"  # noqa: S608
        ).fetchone()
        assert row is not None, f"the release DB has no {child} row pointing at {parent}"
        child_rowid, parent_id = row
        with connection:
            connection.execute(f"DELETE FROM {quoted(parent)} WHERE id = ?", (parent_id,))  # noqa: S608
        after = connection.execute(
            f"SELECT {quoted(column)} FROM {quoted(child)} WHERE rowid = ?",  # noqa: S608
            (child_rowid,),
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert after == ((None,) if action == "SET NULL" else None)
    assert violations == []
