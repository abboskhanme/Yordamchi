"""`bridge.service.project_names` (T77): the dev bot's project names for the personal bot's speech vocabulary, read
through the read-only bridge. Best effort: no bridge, or a handle that is not read-only, gives none, never an error."""

from dataclasses import replace

from tests.factories import Fakes, add_digest_row
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.bridge import service
from yordamchi.shared.bridge.domain import ProjectName


async def test_the_dev_bots_projects_are_named(personal_deps: PersonalDeps, dev_db: Database, fakes: Fakes) -> None:
    await add_digest_row(dev_db, "BonviHR", at=fakes.clock.now())
    await add_digest_row(dev_db, "HRMentor", at=fakes.clock.now())
    names = await service.project_names(personal_deps, timeout=2.0)
    assert sorted(names, key=lambda name: name.id) == [ProjectName(id="BonviHR"), ProjectName(id="HRMentor")]


async def test_without_a_read_only_bridge_there_are_none(personal_deps: PersonalDeps, dev_db: Database) -> None:
    assert await service.project_names(replace(personal_deps, peer_db=None), timeout=2.0) == ()
    assert await service.project_names(replace(personal_deps, peer_db=dev_db), timeout=2.0) == ()
