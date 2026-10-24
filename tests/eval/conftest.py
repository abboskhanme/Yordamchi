"""Eval-wide options (CONVENTIONS §11).

`--record-llm`: a suite's `live` test writes the real model's answers to `tests/fixtures/llm/<suite>/` (see
`tests/eval/recorder.py`); without it the live test only checks them. The option is declared here, the one home for
every eval suite, so pytest knows it once this file is loaded at startup: name an eval path on the command line,
`pytest -m live --record-llm tests/eval/settings tests/eval/injection`.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record-llm",
        action="store_true",
        default=False,
        help="live eval tests write the model answers they get to tests/fixtures/llm/<suite>/",
    )


@pytest.fixture
def record_llm(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--record-llm"))
