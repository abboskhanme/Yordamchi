"""The declarative base every feature's `models.py` uses (CONVENTIONS §8, SPEC §4.1).

- `MetaData(naming_convention=…)`: SQLite batch migrations need named constraints.
- `Mapped[datetime]` maps to `UTCDateTime` by default, so a `*_at` column is UTC-aware without extra code.
- Tables have `id INTEGER PRIMARY KEY` unless a key is given (`Integer`, not `BigInteger`: SQLite only aliases the
  rowid, and so only autoincrements, for `INTEGER`). Telegram ids are `BigInteger`; JSON is `JSONText`.
- Cross-feature foreign keys are strings, with no `relationship()` (A2).
"""

import importlib
import pkgutil
from datetime import datetime
from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from yordamchi.persistence.types import UTCDateTime

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime()}  # noqa: RUF012  # SQLAlchemy reads this class attribute


def import_all_models(package: str = "yordamchi") -> MetaData:
    """Import every `<package>.**.models` module so `Base.metadata` holds every table.

    Used by `migrations/env.py` (autogenerate, `alembic check`) and the test DB fixture (`create_all`). Discovery is
    by module name, so persistence never imports a feature statically (C1).
    """
    root = importlib.import_module(package)
    for module in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        if module.name.rsplit(".", 1)[-1] == "models":
            importlib.import_module(module.name)
    return Base.metadata
