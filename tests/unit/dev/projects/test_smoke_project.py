"""The hidden smoke project (SPEC §5.6, NFR-REL-08; T32): the CLI smoke turn's scratch repo has a `projects` row, but
the owner never sees it, routing never picks it, and the scan never marks it missing."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.factories import Fakes, make_project_folder, make_project_record, projects_root_of
from yordamchi.core.errors import ProjectNotFound
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import domain, service
from yordamchi.dev.projects.domain import NO_DEPLOY, SMOKE_PROJECT_ID

AT = datetime(2026, 10, 23, 9, tzinfo=UTC)
SCRATCH = "/data/sessions/smoke"


def test_hidden_ids_are_the_ones_no_scanned_folder_can_have() -> None:
    assert domain.is_hidden(SMOKE_PROJECT_ID)
    assert not domain.is_hidden("BonviHR")
    assert domain.skip_reason(SMOKE_PROJECT_ID, is_dir=True, is_symlink=False) is domain.SkipReason.HIDDEN


def test_the_smoke_record_deploys_nothing_and_keeps_its_first_discovery() -> None:
    record = domain.smoke_record(SCRATCH, previous=None, now=AT)
    assert (record.id, record.path, record.deploy, record.git_remote_url, record.missing_since) == (
        SMOKE_PROJECT_ID,
        SCRATCH,
        NO_DEPLOY,
        None,
        None,
    )
    later = domain.smoke_record(SCRATCH, previous=record, now=AT + timedelta(days=3))
    assert (later.discovered_at, later.last_seen_at) == (AT, AT + timedelta(days=3))


def test_a_scan_never_counts_a_hidden_project_as_vanished() -> None:
    known = {
        SMOKE_PROJECT_ID: domain.smoke_record(SCRATCH, previous=None, now=AT),
        "Gone": make_project_record("Gone", at=AT),
    }
    assert domain.vanished(known, seen=()) == ("Gone",)


async def test_the_smoke_project_is_reachable_by_its_id_only(dev_deps: DevDeps, paths: Paths) -> None:
    project = await service.ensure_smoke_project(dev_deps)

    assert project.id == SMOKE_PROJECT_ID
    assert str(project.path) == str(paths.session_dir("smoke"))
    assert not project.deploy.deploys
    assert (await service.get_project(dev_deps, SMOKE_PROJECT_ID)).id == SMOKE_PROJECT_ID
    assert await service.list_projects(dev_deps) == ()
    for name in (SMOKE_PROJECT_ID, "smoke"):
        with pytest.raises(ProjectNotFound):
            await service.find_project(dev_deps, name)
    assert SMOKE_PROJECT_ID not in await service.show_projects(dev_deps)


async def test_registering_it_again_only_moves_its_last_seen_time(dev_deps: DevDeps, fakes: Fakes) -> None:
    first = await service.ensure_smoke_project(dev_deps)
    await fakes.clock.advance(600)
    again = await service.ensure_smoke_project(dev_deps)
    assert again.discovered_at == first.discovered_at
    assert again.last_seen_at == fakes.clock.now()


async def test_a_scan_of_the_projects_root_leaves_the_smoke_project_alone(dev_deps: DevDeps, paths: Paths) -> None:
    make_project_folder(projects_root_of(paths), "BonviHR")
    await service.ensure_smoke_project(dev_deps)

    report = await service.sync_projects(dev_deps)

    assert report.vanished == ()
    assert [project.id for project in await service.list_projects(dev_deps)] == ["BonviHR"]
    assert not (await service.get_project(dev_deps, SMOKE_PROJECT_ID)).missing
