"""Retention (SPEC §4.2; TASKS R4: every table ships its retention function).

- The inventory (`inventory.RETENTION` / `KEPT`) names every table exactly once, and every `apply_retention` there is.
- The dev bot runs each of them daily (`DevApp.retain`), apart from the documented `NOT_SCHEDULED_YET`.
- On the release DB, brought to head by the real chain as the next release finds it, 401 days later (past every
  period), each function prunes its own tables and nothing that is kept, never a row still in flight, and the foreign
  keys hold; the end state does not depend on the order, and a second run changes nothing.
"""

import importlib
import pkgutil
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from pathlib import Path
from typing import Final, cast

import pytest

from tests.factories import Fakes, make_dev_deps, make_paths, make_personal_deps
from tests.fakes.clock import FakeClock
from tests.migrations import inventory
from tests.migrations.harness import Table, foreign_key_violations, read_tables, release_at_head
from tests.migrations.p1_release import RELEASE_AT
from yordamchi.bots.dev import runtime
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import domain as gate_domain
from yordamchi.dev.newproject import domain as newproject_domain
from yordamchi.dev.questions import domain as questions_domain
from yordamchi.dev.routing import domain as routing_domain
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.persistence.base import import_all_models
from yordamchi.persistence.engine import open_database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa import store as qa_store
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.health import service as health_service
from yordamchi.shared.inbox import domain as inbox_domain
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.voice import domain as voice_domain

type Retain = Callable[[DevDeps | PersonalDeps], Awaitable[int]]

LATER: Final = RELEASE_AT + timedelta(days=401)  # past the longest period (400 d)
SERVICES: Final = sorted({rule.service for rule in inventory.RETENTION.values()})
PERSONAL_ONLY: Final = "yordamchi.personal."  # the dev bot never runs these; the personal bot runs shared + these
# SPEC §4.2 / §4.6: retention never touches a row in a non-terminal state.
IN_FLIGHT: Final[dict[str, frozenset[str]]] = {
    "inbound_updates": frozenset({"received"}),
    "bursts": frozenset({"open", "holding", "closed"}),
    "transcripts": frozenset({"pending"}),
    "outbox_messages": frozenset({"pending", "sending"}),
    "turns": frozenset(
        {"queued", "starting", "running", "waiting_question", "waiting_decision", "waiting_limit"}
        | {"paused_runaway", "stopping"}
    ),
    "questions": frozenset({"open"}),
    "decisions": frozenset({"pending", "approved"}),
    "clarifications": frozenset({"open"}),
    "project_drafts": frozenset({"collecting", "creating", "planning", "awaiting_approval", "revising", "building"}),
    "assistant_sessions": frozenset({"live"}),
}
SET_NULL: Final = {
    (child, column) for (child, column), (_, action) in inventory.FOREIGN_KEYS.items() if action == "SET NULL"
}


def _retention_modules() -> set[str]:
    """Every `yordamchi.**.service` module that defines `apply_retention`."""
    root = importlib.import_module("yordamchi")
    found: set[str] = set()
    for info in pkgutil.walk_packages(root.__path__, prefix="yordamchi."):
        if info.name.rsplit(".", 1)[-1] == "service" and callable(
            getattr(importlib.import_module(info.name), "apply_retention", None)
        ):
            found.add(info.name)
    return found


def _retain(service: str) -> Retain:
    return cast("Retain", importlib.import_module(service).apply_retention)


def test_the_in_flight_statuses_are_real_and_never_terminal() -> None:
    """`IN_FLIGHT` is written from SPEC §4.6, not copied from the code; here it meets the code's own status sets."""
    domains: dict[str, tuple[set[str], set[str]]] = {
        "inbound_updates": ({s.value for s in inbox_domain.UpdateStatus}, {s.value for s in inbox_domain.TERMINAL}),
        "bursts": ({s.value for s in burst_domain.BurstStatus}, {s.value for s in burst_domain.TERMINAL}),
        "transcripts": ({s.value for s in voice_domain.SttStatus}, {s.value for s in voice_domain.TERMINAL}),
        "outbox_messages": ({s.value for s in outbox_domain.Status}, {s.value for s in outbox_domain.TERMINAL}),
        "turns": ({s.value for s in turns_domain.TurnStatus}, {s.value for s in turns_domain.TERMINAL}),
        "questions": ({s.value for s in questions_domain.QuestionStatus}, {s.value for s in questions_domain.TERMINAL}),
        "decisions": (
            set(gate_domain.OPEN_STATUSES) | set(gate_domain.TERMINAL_STATUSES),
            set(gate_domain.TERMINAL_STATUSES),
        ),
        "clarifications": ({s.value for s in routing_domain.ClarificationStatus}, {"resolved", "expired"}),
        "project_drafts": (
            {s.value for s in newproject_domain.DraftStatus},
            {s.value for s in newproject_domain.TERMINAL},
        ),
        "assistant_sessions": ({qa_store.LIVE, qa_store.CLOSED}, {qa_store.CLOSED}),
    }
    assert set(domains) == set(IN_FLIGHT)
    for table, (statuses, terminal) in domains.items():
        assert IN_FLIGHT[table] <= statuses, table
        assert IN_FLIGHT[table] & terminal == set(), table
        assert IN_FLIGHT[table] | terminal == statuses, table  # every status is one or the other


def test_every_table_has_a_retention_rule_or_a_reason_to_be_kept() -> None:
    tables = set(import_all_models().tables)
    assert set(inventory.RETENTION) & set(inventory.KEPT) == set()
    assert tables - set(inventory.RETENTION) - set(inventory.KEPT) == set(), "a table without a SPEC §4.2 retention"
    assert set(inventory.RETENTION) | set(inventory.KEPT) == tables


def test_every_retention_rule_names_a_real_apply_retention() -> None:
    assert set(SERVICES) == _retention_modules()
    assert set(inventory.NOT_SCHEDULED_YET) <= _retention_modules()


async def test_the_dev_bot_runs_every_retention_daily(dev_deps: DevDeps, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def recorder(name: str) -> Callable[[DevDeps], Awaitable[int]]:
        async def record(deps: DevDeps) -> int:
            del deps
            called.append(name)
            return 0

        return record

    for name in _retention_modules():
        monkeypatch.setattr(importlib.import_module(name), "apply_retention", recorder(name))
    monkeypatch.setattr(health_service, "purge_media", recorder("media"))
    app = runtime.build_app(dev_deps, bot=None, version="2026.10.24-test")
    await app.retain()
    dev_side = {name for name in _retention_modules() if not name.startswith(PERSONAL_ONLY)}
    assert sorted(called) == sorted({*(dev_side - set(inventory.NOT_SCHEDULED_YET)), "media"})


async def _run(db_path: Path, workdir: Path, services: Iterable[str]) -> int:
    """Each service's retention on `db_path` at `LATER`, with the deps of the bot that runs it."""
    db = open_database(db_path)
    fakes, paths = Fakes(clock=FakeClock(LATER)), make_paths(workdir)
    dev, personal = make_dev_deps(db=db, paths=paths, fakes=fakes), make_personal_deps(db=db, paths=paths, fakes=fakes)
    try:
        return sum(
            [await _retain(service)(personal if service.startswith(PERSONAL_ONLY) else dev) for service in services]
        )
    finally:
        await db.dispose()


def _changed(before: dict[str, Table], after: dict[str, Table]) -> set[str]:
    return {name for name, table in before.items() if after[name].project(table.columns) != table.rows}


def _by_key(table: Table) -> dict[tuple[object, ...], dict[str, object]]:
    index = [table.columns.index(column) for column in table.key]
    return {tuple(row[i] for i in index): dict(zip(table.columns, row, strict=True)) for row in table.rows}


@pytest.mark.parametrize("service", SERVICES)
async def test_each_retention_prunes_its_own_tables_and_nothing_kept(tmp_path: Path, service: str) -> None:
    copy = release_at_head(tmp_path)
    before = read_tables(copy)
    assert await _run(copy, tmp_path, [service]) > 0
    after = read_tables(copy)
    changed = _changed(before, after)
    own = {table for table, rule in inventory.RETENTION.items() if rule.service == service}
    assert own <= changed, f"{service}.apply_retention left {sorted(own - changed)} untouched"
    assert changed & set(inventory.KEPT) == set()
    for table in own:
        fewer = len(after[table].rows) < len(before[table].rows)
        assert fewer is inventory.RETENTION[table].deletes_rows, table
    assert foreign_key_violations(copy) == []


async def test_retention_keeps_in_flight_rows_and_prunes_the_rest(tmp_path: Path) -> None:
    copy = release_at_head(tmp_path)
    before = read_tables(copy)
    await _run(copy, tmp_path, SERVICES)
    after = read_tables(copy)

    for table in inventory.KEPT:
        assert after[table].rows == before[table].rows, table
    for table, statuses in IN_FLIGHT.items():
        old, new = _by_key(before[table]), _by_key(after[table])
        expected = {key for key, row in old.items() if row["status"] in statuses}
        assert set(new) == expected, table
        for key in expected:  # untouched, apart from a pointer to a pruned parent (ON DELETE SET NULL)
            changed = {column for column, value in old[key].items() if new[key][column] != value}
            assert changed <= {column for child, column in SET_NULL if child == table}, (table, key, changed)
            assert all(new[key][column] is None for column in changed), (table, key, changed)
    surviving_turns = {key[0] for key in _by_key(after["turns"])}
    assert {row["turn_id"] for row in _by_key(after["deliveries"]).values()} <= surviving_turns
    time_based = ("message_links", "audit_events", "usage_records", "cap_notices", "health_events", "turn_progress")
    for table in (*time_based, "route_decisions", "server_checks", "answers"):
        assert after[table].rows == (), table  # no state to wait for, and everything is past its period
    removed = {row["id"] for row in _by_key(before["integrations"]).values() if row["removed_at"] is not None}
    assert {row["id"] for row in _by_key(after["integrations"]).values()} & removed == set()
    assert {row["integration_id"] for row in _by_key(after["oauth_clients"]).values()} & removed == set()
    vault = _by_key(after["vault_secrets"])
    assert len(vault) == len(before["vault_secrets"].rows)
    assert all((row["ciphertext"] is None) is (row["removed_at"] is not None) for row in vault.values())
    # The queued turn pointed at a burst that retention deleted: the turn stays, its pointer is nulled.
    queued = [row for row in _by_key(after["turns"]).values() if row["status"] == "queued"]
    assert queued
    assert all(row["burst_id"] is None for row in queued)
    assert [row for row in _by_key(before["turns"]).values() if row["status"] == "queued" and row["burst_id"]]
    assert foreign_key_violations(copy) == []


async def test_retention_order_does_not_matter_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    forward = release_at_head(tmp_path / "forward")
    backward = release_at_head(tmp_path / "backward")
    await _run(forward, tmp_path / "forward", SERVICES)
    await _run(backward, tmp_path / "backward", reversed(SERVICES))
    once = read_tables(forward)
    assert read_tables(backward) == once
    assert await _run(forward, tmp_path / "forward", SERVICES) == 0
    assert read_tables(forward) == once
