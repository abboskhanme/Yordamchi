from pathlib import Path

import pytest

from tests.fakes.git import FakeGit
from yordamchi.ports.git import Git, GitStatus

TIMEOUT = 10.0


class GitContract:
    """Run against a fresh repository (`repo` fixture). The real adapter (T17) subclasses this with `git init`."""

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
