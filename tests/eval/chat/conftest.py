"""The chat eval's report in the terminal summary (CONVENTIONS §11). `--record-llm` lives in `tests/conftest.py`:

uv run pytest -m live --record-llm tests/eval/chat
"""

import pytest

from tests.eval.chat.report import REPORT


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    lines = config.stash.get(REPORT, [])
    if lines:
        terminalreporter.section("chat eval (dev group answers)")
        for line in lines:
            terminalreporter.write_line(line)
