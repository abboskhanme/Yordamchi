"""Root plugin: command-line options only. Fixtures live in `tests/conftest.py`.

`pytest_addoption` must sit in a conftest pytest loads before it parses the command line; this one is loaded for any
test path, so `pytest tests/unit/test_golden_messages.py --update-golden` works as well as a full run.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/* from the real rendering path; review `git diff tests/golden` before committing",
    )
