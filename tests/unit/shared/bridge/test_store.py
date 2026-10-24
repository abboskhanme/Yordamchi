"""`shared/bridge/store.py` (T70, UC-36): its columns match the dev models, and it has no write path at all."""

import ast
from pathlib import Path

from yordamchi.persistence.base import import_all_models
from yordamchi.shared.bridge import domain, store

STORE = Path(store.__file__)
SERVICE = STORE.with_name("service.py")
WRITES = {"insert", "update", "delete", "merge", "add", "add_all", "flush", "commit", "execute_many", "uow"}


def test_every_bridge_column_exists_in_the_real_model_with_its_type() -> None:
    """The bridge declares the dev tables' columns itself (A2); a dev-side rename or type change fails here."""
    metadata = import_all_models()
    for table in store.TABLES:
        real = metadata.tables[table.name]
        for column in table.columns:
            assert column.name in real.columns, f"{table.name}.{column.name} is not in the dev model"
            assert type(column.type) is type(real.columns[column.name].type), f"{table.name}.{column.name} type"


def test_the_bridge_reads_only_tables_spec_2_3_allows() -> None:
    assert {table.name for table in store.TABLES} <= domain.BRIDGE_TABLES


def test_the_private_metadata_is_not_the_migrations_metadata() -> None:
    metadata = import_all_models()
    assert all(table.metadata is not metadata for table in store.TABLES)


def _called_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_the_bridge_code_has_no_write_call() -> None:
    """No INSERT/UPDATE/DELETE builder, no `s.add`, no flush, no unit of work: reads only (`db.read()`)."""
    for path in (STORE, SERVICE):
        assert _called_names(path).isdisjoint(WRITES), f"{path.name}: {_called_names(path) & WRITES}"


def test_the_bridge_imports_nothing_from_dev_adapters_or_the_executor() -> None:
    """UC-36 by import: `shared` cannot reach `dev` (C1), and the bridge names no executor or adapter."""
    forbidden = ("yordamchi.dev", "yordamchi.adapters", "yordamchi.bots", "yordamchi.ports.agent")
    for path in sorted(STORE.parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), f"{path.name} imports {node.module}"
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith(forbidden) for alias in node.names)
