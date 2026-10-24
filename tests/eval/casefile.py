"""Strict reading of `tests/eval/<suite>/cases.yaml` (CONVENTIONS §11 eval pattern).

A typo in a case must fail the suite, never quietly drop an expectation: every mapping is checked for unknown and
missing keys, and every value for its type. Each helper names the case and field in its error.
"""

import tomllib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Final

import yaml

EVAL_DIR: Final = Path(__file__).resolve().parent


class CaseFileError(ValueError):
    """`cases.yaml` does not follow the format described at its top."""


def thresholds(table: str) -> Mapping[str, float]:
    """One table of `thresholds.toml` (read only; protected core). A missing table fails, it never skips."""
    data = tomllib.loads((EVAL_DIR / "thresholds.toml").read_text(encoding="utf-8"))
    rows = data.get(table)
    if not isinstance(rows, dict) or not rows:
        msg = f"thresholds.toml has no [{table}] table"
        raise CaseFileError(msg)
    values: dict[str, float] = {}
    for key, value in rows.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"thresholds.toml [{table}] {key} is not a number"
            raise CaseFileError(msg)
        values[key] = float(value)
    return values


def load_cases(path: Path) -> list[Mapping[str, object]]:
    """The `cases:` list of one suite, each entry a mapping with a unique string `id`."""
    document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    top = fields(document, str(path), required={"cases"})
    raw_cases = as_list(top["cases"], f"{path}: cases")
    cases: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            msg = f"{path}: case #{index} is not a mapping"
            raise CaseFileError(msg)
        case_id = as_str(raw.get("id"), f"{path}: case #{index} id")
        if case_id in seen:
            msg = f"{path}: case id {case_id!r} is used twice"
            raise CaseFileError(msg)
        seen.add(case_id)
        cases.append({str(key): value for key, value in raw.items()})
    return cases


def fields(
    raw: object, where: str, *, required: Collection[str] = (), optional: Collection[str] = ()
) -> Mapping[str, object]:
    """`raw` as a mapping holding every `required` key and nothing outside `required | optional`."""
    if not isinstance(raw, dict):
        msg = f"{where}: expected a mapping"
        raise CaseFileError(msg)
    keys = {str(key) for key in raw}
    unknown = keys - set(required) - set(optional)
    missing = set(required) - keys
    if unknown or missing:
        msg = f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
        raise CaseFileError(msg)
    return {str(key): value for key, value in raw.items()}


def as_list(raw: object, where: str) -> list[object]:
    if not isinstance(raw, list):
        msg = f"{where}: expected a list"
        raise CaseFileError(msg)
    return list(raw)


def as_str(raw: object, where: str) -> str:
    if not isinstance(raw, str):
        msg = f"{where}: expected a string, got {type(raw).__name__}"
        raise CaseFileError(msg)
    return raw


def as_optional_str(raw: object, where: str) -> str | None:
    return None if raw is None else as_str(raw, where)


def as_float(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        msg = f"{where}: expected a number, got {type(raw).__name__}"
        raise CaseFileError(msg)
    return float(raw)


def as_int(raw: object, where: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = f"{where}: expected an integer, got {type(raw).__name__}"
        raise CaseFileError(msg)
    return raw


def as_bool(raw: object, where: str) -> bool:
    if not isinstance(raw, bool):
        msg = f"{where}: expected true or false, got {type(raw).__name__}"
        raise CaseFileError(msg)
    return raw
