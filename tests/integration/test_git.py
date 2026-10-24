"""`GitCli` against real git on `tmp_path` repositories (CONVENTIONS §4, §11): every port call, the output parsing
(spaces, non-ASCII, renames), and the error mapping (not a repo, bad ref, conflict, lock, timeout, missing binary).

HOME points at an empty folder, so the owner's global git config (signing, hooks, aliases) never leaks in; each repo
sets its own identity.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from tests.factories import WORKFLOW_FIXTURES, projects_root_of
from yordamchi.adapters.git.cli import GitCli
from yordamchi.core import proc, retry
from yordamchi.core.errors import CallTimedOut, GitFailed, NotInstalled
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import service

SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
TIMEOUT = 20.0
OLD_DATE = "2001-01-01T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
def git() -> GitCli:
    return GitCli(SEARCH_PATH)


async def sh(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Set-up git, outside the adapter under test."""
    base = {name: value for name, value in proc.clean_env(path=SEARCH_PATH).items() if not name.startswith("GIT_")}
    result = await proc.run(["git", *args], env={**base, **(env or {})}, timeout=TIMEOUT, cwd=repo)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode().strip()


async def new_repo(path: Path, *, branch: str = "main") -> Path:
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    await sh(path, "init", "-q", f"--initial-branch={branch}")
    await sh(path, "config", "user.name", "Test")
    await sh(path, "config", "user.email", "test@example.com")
    await sh(path, "config", "commit.gpgsign", "false")
    return path


async def commit_file(repo: Path, name: str, text: str, message: str, *, date: str | None = None) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    await sh(repo, "add", "--", name)
    env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else None
    await sh(repo, "commit", "-q", "-m", message, env=env)
    return await sh(repo, "rev-parse", "HEAD")


@pytest.fixture
async def repo(tmp_path: Path) -> Path:
    path = await new_repo(tmp_path / "Bonvi HR")  # a space in the path, as in "MegaPlus ERP"
    await commit_file(path, "README.md", "# Bonvi\n", "first commit")
    return path


# --- reads ------------------------------------------------------------------------------------------------------------
async def test_head_and_commit(git: GitCli, repo: Path) -> None:
    sha = await sh(repo, "rev-parse", "HEAD")
    assert await git.head(repo, timeout=TIMEOUT) == sha
    found = await git.find_commit(repo, "HEAD", timeout=TIMEOUT)
    assert found is not None
    assert (found.sha, found.subject, found.committed_at.tzinfo is not None) == (sha, "first commit", True)
    assert await git.find_commit(repo, "no-such-ref", timeout=TIMEOUT) is None


async def test_an_empty_repo_has_no_head_but_a_branch(git: GitCli, tmp_path: Path) -> None:
    empty = await new_repo(tmp_path / "empty")
    assert await git.head(empty, timeout=TIMEOUT) is None
    assert await git.find_commit(empty, "HEAD", timeout=TIMEOUT) is None
    assert await git.current_branch(empty, timeout=TIMEOUT) == "main"
    assert await git.log(empty, since_days=7, limit=10, timeout=TIMEOUT) == []
    assert await git.tree_files(empty, "HEAD", ".github/workflows", max_bytes=1000, timeout=TIMEOUT) == {}
    assert await git.default_branch(empty, timeout=TIMEOUT) == "main"


async def test_current_branch_is_none_when_detached(git: GitCli, repo: Path) -> None:
    assert await git.current_branch(repo, timeout=TIMEOUT) == "main"
    await sh(repo, "checkout", "-q", "--detach")
    assert await git.current_branch(repo, timeout=TIMEOUT) is None
    assert (await git.status(repo, timeout=TIMEOUT)).branch is None


async def test_status_counts_changes_untracked_files_and_renames(git: GitCli, repo: Path) -> None:
    clean = await git.status(repo, timeout=TIMEOUT)
    assert (clean.branch, clean.upstream, clean.changed, clean.untracked) == ("main", None, 0, 0)
    assert clean.head == await sh(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    await commit_file(repo, "old name.txt", "x\n", "add")
    await sh(repo, "mv", "old name.txt", "yangi fayl ✓.txt")
    (repo / "notes.txt").write_text("u\n", encoding="utf-8")
    (repo / "new dir").mkdir()
    (repo / "new dir" / "a.txt").write_text("a\n", encoding="utf-8")
    status = await git.status(repo, timeout=TIMEOUT)
    assert (status.changed, status.untracked) == (2, 2)  # README + the rename; a new directory counts once


async def test_ahead_and_behind_follow_the_upstream(git: GitCli, repo: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    await sh(tmp_path, "clone", "-q", str(repo), str(clone))
    await sh(clone, "config", "user.name", "Test")
    await sh(clone, "config", "user.email", "test@example.com")
    await commit_file(clone, "local.txt", "l\n", "local work")
    await commit_file(repo, "remote.txt", "r\n", "remote work")
    await sh(clone, "fetch", "-q")
    status = await git.status(clone, timeout=TIMEOUT)
    assert (status.upstream, status.ahead, status.behind) == ("origin/main", 1, 1)


async def test_log_keeps_recent_commits_newest_first(git: GitCli, tmp_path: Path) -> None:
    history = await new_repo(tmp_path / "history")
    await commit_file(history, "old.txt", "o\n", "ancient", date=OLD_DATE)
    await commit_file(history, "a.txt", "a\n", "o'zgarish: «yangi»")
    await commit_file(history, "b.txt", "b\n", "second")
    recent = await git.log(history, since_days=7, limit=10, timeout=TIMEOUT)
    assert [item.subject for item in recent] == ["second", "o'zgarish: «yangi»"]
    assert [item.subject for item in await git.log(history, since_days=7, limit=1, timeout=TIMEOUT)] == ["second"]
    assert await git.log(history, since_days=7, limit=0, timeout=TIMEOUT) == []


async def test_commits_and_changed_files_since_a_base(git: GitCli, repo: Path) -> None:
    await commit_file(repo, "gone.txt", "g\n", "to delete")
    await commit_file(repo, "keep me.txt", "k\n", "to rename")
    base = await sh(repo, "rev-parse", "HEAD")
    await commit_file(repo, "added.py", "print()\n", "add a file")
    await sh(repo, "rm", "-q", "gone.txt")
    await sh(repo, "mv", "keep me.txt", "kept ✓.txt")
    await sh(repo, "commit", "-q", "-m", "delete and rename")
    (repo / "README.md").write_text("edited, not committed\n", encoding="utf-8")

    assert [item.subject for item in await git.commits_since(repo, base, timeout=TIMEOUT)] == [
        "delete and rename",
        "add a file",
    ]
    changes = {
        (change.status, change.path, change.old_path) for change in await git.changed_files(repo, base, timeout=TIMEOUT)
    }
    assert changes == {
        ("A", "added.py", None),
        ("D", "gone.txt", None),
        ("R", "kept ✓.txt", "keep me.txt"),
        ("M", "README.md", None),
    }


async def test_untracked_and_ignored_paths(git: GitCli, repo: Path) -> None:
    await commit_file(repo, ".gitignore", "build/\n*.log\n", "ignore")
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("x", encoding="utf-8")
    (repo / "debug.log").write_text("x", encoding="utf-8")
    (repo / "draft notes.md").write_text("x", encoding="utf-8")
    assert await git.untracked_files(repo, timeout=TIMEOUT) == ["draft notes.md"]
    asked = ["build/out.js", "debug.log", "draft notes.md", "README.md"]
    assert await git.ignored(repo, asked, timeout=TIMEOUT) == {"build/out.js", "debug.log"}
    assert await git.ignored(repo, [], timeout=TIMEOUT) == frozenset()
    assert await git.ignored(repo, ["README.md"], timeout=TIMEOUT) == frozenset()


async def test_the_default_branch_prefers_the_remotes_head(git: GitCli, tmp_path: Path) -> None:
    upstream = await new_repo(tmp_path / "upstream", branch="trunk")
    await commit_file(upstream, "a.txt", "a\n", "first")
    clone = tmp_path / "clone"
    await sh(tmp_path, "clone", "-q", str(upstream), str(clone))
    await sh(clone, "switch", "-q", "-c", "feature")
    assert await git.default_branch(clone, timeout=TIMEOUT) == "trunk"

    local = await new_repo(tmp_path / "local", branch="master")
    await commit_file(local, "a.txt", "a\n", "first")
    await sh(local, "switch", "-q", "-c", "work")
    assert await git.default_branch(local, timeout=TIMEOUT) == "master"

    other = await new_repo(tmp_path / "other", branch="dev")
    await commit_file(other, "a.txt", "a\n", "first")
    assert await git.default_branch(other, timeout=TIMEOUT) == "dev"
    await sh(other, "checkout", "-q", "--detach")
    assert await git.default_branch(other, timeout=TIMEOUT) is None


async def test_the_remote_url_never_carries_credentials(git: GitCli, repo: Path) -> None:
    assert await git.remote_url(repo, remote="origin", timeout=TIMEOUT) is None
    await sh(repo, "remote", "add", "origin", "https://abbos:ghp_secretvalue@github.com/abboskhanme/BonviHR.git")
    assert await git.remote_url(repo, remote="origin", timeout=TIMEOUT) == "https://github.com/abboskhanme/BonviHR.git"
    await sh(repo, "remote", "add", "mirror", "git@github.com:abboskhanme/BonviHR.git")
    assert await git.remote_url(repo, remote="mirror", timeout=TIMEOUT) == "git@github.com:abboskhanme/BonviHR.git"
    await sh(repo, "remote", "add", "ssh", "ssh://git@example.com:2222/x.git")
    assert await git.remote_url(repo, remote="ssh", timeout=TIMEOUT) == "ssh://git@example.com:2222/x.git"


async def test_tree_files_reads_a_directory_as_committed(git: GitCli, repo: Path) -> None:
    await commit_file(repo, ".github/workflows/deploy.yml", "on: push\n", "workflows")
    await commit_file(repo, ".github/workflows/same.yml", "on: push\n", "same content, one blob")
    await commit_file(repo, ".github/workflows/big.yml", "x" * 5000, "big")
    await commit_file(repo, ".github/workflows/nested/inner.yml", "on: push\n", "nested")
    (repo / ".github" / "workflows" / "link.yml").symlink_to("deploy.yml")
    await sh(repo, "add", "--", ".github/workflows/link.yml")
    await sh(repo, "commit", "-q", "-m", "symlink")
    sha = await sh(repo, "rev-parse", "HEAD")
    (repo / ".github" / "workflows" / "deploy.yml").write_text("changed in the working tree\n", encoding="utf-8")

    files = await git.tree_files(repo, sha, ".github/workflows/", max_bytes=1000, timeout=TIMEOUT)
    assert files == {"deploy.yml": b"on: push\n", "same.yml": b"on: push\n", "big.yml": None, "link.yml": None}
    assert await git.tree_files(repo, "HEAD~5", ".github/workflows", max_bytes=1000, timeout=TIMEOUT) == {}
    assert await git.tree_files(repo, sha, "docs", max_bytes=1000, timeout=TIMEOUT) == {}


# --- writes -----------------------------------------------------------------------------------------------------------
async def test_init_and_create_branch(git: GitCli, tmp_path: Path) -> None:
    target = tmp_path / "Yangi Loyiha"
    await git.init(target, branch="main", timeout=TIMEOUT)
    assert (target / ".git").is_dir()
    assert await git.current_branch(target, timeout=TIMEOUT) == "main"
    await sh(target, "config", "user.name", "Test")
    await sh(target, "config", "user.email", "test@example.com")
    await commit_file(target, "README.md", "# Yangi\n", "first")  # before a commit no branch exists to collide with
    await git.create_branch(target, "selfupdate/17-tts", timeout=TIMEOUT)
    assert await git.current_branch(target, timeout=TIMEOUT) == "selfupdate/17-tts"
    with pytest.raises(GitFailed):
        await git.create_branch(target, "main", timeout=TIMEOUT)


async def test_checkout_paths_restores_files_taken_literally(git: GitCli, repo: Path) -> None:
    base = await commit_file(repo, "*.txt", "star\n", "a file named like a glob")
    await commit_file(repo, "a.txt", "a\n", "a")
    (repo / "*.txt").write_text("broken\n", encoding="utf-8")
    (repo / "a.txt").write_text("broken\n", encoding="utf-8")
    await git.checkout_paths(repo, base, ["*.txt"], timeout=TIMEOUT)
    assert (repo / "*.txt").read_text(encoding="utf-8") == "star\n"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "broken\n"  # not matched by the "glob"
    await git.checkout_paths(repo, base, [], timeout=TIMEOUT)
    with pytest.raises(GitFailed) as missing:
        await git.checkout_paths(repo, base, ["a.txt"], timeout=TIMEOUT)  # did not exist at base
    assert missing.value.reason == "bad_ref"


async def test_revert_runs_newest_first_whatever_the_order(git: GitCli, repo: Path) -> None:
    await commit_file(repo, "app.py", "v1\n", "v1")
    second = await commit_file(repo, "app.py", "v2\n", "v2")
    third = await commit_file(repo, "app.py", "v3\n", "v3")
    await git.revert(repo, [second, third], timeout=TIMEOUT)  # oldest first on purpose
    assert (repo / "app.py").read_text(encoding="utf-8") == "v1\n"
    subjects = [item.subject for item in await git.commits_since(repo, third, timeout=TIMEOUT)]
    assert subjects == ['Revert "v2"', 'Revert "v3"']  # v3 was reverted first
    await git.revert(repo, [], timeout=TIMEOUT)


async def test_a_conflicting_revert_is_aborted_and_the_repo_left_as_it_was(git: GitCli, repo: Path) -> None:
    target = await commit_file(repo, "app.py", "v1\n", "v1")
    await commit_file(repo, "app.py", "v2 rewritten\n", "v2")
    before = await sh(repo, "rev-parse", "HEAD")
    with pytest.raises(GitFailed) as conflict:
        await git.revert(repo, [target], timeout=TIMEOUT)
    assert conflict.value.reason == "conflict"
    assert await sh(repo, "rev-parse", "HEAD") == before
    assert await sh(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "REVERT_HEAD").exists()


# --- errors -----------------------------------------------------------------------------------------------------------
async def test_not_a_repository(git: GitCli, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    for call in (git.status(plain, timeout=TIMEOUT), git.head(plain, timeout=TIMEOUT)):
        with pytest.raises(GitFailed) as failed:
            await call
        assert (failed.value.reason, failed.value.retryable) == ("not_a_repo", False)
    with pytest.raises(GitFailed) as gone:
        await git.status(tmp_path / "no such folder", timeout=TIMEOUT)
    assert gone.value.reason == "not_a_repo"


async def test_an_unknown_base_is_a_bad_ref(git: GitCli, repo: Path) -> None:
    with pytest.raises(GitFailed) as failed:
        await git.changed_files(repo, "deadbeef", timeout=TIMEOUT)
    assert failed.value.reason == "bad_ref"
    assert failed.value.params == {"operation": "git_changed_files"}


async def test_refs_that_look_like_options_are_refused(git: GitCli, repo: Path) -> None:
    with pytest.raises(ValueError, match="option"):
        await git.commits_since(repo, "--output=/tmp/x", timeout=TIMEOUT)
    with pytest.raises(ValueError, match="option"):
        await git.create_branch(repo, "-D", timeout=TIMEOUT)


async def test_a_held_index_lock_is_retried_then_reported_as_locked(git: GitCli, repo: Path) -> None:
    base = await sh(repo, "rev-parse", "HEAD")
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    with pytest.raises(GitFailed) as locked:
        await git.checkout_paths(repo, base, ["README.md"], timeout=TIMEOUT)
    assert (locked.value.reason, locked.value.retryable) == ("locked", True)


async def test_a_missing_binary_and_a_timeout(repo: Path) -> None:
    with pytest.raises(NotInstalled):
        await GitCli(SEARCH_PATH, binary="/nonexistent/git").status(repo, timeout=TIMEOUT)
    with pytest.raises(CallTimedOut):
        await GitCli(SEARCH_PATH).status(repo, timeout=0.000_001)


async def test_an_inherited_git_dir_is_ignored(
    git: GitCli, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = await new_repo(tmp_path / "other")
    await commit_file(other, "x.txt", "x\n", "elsewhere")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    found = await git.find_commit(repo, "HEAD", timeout=TIMEOUT)
    assert found is not None
    assert found.subject == "first commit"


# --- the registry on a real repository --------------------------------------------------------------------------------
async def test_the_registry_reads_a_real_repository_and_its_ci(git: GitCli, dev_deps: DevDeps, paths: Paths) -> None:
    """UC-13 AC on real git: CI deploys on push to main, so main asks and a feature branch does not — and the
    committed workflow still counts after it is deleted from the working tree."""
    root = projects_root_of(paths)
    shop = await new_repo(root / "Shop")
    workflow = (WORKFLOW_FIXTURES / "deploy_on_push_main.yml").read_text(encoding="utf-8")
    await commit_file(shop, ".github/workflows/deploy.yml", workflow, "ci: deploy on main")
    await sh(shop, "remote", "add", "origin", "https://owner:ghp_notreal@github.com/abboskhanme/Shop.git")
    deps = replace(dev_deps, git=git)

    report = await service.sync_projects(deps)
    assert report.added == ("Shop",)
    project = await service.get_project(deps, "Shop")
    assert (project.default_branch, project.git_remote_url) == ("main", "https://github.com/abboskhanme/Shop.git")
    assert project.deploy.covers_branch("main")
    assert not project.deploy.covers_branch("feature/cart")

    (shop / ".github" / "workflows" / "deploy.yml").unlink()  # not committed: the pushed commit still deploys
    fresh = await service.deploy_branches(deps, "Shop")
    assert fresh.covers_branch("main")
    assert not fresh.covers_branch("feature/cart")
