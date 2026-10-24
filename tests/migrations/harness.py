"""Shared helpers of the migration tests (CONVENTIONS §8, §11; TASKS migration plan).

- `alembic_config` / `upgrade`: run the real `migrations/env.py` on any file, optionally on a scratch copy of the chain
  (`scratch_chain`) that carries extra, synthetic revisions.
- `release_copy`: a private copy of `fixtures/p1_release.db`, the previous-release DB. The committed file is never
  opened in place: SQLite would leave `-wal`/`-shm` files beside it.
- `read_tables` / `lost_data` / `new_column_problems`: what "no data loss" means here. Every table, column and row of
  the old file must still be there with byte-identical values (read with the stdlib driver, so a type change shows up
  as a different Python type), and a column a later revision added holds NULL or its server default on old rows.
"""

import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import Script, ScriptDirectory

ROOT: Final = Path(__file__).resolve().parents[2]
MIGRATIONS: Final = ROOT / "migrations"
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"
P1_RELEASE: Final = FIXTURES / "p1_release.db"
ALEMBIC_VERSION: Final = "alembic_version"

type Row = tuple[object, ...]


def alembic_config(db_path: Path, *, script_location: Path | None = None) -> Config:
    """The repo's `alembic.ini` aimed at `db_path` (and at a scratch chain, if given)."""
    config = Config(str(ROOT / "alembic.ini"))
    if script_location is not None:
        config.set_main_option("script_location", str(script_location))
    config.attributes["db_path"] = db_path
    config.attributes["skip_logging_config"] = True
    return config


def upgrade(db_path: Path, revision: str = "head", *, script_location: Path | None = None) -> None:
    command.upgrade(alembic_config(db_path, script_location=script_location), revision)


def script_directory(script_location: Path = MIGRATIONS) -> ScriptDirectory:
    return ScriptDirectory(str(script_location))


def revisions_after(revision: str, *, script_location: Path = MIGRATIONS) -> list[Script]:
    """The revisions above `revision` on the way to head, oldest first."""
    scripts = script_directory(script_location)
    return list(reversed(list(scripts.iterate_revisions("heads", revision))))


def head_revision(script_location: Path = MIGRATIONS) -> str:
    (head,) = script_directory(script_location).get_heads()
    return str(head)


# Synthetic revisions sit on top of the real head under an id the real chain never uses, so they keep working
# whatever the chain grows into.
SYNTHETIC: Final = "t40_synthetic"


def synthetic_revision(body: str, *, revision: str = SYNTHETIC, message: str = "synthetic (test)") -> str:
    """A revision module above the real head whose `upgrade()` is `body` (written unindented)."""
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return f'''"""{message}"""
import sqlalchemy as sa
from alembic import op

revision = "{revision}"
down_revision = "{head_revision()}"
branch_labels = None
depends_on = None


def upgrade() -> None:
{indented}


def downgrade() -> None:
    pass
'''


def scratch_chain(root: Path, revisions: Mapping[str, str]) -> Path:
    """A copy of `migrations/` under `root` with extra revision files (`file name → source`). Returns its location."""
    location = root / "migrations"
    shutil.copytree(MIGRATIONS, location, ignore=shutil.ignore_patterns("__pycache__"))
    for name, source in revisions.items():
        (location / "versions" / name).write_text(source, encoding="utf-8")
    return location


def release_at_head(root: Path) -> Path:
    """A release copy upgraded through the real chain: the starting point of a synthetic revision on top of it."""
    copy = release_copy(root)
    upgrade(copy)
    return copy


def release_copy(root: Path, name: str = "dev.db") -> Path:
    """A private copy of the P1 release DB, where the bot would keep it (`<root>/db/dev.db`)."""
    target = root / "db" / name
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(P1_RELEASE, target)
    return target


def connect(db_path: Path) -> sqlite3.Connection:
    """A plain stdlib connection (no app pragmas): the raw stored values, as the next release will find them."""
    return sqlite3.connect(db_path)


def revision_of(db_path: Path) -> tuple[str, ...]:
    with closing(connect(db_path)) as connection:
        rows = connection.execute(f"SELECT version_num FROM {ALEMBIC_VERSION} ORDER BY 1").fetchall()  # noqa: S608
    return tuple(str(row[0]) for row in rows)


def schema_of(db_path: Path) -> list[tuple[str, str, str]]:
    """Every table and index definition (type, name, SQL), as SQLite stores it."""
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT type, name, coalesce(sql, '') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return [(str(kind), str(name), str(sql)) for kind, name, sql in rows]


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class Table:
    columns: tuple[str, ...]
    key: tuple[str, ...]  # primary key columns, the row order
    defaults: Mapping[str, str | None]  # column → its DEFAULT expression, as SQLite stores it
    rows: tuple[Row, ...]

    def project(self, columns: Sequence[str]) -> tuple[Row, ...]:
        """These rows restricted to `columns` (in that order)."""
        index = [self.columns.index(column) for column in columns]
        return tuple(tuple(row[i] for i in index) for row in self.rows)


def _table(connection: sqlite3.Connection, name: str, *, order_by: Sequence[str] | None = None) -> Table:
    info = connection.execute(f"PRAGMA table_info({quoted(name)})").fetchall()
    columns = tuple(str(row[1]) for row in info)
    key = tuple(str(row[1]) for row in sorted((row for row in info if row[5]), key=lambda row: int(row[5])))
    defaults = {str(row[1]): (None if row[4] is None else str(row[4])) for row in info}
    order = order_by or key or ("rowid",)
    select = ", ".join(quoted(column) for column in columns)
    rows = connection.execute(
        f"SELECT {select} FROM {quoted(name)} ORDER BY {', '.join(quoted(column) for column in order)}"  # noqa: S608
    ).fetchall()
    return Table(columns=columns, key=key, defaults=defaults, rows=tuple(tuple(row) for row in rows))


def table_names(db_path: Path) -> list[str]:
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [str(row[0]) for row in rows if row[0] != ALEMBIC_VERSION]


def read_tables(db_path: Path, *, order_like: Mapping[str, Table] | None = None) -> dict[str, Table]:
    """Every app table with all its rows, in primary-key order (`order_like`: keep the key order of an older read, so
    rows line up even if a later revision changed a key)."""
    with closing(connect(db_path)) as connection:
        return {
            name: _table(connection, name, order_by=order_like[name].key if order_like and name in order_like else None)
            for name in table_names(db_path)
        }


def _typed(row: Row) -> tuple[tuple[type, object], ...]:
    """A row as (type, value) pairs: in Python 1250 == 1250.0 == True, so an INTEGER column that came back REAL (or a
    boolean stored as text) would otherwise compare equal."""
    return tuple((type(value), value) for value in row)


def lost_data(before: Mapping[str, Table], after: Mapping[str, Table], *, shown: int = 3) -> list[str]:
    """What `after` no longer holds of `before`: a table, a column, a row, or a changed value. Empty means no loss."""
    problems: list[str] = []
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            problems.append(f"{name}: table gone ({len(old.rows)} rows)")
            continue
        missing = [column for column in old.columns if column not in new.columns]
        if missing:
            problems.append(f"{name}: columns gone {missing}")
            continue
        if len(new.rows) != len(old.rows):
            problems.append(f"{name}: {len(old.rows)} rows before, {len(new.rows)} after")
            continue
        changed = [
            (old_row, new_row)
            for old_row, new_row in zip(old.rows, new.project(old.columns), strict=True)
            if _typed(old_row) != _typed(new_row)
        ]
        problems.extend(
            f"{name}: row changed {dict(zip(old.columns, old_row, strict=True))} → "
            f"{dict(zip(old.columns, new_row, strict=True))}"
            for old_row, new_row in changed[:shown]
        )
        if len(changed) > shown:
            problems.append(f"{name}: … {len(changed) - shown} more changed rows")
    return problems


def new_column_problems(db_path: Path, before: Mapping[str, Table], after: Mapping[str, Table]) -> list[str]:
    """A column added to an existing table must hold NULL or its server default on every row that was already there
    (expand-only: the old release never wrote it, and nothing may backfill it in the migration)."""
    problems: list[str] = []
    with closing(connect(db_path)) as connection:
        for name, old in before.items():
            new = after.get(name)
            if new is None or not old.rows:
                continue
            for column in (column for column in new.columns if column not in old.columns):
                default = new.defaults[column]
                allowed = f"{quoted(column)} IS NULL" + (
                    "" if default is None else f" OR {quoted(column)} IS ({default})"
                )
                count = connection.execute(
                    f"SELECT count(*) FROM {quoted(name)} WHERE NOT ({allowed})"  # noqa: S608
                ).fetchone()[0]
                if count:
                    expected = "NULL" if default is None else f"NULL or its default {default}"
                    problems.append(f"{name}.{column}: {count} existing rows were backfilled (expected {expected})")
    return problems


def side_files(db_path: Path) -> list[Path]:
    """The files SQLite keeps next to an open or crashed DB."""
    return [
        path for suffix in ("-wal", "-shm", "-journal") if (path := db_path.with_name(db_path.name + suffix)).exists()
    ]


def foreign_key_violations(db_path: Path) -> list[Row]:
    with closing(connect(db_path)) as connection:
        return [tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]


def integrity(db_path: Path) -> str:
    with closing(connect(db_path)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
