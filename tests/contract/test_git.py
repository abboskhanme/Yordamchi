from pathlib import Path

import pytest

from tests.fakes.git import FakeGit
from yordamchi.adapters.git.cli import GitCli
from yordamchi.ports.git import Git, GitStatus

TIMEOUT = 10.0
SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


class GitContract:
    """Run against a fresh repository on branch `main` (`repo` fixture). The real adapter subclasses this with
    `git init`."""

    @pytest.fixture
    def git(self) -> Git:
        raise NotImplementedError

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return tmp_path

    async def test_a_fresh_repo_has_no_head_and_a_clean_status(self, git: Git, repo: Path) -> None:
        assert await git.head(repo, timeout=TIMEOUT) is None
        status = await git.status(repo, timeout=TIMEOUT)
        assert isinstance(status, GitStatus)
        assert (status.changed, status.untracked, status.ahead, status.behind) == (0, 0, 0, 0)

    async def test_ignored_returns_a_subset_of_the_paths_asked(self, git: Git, repo: Path) -> None:
        asked = ["a.txt", "b.txt"]
        assert await git.ignored(repo, asked, timeout=TIMEOUT) <= set(asked)

    async def test_a_fresh_repo_is_on_main_with_no_commit_history_or_files(self, git: Git, repo: Path) -> None:
        assert await git.current_branch(repo, timeout=TIMEOUT) == "main"
        assert await git.find_commit(repo, "HEAD", timeout=TIMEOUT) is None
        assert await git.log(repo, since_days=7, limit=10, timeout=TIMEOUT) == []
        assert await git.tree_files(repo, "HEAD", ".github/workflows", max_bytes=1024, timeout=TIMEOUT) == {}
        assert await git.remote_url(repo, remote="origin", timeout=TIMEOUT) is None


class TestFakeGit(GitContract):
    @pytest.fixture
    def git(self) -> Git:
        return FakeGit()

    async def test_scripted_facts_and_failures(self, tmp_path: Path) -> None:
        git = FakeGit()
        git.heads[tmp_path] = "a1b2c3d"
        git.ignored_paths[tmp_path] = {"build/out.js"}
        git.failures["status"] = RuntimeError("git failed")
        assert await git.head(tmp_path, timeout=TIMEOUT) == "a1b2c3d"
        assert await git.ignored(tmp_path, ["build/out.js", "src/x.py"], timeout=TIMEOUT) == {"build/out.js"}
        with pytest.raises(RuntimeError):
            await git.status(tmp_path, timeout=TIMEOUT)
        assert [call[0] for call in git.calls] == ["head", "ignored", "status"]

    async def test_scripted_trees_respect_the_size_limit(self, tmp_path: Path) -> None:
        git = FakeGit()
        git.trees[(tmp_path, "HEAD", ".github/workflows")] = {"a.yml": b"on: push\n", "big.yml": b"x" * 50}
        files = await git.tree_files(tmp_path, "HEAD", ".github/workflows/", max_bytes=10, timeout=TIMEOUT)
        assert files == {"a.yml": b"on: push\n", "big.yml": None}


class TestGitCli(GitContract):
    """The real adapter on a real, empty repository (local only: no network, so not `live`)."""

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    @pytest.fixture
    def git(self) -> Git:
        return GitCli(SEARCH_PATH)

    @pytest.fixture
    async def repo(self, tmp_path: Path) -> Path:
        path = tmp_path / "repo"
        await GitCli(SEARCH_PATH).init(path, branch="main", timeout=TIMEOUT)
        return path
