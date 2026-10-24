"""Where the routing eval leaves its per-bucket report for the terminal summary (conftest.py)."""

from typing import Final

import pytest

REPORT: Final = pytest.StashKey[list[str]]()
