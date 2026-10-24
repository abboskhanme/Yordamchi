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
# Each checkout gets its own pytest temp root under the system temp dir: parallel runs (agent worktrees) sharing
# one root clean up each other's dirs and fail on the cleanup warnings. It stays under $TMPDIR on purpose: the
# gate eval treats /private/tmp as a temp root, so the default location must not move there.
PYTEST_TEMPROOT ?= $(shell python3 -c 'import hashlib,os,tempfile;print(os.path.join(tempfile.gettempdir(),"yordamchi-"+hashlib.sha1(os.getcwd().encode()).hexdigest()[:10]))')
test:
	mkdir -p $(PYTEST_TEMPROOT) && PYTEST_DEBUG_TEMPROOT=$(PYTEST_TEMPROOT) $(RUN) pytest

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
