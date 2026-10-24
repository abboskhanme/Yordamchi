"""Eval-wide fixtures (CONVENTIONS §11).

`--record-llm` is declared once, in `tests/conftest.py`. With it, a suite's `live` test writes the real model's
answers to `tests/fixtures/llm/<suite>/` (see `tests/eval/recorder.py`); without it, the live test only checks them:
`pytest -m live --record-llm tests/eval/settings tests/eval/injection`.
"""

import pytest


@pytest.fixture
def record_llm(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--record-llm"))
