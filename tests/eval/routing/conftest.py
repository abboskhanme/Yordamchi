"""The routing eval's report in the terminal summary (CONVENTIONS §11). `--record-llm` lives in `tests/conftest.py`:

uv run pytest -m live --record-llm
"""

import pytest

from tests.eval.routing.report import REPORT


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    lines = config.stash.get(REPORT, [])
    if lines:
        terminalreporter.section("routing eval (UC-10)")
        for line in lines:
            terminalreporter.write_line(line)
