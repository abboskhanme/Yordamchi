"""Schema version checks (SPEC §2.5 step 5): a bot refuses to start unless its DB is at the Alembic head.

Bots never migrate; the installer and the supervisor run `alembic upgrade head` per DB file (SPEC §3.2.12 step 4).
"""

from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from yordamchi.persistence.uow import Database


def head_revisions(migrations_dir: Path) -> tuple[str, ...]:
    """The head revision ids of the chain in `migrations_dir` (exactly one in a healthy repo)."""
    return tuple(ScriptDirectory(str(migrations_dir)).get_heads())


def _current(connection: Connection) -> tuple[str, ...]:
    return tuple(MigrationContext.configure(connection).get_current_heads())


async def current_revisions(db: Database) -> tuple[str, ...]:
    async with db.engine.connect() as connection:
        return await connection.run_sync(_current)


async def schema_is_current(db: Database, migrations_dir: Path) -> bool:
    return set(await current_revisions(db)) == set(head_revisions(migrations_dir))
