#!/bin/sh
# The local gate before a commit: nothing here leaves the Mac.
set -eu
uv run ruff check .
uv run mypy src
uv run pytest -q
