"""`bridge.service` on a real dev.db opened read-only (`mode=ro`, `query_only`), as the personal bot opens it.

Proves UC-36: a status answer comes from the digest with its freshness line; a write through the bridge's engine
fails; the bridge refuses a read-write handle; the tools hand the model data only.
"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from tests.factories import Fakes, add_digest_row, add_server_check_row
from yordamchi.core import strings as S
from yordamchi.core.errors import BridgeNotReadOnly, BridgeUnavailable
from yordamchi.persistence.uow import Database, ReadOnlyDatabase
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.bridge import service
from yordamchi.shared.bridge.domain import ProjectRef

TIMEOUT = 2.0
ZONE = "Asia/Tashkent"


async def test_a_plain_status_question_is_answered_from_the_digest(
    personal_deps: PersonalDeps, dev_db: Database, fakes: Fakes
) -> None:
    now = fakes.clock.now()
    await add_digest_row(dev_db, "BonviHR", at=now, checked_at=now - timedelta(minutes=3), description="HR tizimi")
    text = await service.status_reply(personal_deps, "BonviHR qanday?", timezone=ZONE, timeout=TIMEOUT)
    assert text is not None
    assert text.splitlines()[0] == S.BRIDGE_PROJECT(project="BonviHR", description="HR tizimi")
    assert "Login sahifasi tuzatildi" in text
    assert text.splitlines()[-1] == S.FRESHNESS(age=S.AGE_MINUTES(count=3))


async def test_other_messages_and_unknown_projects_return_none(
    personal_deps: PersonalDeps, dev_db: Database, fakes: Fakes
) -> None:
    await add_digest_row(dev_db, "BonviHR", at=fakes.clock.now())
    assert await service.status_reply(personal_deps, "Kvant kompyuter nima?", timezone=ZONE, timeout=TIMEOUT) is None
    assert await service.status_reply(personal_deps, "HRMentor qanday?", timezone=ZONE, timeout=TIMEOUT) is None


async def test_without_a_bridge_the_assistant_answers(personal_deps: PersonalDeps) -> None:
    deps = replace(personal_deps, peer_db=None)
    assert await service.status_reply(deps, "BonviHR qanday?", timezone=ZONE, timeout=TIMEOUT) is None
    with pytest.raises(BridgeUnavailable):
        await service.list_projects(deps, timeout=TIMEOUT)


async def test_a_write_through_the_bridge_engine_fails(personal_deps: PersonalDeps, dev_db: Database) -> None:
    """The handle is `mode=ro` + `query_only`: SQLite itself refuses any write, and no unit of work opens on it."""
    bridge_db = personal_deps.peer_db
    assert bridge_db is not None
    await service.check_read_only(personal_deps, timeout=TIMEOUT)
    async with bridge_db.engine.connect() as connection:
        with pytest.raises(OperationalError, match=r"readonly|read-only"):
            await connection.exec_driver_sql(
                "INSERT INTO projects (id, path, description, description_source, is_worktree, is_archive_name, "
                "executor, deploy_branches_json, discovered_at, last_seen_at) "
                "VALUES ('X', '/x', '', 'none', 0, 0, 'claude', '{}', '2026-01-01', '2026-01-01')"
            )
    with pytest.raises(ReadOnlyDatabase):
        async with bridge_db.uow():
            pass
    async with dev_db.read() as s:
        connection = await s.connection()
        rows = (await connection.exec_driver_sql("SELECT count(*) FROM projects")).scalar_one()
    assert rows == 0


async def test_the_bridge_refuses_a_read_write_handle(personal_deps: PersonalDeps, dev_db: Database) -> None:
    deps = replace(personal_deps, peer_db=dev_db)  # the dev bot's own read-write handle: never the bridge's
    with pytest.raises(BridgeNotReadOnly):
        await service.check_read_only(deps, timeout=TIMEOUT)
    with pytest.raises(BridgeNotReadOnly):
        await service.list_projects(deps, timeout=TIMEOUT)


async def test_the_project_tool_returns_data_and_logs_its_freshness(
    personal_deps: PersonalDeps, dev_db: Database, fakes: Fakes
) -> None:
    now = fakes.clock.now()
    await add_digest_row(dev_db, "BonviHR", at=now, checked_at=now - timedelta(minutes=7), ahead=2)
    log = service.ToolLog()
    toolset = service.toolset(personal_deps, timeout_s=TIMEOUT, tool_log=log)
    assert toolset.server == "yordamchi_bridge"
    assert toolset.tool_names == ("mcp__yordamchi_bridge__project_status", "mcp__yordamchi_bridge__server_status")
    project_tool = toolset.tools[0]
    assert all(tool.read_only for tool in toolset.tools)

    output = await project_tool.handler({"project": "bonvihr"})
    data = json.loads(output.text)
    assert output.is_error is False
    assert data["project"] == "BonviHR"
    assert data["data_minutes_ago"] == 7
    assert data["git"]["ahead"] == 2
    assert log.read == [("BonviHR", now - timedelta(minutes=7))]
    assert log.oldest == now - timedelta(minutes=7)

    unknown = json.loads((await project_tool.handler({"project": "Nope"})).text)
    assert unknown["known_projects"] == ["BonviHR"]
    assert (await project_tool.handler({})).is_error is True


async def test_the_server_tool_reads_the_latest_check_per_host(
    personal_deps: PersonalDeps, dev_db: Database, fakes: Fakes
) -> None:
    now = fakes.clock.now()
    await add_server_check_row(dev_db, "bonvi-srv", at=now - timedelta(hours=2), ok=True)
    await add_server_check_row(dev_db, "bonvi-srv", at=now - timedelta(minutes=5), ok=False, issues=("/",))
    await add_server_check_row(dev_db, "old-host", at=now - timedelta(days=3), ok=True)
    toolset = service.toolset(personal_deps, timeout_s=TIMEOUT, tool_log=service.ToolLog())
    data = json.loads((await toolset.tools[1].handler({})).text)
    assert data == {
        "servers": [{"host": "bonvi-srv", "ok": False, "summary": "disk / 91", "error": None, "minutes_ago": 5}]
    }


async def test_a_missing_bridge_is_an_error_result_not_a_raise(personal_deps: PersonalDeps) -> None:
    deps = replace(personal_deps, peer_db=None)
    toolset = service.toolset(deps, timeout_s=TIMEOUT, tool_log=service.ToolLog())
    output = await toolset.tools[0].handler({"project": "BonviHR"})
    assert output.is_error is True
    assert output.text == "bridge_unavailable"


async def test_a_project_without_a_digest_reads_as_empty(personal_deps: PersonalDeps) -> None:
    status = await service.project_status(personal_deps, ProjectRef("BonviHR"), timeout=TIMEOUT)
    assert status.has_digest is False
