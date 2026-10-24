"""Golden files (CONVENTIONS §11): what the owner reads, compared byte for byte with `tests/golden/`.

A golden test renders its text through the same function the service calls, never a hand-built string, and passes
it to `check_golden`. Without a flag a missing or different file fails with a unified diff. After a reviewed wording
or format change, `pytest --update-golden` rewrites the files (the root `conftest.py` registers the option); the diff
in `git diff tests/golden` is what gets reviewed.

A file of several cases holds one `=== <case> ===` header per case, so a reviewer sees every variant of a message
family on one page.
"""

import difflib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

GOLDEN: Final = Path(__file__).resolve().parent / "golden"
UPDATE_OPTION: Final = "update_golden"

type Case = tuple[str, str]  # (title, the text exactly as the owner gets it)


def cases_text(cases: Sequence[Case]) -> str:
    """The file body: every case under its header, a blank line between cases, one trailing newline."""
    titles = [title for title, _ in cases]
    if len(set(titles)) != len(titles):
        msg = f"duplicate golden case titles: {titles}"
        raise ValueError(msg)
    return "\n\n".join(f"=== {title} ===\n{text}" for title, text in cases) + "\n"


def updating(config: pytest.Config) -> bool:
    return bool(config.getoption(UPDATE_OPTION, default=False))


def check_golden(config: pytest.Config, name: str, actual: str) -> None:
    """`actual` must equal `tests/golden/<name>`; with `--update-golden` the file is rewritten first."""
    path = GOLDEN / name
    if updating(config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    if not path.is_file():
        pytest.fail(f"tests/golden/{name} is missing: run `pytest --update-golden` and review the new file")
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"tests/golden/{name}",
                tofile="rendered now",
            )
        )
        pytest.fail(f"owner-visible text changed; if intended, run `pytest --update-golden` and review:\n{diff}")


def check_cases(config: pytest.Config, name: str, cases: Sequence[Case]) -> None:
    check_golden(config, name, cases_text(cases))
