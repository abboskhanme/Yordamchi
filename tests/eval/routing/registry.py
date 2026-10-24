"""The owner's project registry as the routing eval sees it (UC-10, UC-18; SPEC §3.2.6).

The registry is what the real scan (`dev/projects.service.sync_projects`) makes of `~/Documents/Projects`: one
project per top-level folder, its id the folder name exactly, its description the first prose paragraph of README,
CLAUDE.md or the SPEC, and the worktree and archive flags. The scan only lists folders and reads the first bytes of
those files; git is the fake, since routing reads no git fact.

`registry.json` is that scan, snapshotted, so the eval is hermetic: `make check` writes these rows into each case's
SQLite file and never reads the owner's disk. The live record pass (`pytest -m live --record-llm tests/eval/routing`)
scans again and rewrites the snapshot before it records, because the descriptions are part of every routing ask.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from tests.eval.casefile import CaseFileError, as_bool, as_list, as_str, fields
from tests.factories import Fakes, add_setting_row, make_dev_deps, make_paths, make_project_record, open_test_db
from yordamchi.core.paths import BotName
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects import store as projects_store
from yordamchi.dev.projects.domain import DescriptionSource
from yordamchi.persistence.uow import Session
from yordamchi.shared.settings import defaults

REGISTRY_FILE: Final = Path(__file__).with_name("registry.json")
OWNER_ROOT: Final = "~/Documents/Projects"  # `projects.root` default


def owner_root() -> Path:
    return Path(OWNER_ROOT).expanduser()


@dataclass(frozen=True, slots=True)
class Entry:
    """One `projects` row as far as routing reads it."""

    id: str
    description: str
    description_source: DescriptionSource
    is_worktree: bool
    is_archive_name: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    root: str
    skipped: int  # top-level entries that are not projects (BONVI_SERVER.md, .DS_Store)
    entries: tuple[Entry, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries)


def _entry(raw: object, where: str) -> Entry:
    row = fields(raw, where, required={"id", "description", "description_source", "is_worktree", "is_archive_name"})
    source = as_str(row["description_source"], f"{where} description_source")
    try:
        parsed = DescriptionSource(source)
    except ValueError:
        msg = f"{where}: unknown description_source {source!r}"
        raise CaseFileError(msg) from None
    return Entry(
        id=as_str(row["id"], f"{where} id"),
        description=as_str(row["description"], f"{where} description"),
        description_source=parsed,
        is_worktree=as_bool(row["is_worktree"], f"{where} is_worktree"),
        is_archive_name=as_bool(row["is_archive_name"], f"{where} is_archive_name"),
    )


def load(path: Path = REGISTRY_FILE) -> Snapshot:
    document: object = json.loads(path.read_text(encoding="utf-8"))
    top = fields(document, str(path), required={"root", "skipped", "projects"})
    skipped = top["skipped"]
    if isinstance(skipped, bool) or not isinstance(skipped, int):
        msg = f"{path}: skipped is not an integer"
        raise CaseFileError(msg)
    raw = as_list(top["projects"], f"{path}: projects")
    entries = tuple(_entry(item, f"{path}: projects[{index}]") for index, item in enumerate(raw))
    return Snapshot(root=as_str(top["root"], f"{path}: root"), skipped=skipped, entries=entries)


def dump(snapshot: Snapshot, path: Path = REGISTRY_FILE) -> None:
    rows = [
        {
            "id": entry.id,
            "description": entry.description,
            "description_source": entry.description_source.value,
            "is_worktree": entry.is_worktree,
            "is_archive_name": entry.is_archive_name,
        }
        for entry in snapshot.entries
    ]
    document = {"root": snapshot.root, "skipped": snapshot.skipped, "projects": rows}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def add_rows(s: Session, snapshot: Snapshot, *, at: datetime) -> None:
    """The snapshot as `projects` rows, the way the scan writes them."""
    for entry in snapshot.entries:
        record = make_project_record(
            entry.id,
            at=at,
            description=entry.description,
            description_source=entry.description_source,
            is_worktree=entry.is_worktree,
            is_archive_name=entry.is_archive_name,
        )
        await projects_store.set_project(s, record)


async def scan(root: Path, scratch: Path) -> Snapshot:
    """The real scan of `root` (read only) into a scratch database under `scratch`, read back as a snapshot."""
    paths = make_paths(scratch)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    try:
        deps = make_dev_deps(db=db, paths=paths, fakes=fakes)
        await add_setting_row(db, defaults.PROJECTS_ROOT.key, str(root), at=fakes.clock.now())
        report = await projects.sync_projects(deps)
        if not report.root_readable:
            msg = f"{root} cannot be listed"
            raise CaseFileError(msg)
        found = await projects.list_projects(deps)
    finally:
        await db.dispose()
    entries = tuple(
        Entry(
            id=project.id,
            description=project.description,
            description_source=project.description_source,
            is_worktree=project.is_worktree,
            is_archive_name=project.is_archive_name,
        )
        for project in found
    )
    return Snapshot(root=OWNER_ROOT, skipped=report.skipped, entries=entries)
