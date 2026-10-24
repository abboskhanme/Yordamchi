"""The routing eval's command-line option and its report (CONVENTIONS §11).

`--record-llm` makes the live pass write what the model answered into `tests/fixtures/llm/routing/`. pytest reads
options only from the conftest files of the paths it is given, so name the suite:

    uv run pytest -m live --record-llm tests/eval/routing
"""

import pytest

from tests.eval.routing.report import REPORT


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record-llm",
        action="store_true",
        default=False,
        help="live routing eval: ask the real routing model once per case and write tests/fixtures/llm/routing/",
    )


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    lines = config.stash.get(REPORT, [])
    if lines:
        terminalreporter.section("routing eval (UC-10)")
        for line in lines:
            terminalreporter.write_line(line)
