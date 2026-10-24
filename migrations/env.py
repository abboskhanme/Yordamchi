"""Alembic environment for `dev.db` and `personal.db` (SPEC §2.3; CONVENTIONS §8).

The target file comes from `config.attributes["db_path"]` (tests), else `-x db=dev|personal|<path>`.
Models are discovered by name (`*/models.py`), batch mode is on (SQLite cannot ALTER most columns), and custom column
types render as their plain `impl`, so a revision never imports app code.
"""

import logging.config
import sqlite3
from pathlib import Path
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import create_engine, event
from sqlalchemy.pool import ConnectionPoolEntry, NullPool
from sqlalchemy.types import TypeDecorator

from yordamchi.core.config import load_paths
from yordamchi.core.paths import BotName
from yordamchi.persistence.base import import_all_models
from yordamchi.persistence.engine import WRITE_PRAGMAS, apply_pragmas, ensure_db_file, sqlite_url

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = import_all_models()


def _db_path() -> Path:
    configured = config.attributes.get("db_path")
    if configured is not None:
        return Path(str(configured))
    name = context.get_x_argument(as_dictionary=True).get("db", "dev")
    if name in {bot.value for bot in BotName}:
        return load_paths().db_file(BotName(name))
    return Path(name).expanduser()


def _render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    del autogen_context
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl!r}"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=sqlite_url(_db_path(), readonly=False, driver=""),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    path = _db_path()
    ensure_db_file(path)
    engine = create_engine(sqlite_url(path, readonly=False, driver=""), poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: sqlite3.Connection, record: ConnectionPoolEntry) -> None:
        del record
        apply_pragmas(dbapi_connection, WRITE_PRAGMAS)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
