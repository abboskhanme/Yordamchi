# `make check` = every gate (CONVENTIONS header; SPEC §8, §12). The supervisor runs the same target before it
# switches to a new release, so a red stage here is a refused update there.
UV ?= uv
RUN := $(UV) run --locked

.PHONY: check lint typecheck imports test supervisor-test migrations fix

check: lint typecheck imports test supervisor-test migrations
	@echo "make check: all stages green"

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

typecheck:
	$(RUN) mypy

imports:
	$(RUN) lint-imports

# Includes the architecture rules (tests/architecture, A1–A9) with the network closed (pytest-socket).
test:
	$(RUN) pytest

# The supervisor's own interpreter and venv, stdlib unittest only (no pytest dependency).
supervisor-test:
	cd supervisor && $(RUN) python -m unittest discover -s tests -t . -v

# `alembic upgrade head` on fresh dev.db and personal.db, then `alembic check` (no model drift, one head).
migrations:
	@scratch=$$(mktemp -d) && trap 'rm -rf "$$scratch"' EXIT && \
	for db in dev personal; do \
		$(RUN) alembic -x db="$$scratch/$$db.db" upgrade head && \
		$(RUN) alembic -x db="$$scratch/$$db.db" check || exit 1; \
	done

fix:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .
