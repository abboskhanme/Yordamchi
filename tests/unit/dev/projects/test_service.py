"""projects.service on a real SQLite file with `FakeGit`: the scan (register, refresh, missing, unreadable root,
≥ 40 projects), git facts and failures, the cached and the fresh deploy check, the owner's chat edits, and `loyihalar`
(SPEC §3.2.6, §4.2, §5.7; UC-10, UC-13 #1, UC-18; NFR-CAP-01)."""

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from tests.factories import (
    FAKE_KEYS,
    WORKFLOW_FIXTURES,
    Fakes,
    make_owner_origin,
    make_project,
    make_project_folder,
    projects_root_of,
)
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    CallTimedOut,
    GitFailed,
    ProjectAliasTaken,
    ProjectDescriptionInvalid,
    ProjectNotFound,
)
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import domain, service
from yordamchi.dev.projects.domain import FAIL_CLOSED, NO_DEPLOY, DescriptionSource
from yordamchi.ports.git import Commit
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

ORIGIN = make_owner_origin(update_id=700)
WUNDERKIND = (
    "WunderkindAIAgent",
    "WunderkindEmployeesBot",
    "WunderkindLC",
    "WunderkindLMS",
    "WunderkindOlympics",
    "WunderkindTestPortal",
)


@pytest.fixture
def root(paths: Paths) -> Path:
    folder = projects_root_of(paths)
    folder.mkdir(parents=True)
    return folder


def _fixture(name: str) -> str:
    return (WORKFLOW_FIXTURES / name).read_text(encoding="utf-8")


def _owner_layout(root: Path, tmp_path: Path) -> None:
    """The shape of the owner's ~/Documents/Projects (REQUIREMENTS §6): look-alikes, a worktree, an archive, files."""
    make_project_folder(root, "BonviHR", readme="# BonviHR\n\nKadrlar tizimi: xodimlar va ta'tillar.\n", git_dir=True)
    make_project_folder(root, "HRMentor", claude_md="# HRMentor\n\nHR bo'yicha maslahatchi agent.\n", git_dir=True)
    worktree = make_project_folder(root, "HRMentor-wt-agent-api")
    (worktree / ".git").write_text("gitdir: ../HRMentor/.git/worktrees/agent-api\n", encoding="utf-8")
    for name in WUNDERKIND:
        make_project_folder(root, name, git_dir=True)
    make_project_folder(root, "BonviCall Clone Archive", git_dir=True)
    make_project_folder(root, "MegaPlus ERP", spec="# SPEC\n\n## 1. Goal\n\nERP for a building-materials trader.\n")
    make_project_folder(root, "GasStation")  # empty: still a project
    (root / "BONVI_SERVER.md").write_text("# server notes\n", encoding="utf-8")
    (root / "WunderkindLC.zip").write_bytes(b"PK\x03\x04")
    (root / ".DS_Store").write_bytes(b"\0")
    (tmp_path / "elsewhere").mkdir()
    (root / "Linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)


def _commit(sha: str, *, at: datetime) -> Commit:
    return Commit(sha=sha, subject="work", committed_at=at)


def _calls(fakes: Fakes, method: str) -> list[tuple[str, Path, tuple[str, ...]]]:
    return [call for call in fakes.git.calls if call[0] == method]


# --- the scan -------------------------------------------------------------------------------------------------------
async def test_a_scan_registers_project_folders_and_skips_everything_else(
    dev_deps: DevDeps, root: Path, tmp_path: Path, fakes: Fakes
) -> None:
    _owner_layout(root, tmp_path)
    report = await service.sync_projects(dev_deps)

    look_alikes = ["BonviHR", "HRMentor", "HRMentor-wt-agent-api", *WUNDERKIND]
    expected = sorted([*look_alikes, "BonviCall Clone Archive", "MegaPlus ERP", "GasStation"])
    assert sorted(report.added) == expected
    assert (report.total, report.skipped, report.vanished, report.root_readable) == (len(expected), 4, (), True)
    by_id = {project.id: project for project in await service.list_projects(dev_deps)}
    assert sorted(by_id) == expected
    assert by_id["BonviHR"].path == PurePosixPath(root / "BonviHR")
    assert (by_id["BonviHR"].description, by_id["BonviHR"].description_source) == (
        "Kadrlar tizimi: xodimlar va ta'tillar.",
        DescriptionSource.README,
    )
    assert by_id["HRMentor"].description_source is DescriptionSource.CLAUDE_MD
    assert by_id["MegaPlus ERP"].description_source is DescriptionSource.SPEC
    assert by_id["GasStation"].description_source is DescriptionSource.NONE
    assert (by_id["HRMentor-wt-agent-api"].is_worktree, by_id["HRMentor-wt-agent-api"].routable) == (True, False)
    assert (by_id["BonviCall Clone Archive"].archived, by_id["BonviCall Clone Archive"].routable) == (True, False)
    assert all(by_id[name].routable for name in ("BonviHR", "HRMentor", *WUNDERKIND))
    assert {project.discovered_at for project in by_id.values()} == {fakes.clock.now()}
    # git is asked only about folders that hold a repository
    assert {call[1].name for call in _calls(fakes, "find_commit")} == {
        "BonviHR",
        "HRMentor",
        "HRMentor-wt-agent-api",
        "BonviCall Clone Archive",
        *WUNDERKIND,
    }


async def test_a_new_folder_is_registered_by_the_next_scan(dev_deps: DevDeps, root: Path, fakes: Fakes) -> None:
    """UC-18 AC: listed within 5 min — the scheduler scans every SCAN_INTERVAL_S, which leaves room for the scan."""
    assert domain.SCAN_INTERVAL_S <= 240
    make_project_folder(root, "BonviHR")
    await service.sync_projects(dev_deps)
    make_project_folder(root, "BonviGo", readme="BonviGo — yetkazib berish ilovasi, Flutter.\n")
    fakes.clock.set(fakes.clock.now() + timedelta(seconds=domain.SCAN_INTERVAL_S))

    report = await service.sync_projects(dev_deps)
    assert report.added == ("BonviGo",)
    assert "BonviGo" in await service.show_projects(dev_deps)
    first = await service.get_project(dev_deps, "BonviHR")
    assert first.discovered_at < first.last_seen_at


async def test_a_vanished_folder_is_kept_as_missing_and_comes_back(dev_deps: DevDeps, root: Path, fakes: Fakes) -> None:
    folder = make_project_folder(root, "BonviAGI")
    make_project_folder(root, "BonviHR")
    await service.sync_projects(dev_deps)
    folder.rmdir()
    gone_at = fakes.clock.now() + timedelta(minutes=2)
    fakes.clock.set(gone_at)

    report = await service.sync_projects(dev_deps)
    assert (report.vanished, report.total) == (("BonviAGI",), 1)
    missing = await service.get_project(dev_deps, "BonviAGI")
    assert (missing.missing, missing.routable) == (True, False)
    fakes.clock.set(gone_at + timedelta(minutes=2))
    assert (await service.sync_projects(dev_deps)).vanished == ()  # marked once

    make_project_folder(root, "BonviAGI")
    report = await service.sync_projects(dev_deps)
    assert report.returned == ("BonviAGI",)
    assert not (await service.get_project(dev_deps, "BonviAGI")).missing


async def test_an_unreadable_root_changes_nothing(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR")
    await service.sync_projects(dev_deps)
    await asyncio.to_thread(root.rename, root.with_name("Moved"))

    report = await service.sync_projects(dev_deps)
    assert (report.root_readable, report.total) == (False, 0)
    assert not (await service.get_project(dev_deps, "BonviHR")).missing  # a detached disk marks nothing missing


async def test_the_root_comes_from_settings(dev_deps: DevDeps, tmp_path: Path) -> None:
    other = tmp_path / "work"
    make_project_folder(other, "Elsewhere")
    await settings.record_version(dev_deps, ORIGIN, defaults.PROJECTS_ROOT, str(other), reason="change")
    assert (await service.sync_projects(dev_deps)).added == ("Elsewhere",)
    assert (await service.get_project(dev_deps, "Elsewhere")).path == PurePosixPath(other / "Elsewhere")


async def test_forty_five_projects_are_registered_in_one_quick_scan(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    """NFR-CAP-01: ≥ 40 projects registered."""
    names = [f"Project{index:02d}" for index in range(45)]
    for name in names:
        make_project_folder(root, name, readme=f"# {name}\n\nLoyiha raqami {name}.\n", git_dir=True)
        fakes.git.commits[(root / name, "HEAD")] = _commit(f"sha{name}", at=fakes.clock.now())
    started = time.monotonic()
    report = await service.sync_projects(dev_deps)
    assert time.monotonic() - started < 5.0
    assert len(report.added) == 45
    assert [project.id for project in await service.list_projects(dev_deps)] == names
    assert len(_calls(fakes, "find_commit")) == 45


# --- git facts --------------------------------------------------------------------------------------------------------
async def test_git_facts_and_activity_come_from_the_repository(dev_deps: DevDeps, root: Path, fakes: Fakes) -> None:
    repo = make_project_folder(root, "BonviHR", git_dir=True)
    committed = fakes.clock.now() - timedelta(days=2)
    fakes.git.commits[(repo, "HEAD")] = _commit("a1b2c3d", at=committed)
    fakes.git.remotes[(repo, "origin")] = "git@github.com:abboskhanme/BonviHR.git"
    fakes.git.default_branches[repo] = "main"

    await service.sync_projects(dev_deps)
    project = await service.get_project(dev_deps, "BonviHR")
    assert (project.git_remote_url, project.default_branch, project.last_activity_at) == (
        "git@github.com:abboskhanme/BonviHR.git",
        "main",
        committed,
    )
    assert {call[2] for call in _calls(fakes, "remote_url")} == {("origin",)}


async def test_a_git_failure_keeps_the_project_and_its_last_facts(dev_deps: DevDeps, root: Path, fakes: Fakes) -> None:
    repo = make_project_folder(root, "BonviHR", git_dir=True, workflows={"d.yml": _fixture("deploy_on_push_main.yml")})
    fakes.git.default_branches[repo] = "main"
    await service.sync_projects(dev_deps)

    fakes.git.failures["find_commit"] = CallTimedOut(operation="git_find_commit")
    report = await service.sync_projects(dev_deps)
    assert (report.git_failed, report.total) == (("BonviHR",), 1)
    project = await service.get_project(dev_deps, "BonviHR")
    assert project.default_branch == "main"
    assert project.deploy.covers_branch("main")
    # The next scan checks again (no cache key was stored).
    assert len(_calls(fakes, "find_commit")) == 2
    await service.sync_projects(dev_deps)
    assert len(_calls(fakes, "default_branch")) == 2


async def test_a_readme_key_is_scrubbed_before_it_is_stored(dev_deps: DevDeps, root: Path) -> None:
    token = FAKE_KEYS["github_token"]
    make_project_folder(root, "Leaky", readme=f"Deploy bot. Use token {token} to push releases.\n")
    await service.sync_projects(dev_deps)
    description = (await service.get_project(dev_deps, "Leaky")).description
    assert token not in description
    assert description.startswith("Deploy bot.")


# --- deploy branches --------------------------------------------------------------------------------------------------
async def test_stored_deploy_branches_are_rechecked_only_when_head_or_a_workflow_changes(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    repo = make_project_folder(
        root, "Site", git_dir=True, workflows={"deploy.yml": _fixture("deploy_on_push_main.yml")}
    )
    fakes.git.commits[(repo, "HEAD")] = _commit("a1", at=fakes.clock.now())
    fakes.git.trees[(repo, "a1", domain.WORKFLOWS_DIR)] = {
        "release.yml": (WORKFLOW_FIXTURES / "release_on_tags.yml").read_bytes()
    }

    await service.sync_projects(dev_deps)
    stored = (await service.get_project(dev_deps, "Site")).deploy
    assert (stored.branches, stored.tags) == (("main",), ("v*",))  # the working tree plus the HEAD commit
    assert stored.workflows == ("deploy.yml", "release.yml")
    assert [call[2][0] for call in _calls(fakes, "tree_files")] == ["a1"]

    await service.sync_projects(dev_deps)
    assert len(_calls(fakes, "tree_files")) == 1  # nothing changed: the stored value stands

    fakes.git.commits[(repo, "HEAD")] = _commit("b2", at=fakes.clock.now())
    await service.sync_projects(dev_deps)
    assert [call[2][0] for call in _calls(fakes, "tree_files")] == ["a1", "b2"]
    assert (await service.get_project(dev_deps, "Site")).deploy.tags == ()  # b2 has no release workflow

    (repo / ".github" / "workflows" / "deploy.yml").write_text(_fixture("branches_ignore.yml"), encoding="utf-8")
    await service.sync_projects(dev_deps)
    assert len(_calls(fakes, "tree_files")) == 3
    assert (await service.get_project(dev_deps, "Site")).deploy.every_branch


async def test_a_platform_config_makes_the_default_branch_deploy_without_any_workflow(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    """T17b: a Vercel/Netlify/… Git integration deploys the default branch on push, with no workflow file."""
    repo = make_project_folder(root, "Landing", git_dir=True)
    (repo / "vercel.json").write_text("{}\n", encoding="utf-8")
    fakes.git.default_branches[repo] = "master"
    await service.sync_projects(dev_deps)

    stored = (await service.get_project(dev_deps, "Landing")).deploy
    assert (stored.branches, stored.workflows) == (("master",), ("vercel.json",))
    fresh = await service.deploy_branches(dev_deps, "Landing")
    assert fresh.covers_branch("master")
    assert not fresh.covers_branch("feature/hero")


async def test_a_platform_config_added_without_a_commit_is_seen_by_the_next_scan(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    """`vercel link` writes a git-ignored `.vercel/project.json`: no new commit, yet the cached value must change."""
    repo = make_project_folder(root, "Shop", git_dir=True)
    fakes.git.commits[(repo, "HEAD")] = _commit("a1", at=fakes.clock.now())
    fakes.git.default_branches[repo] = "main"
    await service.sync_projects(dev_deps)
    assert not (await service.get_project(dev_deps, "Shop")).deploy.deploys

    (repo / ".vercel").mkdir()
    (repo / ".vercel" / "project.json").write_text('{"projectId": "prj_x"}\n', encoding="utf-8")
    await service.sync_projects(dev_deps)
    assert (await service.get_project(dev_deps, "Shop")).deploy.branches == ("main",)


async def test_a_platform_config_with_no_known_default_branch_makes_every_branch_deploy(
    dev_deps: DevDeps, root: Path
) -> None:
    repo = make_project_folder(root, "Static")  # not a repository: git knows no default branch
    (repo / "netlify.toml").write_text("[build]\n", encoding="utf-8")
    await service.sync_projects(dev_deps)
    assert (await service.get_project(dev_deps, "Static")).deploy.every_branch
    assert (await service.deploy_branches(dev_deps, "Static")).covers_branch("any/branch")


async def test_an_unparseable_workflow_on_disk_makes_every_push_deploy(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "Odd", workflows={"broken.yml": _fixture("unparseable.yml"), "ci.yml": "on: push\n"})
    await service.sync_projects(dev_deps)
    deploy = await service.deploy_branches(dev_deps, "Odd")
    assert (deploy.every_branch, deploy.every_tag, deploy.unparseable) == (True, True, ("broken.yml",))
    assert (await service.get_project(dev_deps, "Odd")).deploy.every_branch


async def test_the_gate_check_reads_the_pushed_commit_as_well_as_the_working_tree(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    """UC-13 AC: a push to main asks once in a repo whose CI deploys on main; a feature branch does not. The commit
    being pushed counts even when the working tree no longer has that workflow."""
    repo = make_project_folder(root, "Shop", git_dir=True, workflows={"ci.yml": _fixture("ci_only.yml")})
    await service.sync_projects(dev_deps)
    fakes.git.trees[(repo, "f00d", domain.WORKFLOWS_DIR)] = {
        "deploy.yml": (WORKFLOW_FIXTURES / "deploy_on_push_main.yml").read_bytes()
    }
    deploy = await service.deploy_branches(dev_deps, "Shop", ref="f00d")
    assert deploy.covers_branch("main")
    assert not deploy.covers_branch("feature/cart")
    assert not (await service.deploy_branches(dev_deps, "Shop")).deploys  # HEAD has no such workflow


@pytest.mark.parametrize(
    "failure",
    [GitFailed(operation="git_tree_files", reason="failed"), CallTimedOut(operation="git_tree_files")],
    ids=["git_failed", "timed_out"],
)
async def test_a_commit_that_cannot_be_read_makes_every_push_deploy(
    dev_deps: DevDeps, root: Path, fakes: Fakes, failure: Exception
) -> None:
    make_project_folder(root, "Shop", git_dir=True)
    await service.sync_projects(dev_deps)
    fakes.git.failures["tree_files"] = failure
    assert await service.deploy_branches(dev_deps, "Shop") == FAIL_CLOSED


async def test_a_folder_without_a_repository_has_only_its_working_tree(
    dev_deps: DevDeps, root: Path, fakes: Fakes
) -> None:
    make_project_folder(root, "Plain", workflows={"d.yml": _fixture("deploy_on_push_main.yml")})
    await service.sync_projects(dev_deps)
    fakes.git.failures["tree_files"] = GitFailed(operation="git_tree_files", reason="not_a_repo")
    deploy = await service.deploy_branches(dev_deps, "Plain")
    assert (deploy.every_branch, deploy.branches) == (False, ("main",))


async def test_deploy_check_of_an_unknown_project_is_refused(dev_deps: DevDeps) -> None:
    with pytest.raises(ProjectNotFound):
        await service.deploy_branches(dev_deps, "Nowhere")


# --- the owner's edits (UC-18) ----------------------------------------------------------------------------------------
async def test_an_alias_added_by_chat_is_used_at_once(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR")
    make_project_folder(root, "HRMentor")
    await service.sync_projects(dev_deps)

    reply = await service.alias_project(dev_deps, ORIGIN, "bonvihr", " hr ")
    assert reply == S.PROJECT_ALIAS_ADDED(project="BonviHR", alias="hr")
    assert (await service.find_project(dev_deps, "HR")).id == "BonviHR"
    assert (await service.get_project(dev_deps, "BonviHR")).aliases == ("hr",)
    assert await service.alias_project(dev_deps, ORIGIN, "BonviHR", "hr") == reply  # again: no new version

    with pytest.raises(ProjectAliasTaken):
        await service.alias_project(dev_deps, ORIGIN, "HRMentor", "HR")
    with pytest.raises(ProjectNotFound):
        await service.alias_project(dev_deps, ORIGIN, "Bonvi", "b")


async def test_archiving_by_chat_takes_a_project_out_of_routing(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR")
    make_project_folder(root, "BonviCall Clone Archive")
    await service.sync_projects(dev_deps)

    assert await service.archive_project(dev_deps, ORIGIN, "BonviHR", archived=True) == S.PROJECT_ARCHIVED(
        project="BonviHR"
    )
    archived = await service.get_project(dev_deps, "BonviHR")
    assert (archived.archived, archived.routable) == (True, False)
    assert (await service.find_project(dev_deps, "bonvihr")).id == "BonviHR"  # still reachable by its exact name

    reply = await service.archive_project(dev_deps, ORIGIN, "BonviCall Clone Archive", archived=False)
    assert reply == S.PROJECT_UNARCHIVED(project="BonviCall Clone Archive")
    assert (await service.get_project(dev_deps, "BonviCall Clone Archive")).routable
    await service.archive_project(dev_deps, ORIGIN, "BonviHR", archived=False)
    assert (await service.get_project(dev_deps, "BonviHR")).routable


async def test_a_description_given_by_chat_replaces_the_files(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR", readme="Old text from the README file.\n")
    await service.sync_projects(dev_deps)
    reply = await service.describe_project(dev_deps, ORIGIN, "BonviHR", "Bonvi kadrlar tizimi")
    assert reply == S.PROJECT_DESCRIBED(project="BonviHR")
    project = await service.get_project(dev_deps, "BonviHR")
    assert (project.description, project.description_source) == ("Bonvi kadrlar tizimi", DescriptionSource.OWNER)
    await service.sync_projects(dev_deps)  # the scan mirrors it into the row
    assert (await service.get_project(dev_deps, "BonviHR")).description == "Bonvi kadrlar tizimi"


async def test_an_edit_needs_this_bots_owner_origin(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR")
    await service.sync_projects(dev_deps)
    with pytest.raises(ValueError, match="cannot change"):
        await service.alias_project(dev_deps, make_owner_origin(BotName.PERSONAL), "BonviHR", "hr")
    assert (await service.get_project(dev_deps, "BonviHR")).aliases == ()


async def test_loyihalar_lists_the_registry(dev_deps: DevDeps, root: Path) -> None:
    assert await service.show_projects(dev_deps) == S.PROJECTS_NONE(root="~/Documents/Projects")
    make_project_folder(root, "BonviHR", readme="Kadrlar tizimi, FastAPI va React.\n")
    make_project_folder(root, "BonviCall Clone Archive")
    await service.sync_projects(dev_deps)
    assert (await service.show_projects(dev_deps)).splitlines() == [
        S.PROJECTS_SUMMARY(active=1, archived=1),
        "• BonviHR — Kadrlar tizimi, FastAPI va React.",
        S.PROJECTS_ARCHIVED_LIST(projects="BonviCall Clone Archive"),
    ]


async def test_get_project_refuses_an_unknown_id(dev_deps: DevDeps) -> None:
    with pytest.raises(ProjectNotFound):
        await service.get_project(dev_deps, "Nowhere")


async def test_edits_of_unknown_projects_or_bad_text_change_nothing(dev_deps: DevDeps, root: Path) -> None:
    make_project_folder(root, "BonviHR")
    await service.sync_projects(dev_deps)
    with pytest.raises(ProjectNotFound):
        await service.archive_project(dev_deps, ORIGIN, "Nowhere", archived=True)
    with pytest.raises(ProjectDescriptionInvalid):
        await service.describe_project(dev_deps, ORIGIN, "BonviHR", "")
    project = await service.get_project(dev_deps, "BonviHR")
    assert (project.archived, project.description_source) == (False, DescriptionSource.NONE)


async def test_a_row_from_the_factory_reads_back_as_a_project(dev_deps: DevDeps, fakes: Fakes) -> None:
    async with dev_deps.db.uow() as s:
        await make_project(s, "BonviHR", at=fakes.clock.now())
    project = await service.get_project(dev_deps, "BonviHR")
    assert (project.id, project.default_branch, project.deploy, project.routable) == (
        "BonviHR",
        "main",
        NO_DEPLOY,
        True,
    )
