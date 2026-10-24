"""Shared fixtures (CONVENTIONS §11). The network is closed (pytest-socket); `live` tests re-open it."""

from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from pytest_socket import enable_socket

from tests.factories import Fakes, make_dev_deps, make_paths, make_personal_deps, open_test_db
from yordamchi.core import redact
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record-llm",
        action="store_true",
        default=False,
        help="with -m live: ask the real model and rewrite the replay fixtures in tests/fixtures/llm/<suite>/",
    )


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("live") is not None:
        enable_socket()


@pytest.fixture(autouse=True)
def _reset_known_secrets() -> Iterator[None]:
    """The masking set is process-wide; no test may leak secrets into the next one."""
    redact.set_known_secrets([])
    yield
    redact.set_known_secrets([])


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return make_paths(tmp_path)


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
async def dev_db(paths: Paths) -> AsyncIterator[Database]:
    db = await open_test_db(paths.db_file(BotName.DEV))
    yield db
    await db.dispose()


@pytest.fixture
async def personal_db(paths: Paths) -> AsyncIterator[Database]:
    db = await open_test_db(paths.db_file(BotName.PERSONAL))
    yield db
    await db.dispose()


@pytest.fixture
async def dev_db_readonly(paths: Paths, dev_db: Database) -> AsyncIterator[Database]:
    """The personal bot's view of dev.db (`mode=ro`, `query_only`)."""
    del dev_db  # created first, so the file and schema exist
    db = open_database(paths.db_file(BotName.DEV), readonly=True)
    yield db
    await db.dispose()


@pytest.fixture
def dev_deps(dev_db: Database, paths: Paths, fakes: Fakes) -> DevDeps:
    return make_dev_deps(db=dev_db, paths=paths, fakes=fakes)


@pytest.fixture
def personal_deps(personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes) -> PersonalDeps:
    return make_personal_deps(db=personal_db, paths=paths, fakes=fakes, peer_db=dev_db_readonly)


@pytest.fixture(autouse=True)
def _never_the_real_keychain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside `live` tests, any attempt to execute the real `/usr/bin/security` (the macOS Keychain) fails loudly.

    The choke point is `core.proc`, so tests that inject a fake runner, or patch `proc.run_sync` themselves, still work.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    from yordamchi.core import proc

    real_run_sync = proc.run_sync

    def guarded_run_sync(argv: Sequence[str], **kwargs: object) -> proc.ProcResult:
        if argv and str(argv[0]).endswith("/security"):
            raise AssertionError("a test reached the real Keychain; use tests.fakes.security.FakeSecurity")
        return real_run_sync(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(proc, "run_sync", guarded_run_sync)
