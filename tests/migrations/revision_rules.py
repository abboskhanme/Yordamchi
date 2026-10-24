"""Static rules for revision files, read with `ast` (CONVENTIONS §8; TASKS migration plan).

- `create_only_violations`: `0001` is generated against an empty DB, so its `upgrade()` may only create tables and
  indexes. A drop or an alter there is a spurious autogenerate line.
- `expand_only_violations`: every later revision may only add (R-33): new tables, nullable columns (or NOT NULL with a
  literal server default) and indexes. A rename or drop comes one release after the code stopped using the thing.

Anything else in `upgrade()` that calls `op` or a batch operation is refused: an allowlist, so a new kind of operation
fails closed. Two SQLite facts drive the details. First, SQLite cannot alter a column or a constraint, so Alembic's
batch mode copies the table into a new one and drops the old one, and `migrations/env.py` migrates with
`foreign_keys=ON`. The implicit DELETE of that DROP TABLE fires ON DELETE CASCADE and SET NULL in every child table
(test_expand_only proves it on the release DB). Second, inside a batch an `add_column` whose server default is a SQL
expression (`sa.text(...)`, `sa.func.now()`) also takes that path; a literal string default does not.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

# `0001`: generated against an empty DB.
CREATE_ONLY_OP: Final = frozenset({"create_table", "create_index", "batch_alter_table", "f"})
CREATE_ONLY_BATCH: Final = frozenset({"create_index", "f"})
# `000N`: expand only (CONVENTIONS §8).
EXPAND_OP: Final = frozenset({"create_table", "create_index", "add_column", "batch_alter_table", "f"})
EXPAND_BATCH: Final = frozenset({"add_column", "create_index", "f"})
CONTRACTING: Final = frozenset({"drop_table", "drop_column", "rename_table"})
BATCH_KWARGS: Final = frozenset({"schema"})  # `recreate`, `copy_from`, … would force or shape a table rebuild


@dataclass(frozen=True, slots=True)
class OpCall:
    method: str
    call: ast.Call
    batch: bool  # called on a `batch_alter_table` operations object


def upgrade_function(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    msg = "the revision has no upgrade()"
    raise ValueError(msg)


def _batch_names(upgrade: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(upgrade):
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "batch_alter_table"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    names.add(item.optional_vars.id)
    return names


def op_calls(upgrade: ast.FunctionDef) -> Iterator[OpCall]:
    """Every call made on `op` or on a batch operations object inside `upgrade()`."""
    batch = _batch_names(upgrade)
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id == "op":
            yield OpCall(node.func.attr, node, batch=False)
        elif isinstance(receiver, ast.Name) and receiver.id in batch:
            yield OpCall(node.func.attr, node, batch=True)


def _called_names(upgrade: ast.FunctionDef) -> Iterator[tuple[str, ast.Call]]:
    """The name of every call in `upgrade()` (`x.drop_table(...)` and a bare `drop_table(...)` alike)."""
    for node in ast.walk(upgrade):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                yield node.func.attr, node
            elif isinstance(node.func, ast.Name):
                yield node.func.id, node


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _batch_kwargs_problems(found: OpCall) -> list[str]:
    if found.method != "batch_alter_table":
        return []
    extra = sorted(str(keyword.arg) for keyword in found.call.keywords if keyword.arg not in BATCH_KWARGS)
    return [f"batch_alter_table({', '.join(extra)}=…) shapes or forces a table rebuild"] if extra else []


def _column(found: OpCall) -> ast.Call | None:
    for argument in found.call.args:
        if isinstance(argument, ast.Call):
            func = argument.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name == "Column":
                return argument
    return None


def _add_column_problems(found: OpCall) -> list[str]:
    column = _column(found)
    if column is None:
        return ["add_column(…) without a literal sa.Column(…): the guard cannot see whether it is nullable"]
    label = ast.literal_eval(column.args[0]) if column.args and isinstance(column.args[0], ast.Constant) else "?"
    problems: list[str] = []
    nullable, primary_key, default = (_keyword(column, name) for name in ("nullable", "primary_key", "server_default"))
    for name, value in (("nullable", nullable), ("primary_key", primary_key)):
        if value is not None and not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
            problems.append(f"add_column {label!r}: {name}= is not a literal True/False")
    not_null = (isinstance(nullable, ast.Constant) and nullable.value is False) or (
        isinstance(primary_key, ast.Constant) and primary_key.value is True
    )
    has_default = default is not None and not (isinstance(default, ast.Constant) and default.value is None)
    if not_null and not has_default:
        problems.append(f"add_column {label!r}: NOT NULL without a server default fails on existing rows")
    if has_default and not (isinstance(default, ast.Constant) and isinstance(default.value, str)):
        problems.append(
            f"add_column {label!r}: a SQL-expression server default rebuilds the table on SQLite; use a literal string"
        )
    return problems


def create_only_violations(source: str) -> list[str]:
    """What in `upgrade()` is not a `create_table` / `create_index` (with the batch wrapper autogenerate emits)."""
    upgrade = upgrade_function(ast.parse(source))
    problems: list[str] = []
    for found in op_calls(upgrade):
        allowed = CREATE_ONLY_BATCH if found.batch else CREATE_ONLY_OP
        if found.method not in allowed:
            problems.append(f"line {found.call.lineno}: {found.method}() is not create_table/create_index")
        problems.extend(f"line {found.call.lineno}: {problem}" for problem in _batch_kwargs_problems(found))
    return problems


def expand_only_violations(source: str) -> list[str]:
    """What in `upgrade()` is not an expand step: a drop, a rename, a NOT NULL column with no default, a table
    rebuild, raw SQL, or any operation outside the allowlist."""
    upgrade = upgrade_function(ast.parse(source))
    problems: list[str] = []
    for name, call in _called_names(upgrade):
        if name in CONTRACTING:
            problems.append(
                f"line {call.lineno}: {name}() drops or renames; that ships one release after the code stopped using it"
            )
    for found in op_calls(upgrade):
        where = f"line {found.call.lineno}"
        if found.method in CONTRACTING:
            continue  # reported above
        allowed = EXPAND_BATCH if found.batch else EXPAND_OP
        if found.method not in allowed:
            problems.append(f"{where}: {found.method}() is not an expand step (new table, column or index)")
        problems.extend(f"{where}: {problem}" for problem in _batch_kwargs_problems(found))
        if found.method == "add_column":
            problems.extend(f"{where}: {problem}" for problem in _add_column_problems(found))
    return problems


@dataclass(frozen=True, slots=True)
class ForeignKey:
    table: str
    columns: tuple[str, ...]
    referred: str  # the parent table
    ondelete: str | None  # "CASCADE" | "SET NULL" | None (NO ACTION)


def _strings(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.List | ast.Tuple):
        return tuple(str(ast.literal_eval(element)) for element in node.elts)
    return (str(ast.literal_eval(node)),)


def _ondelete(call: ast.Call) -> str | None:
    value = _keyword(call, "ondelete")
    return None if value is None else str(ast.literal_eval(value)).upper()


def created_foreign_keys(source: str) -> set[ForeignKey]:
    """Every foreign key that `upgrade()` creates with `op.create_table` (table-level or inline on a column)."""
    upgrade = upgrade_function(ast.parse(source))
    keys: set[ForeignKey] = set()
    for found in op_calls(upgrade):
        if found.method != "create_table" or not found.call.args:
            continue
        table = str(ast.literal_eval(found.call.args[0]))
        for argument in found.call.args[1:]:
            if not isinstance(argument, ast.Call):
                continue
            func = argument.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name == "ForeignKeyConstraint":
                columns, targets = _strings(argument.args[0]), _strings(argument.args[1])
                keys.add(ForeignKey(table, columns, targets[0].partition(".")[0], _ondelete(argument)))
            elif name == "Column":
                column = str(ast.literal_eval(argument.args[0]))
                for inner in argument.args[1:]:
                    if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "ForeignKey":
                        target = str(ast.literal_eval(inner.args[0]))
                        keys.add(ForeignKey(table, (column,), target.partition(".")[0], _ondelete(inner)))
    return keys
