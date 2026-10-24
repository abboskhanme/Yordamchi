"""The `GitHub` contract: the fake, `GhCli` on a fake `gh` (offline, replaying `tests/fixtures/github/`), and `GhCli`
on the real `gh` (`pytest -m live`, reads only).

The live class never creates a repository. The owner approved exactly one live repository,
`abboskhanme/yordamchi-smoke`, and it belongs to T39; creating is covered by the fake and the offline adapter.
"""

from pathlib import Path

import pytest

from tests.fakes.git import FakeGitHub
from tests.integration.gh_harness import FakeGh, fake_gh, fixture
from yordamchi.adapters.github.cli import GhCli
from yordamchi.core import retry
from yordamchi.core.errors import GitHubFailed
from yordamchi.ports.github import GitHub

TIMEOUT = 15.0
OWNER = "abboskhanme"
MISSING = "yordamchi-contract-missing-repo"  # no repository of the account has this name
NEW = "KitobBot"
LIVE_GH = Path.home() / ".local" / "bin" / "gh"
LIVE_SEARCH_PATH = f"{LIVE_GH.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


class GitHubReadContract:
    @pytest.fixture
    def github(self) -> GitHub:
        raise NotImplementedError

    async def test_auth_user_is_a_login_or_none(self, github: GitHub) -> None:
        user = await github.auth_user(timeout=TIMEOUT)
        assert user is None or user.strip() == user != ""

    async def test_a_repository_that_does_not_exist_is_none(self, github: GitHub) -> None:
        owner = await github.auth_user(timeout=TIMEOUT) or OWNER
        assert await github.view_repo(owner, MISSING, timeout=TIMEOUT) is None


class GitHubCreateContract:
    """Run against an implementation whose `OWNER/NEW` does not exist yet."""

    @pytest.fixture
    def github(self) -> GitHub:
        raise NotImplementedError

    async def test_a_new_repository_is_private_by_default(self, github: GitHub, tmp_path: Path) -> None:
        repo = await github.create_repo(OWNER, NEW, source=tmp_path, timeout=TIMEOUT)
        assert (repo.owner, repo.name, repo.private) == (OWNER, NEW, True)
        assert repo.url == f"https://github.com/{OWNER}/{NEW}"

    async def test_a_name_that_is_taken_cannot_be_created_again(self, github: GitHub, tmp_path: Path) -> None:
        await github.create_repo(OWNER, NEW, source=tmp_path, timeout=TIMEOUT)
        with pytest.raises(GitHubFailed) as caught:
            await github.create_repo(OWNER, NEW, source=tmp_path, timeout=TIMEOUT)
        assert caught.value.reason == "name_taken"
        assert caught.value.retryable is False


# --- the fake ---------------------------------------------------------------------------------------------------------
class TestFakeGitHub(GitHubReadContract, GitHubCreateContract):
    @pytest.fixture
    def github(self) -> GitHub:
        return FakeGitHub()

    async def test_a_created_repository_is_visible_and_names_compare_case_insensitively(self, tmp_path: Path) -> None:
        github = FakeGitHub()
        await github.create_repo(OWNER, NEW, source=tmp_path, timeout=TIMEOUT)
        viewed = await github.view_repo(OWNER.upper(), NEW.lower(), timeout=TIMEOUT)
        assert viewed is not None
        assert viewed.private
        assert github.created == [(OWNER, NEW, tmp_path, True)]

    async def test_scripted_failures_are_raised_once(self) -> None:
        github = FakeGitHub(user=None)
        github.failures["view_repo"] = GitHubFailed(operation="gh_repo_view", reason="forbidden")
        assert await github.auth_user(timeout=TIMEOUT) is None
        with pytest.raises(GitHubFailed):
            await github.view_repo(OWNER, NEW, timeout=TIMEOUT)
        assert await github.view_repo(OWNER, NEW, timeout=TIMEOUT) is None
        assert [call[0] for call in github.calls] == ["auth_user", "view_repo", "view_repo"]


# --- the adapter on a fake gh (offline) -------------------------------------------------------------------------------
class TestGhCli(GitHubReadContract, GitHubCreateContract):
    @pytest.fixture(autouse=True)
    def _instant_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)

    @pytest.fixture
    def gh(self, tmp_path: Path) -> FakeGh:
        gh = fake_gh(tmp_path / "gh")
        gh.reply("auth-status", stdout=fixture("auth_status_ok.json"))
        gh.fail("repo-view", "repo_view_not_found.stderr")
        gh.reply("repo-create", stdout=fixture("repo_create_ok.stdout"), attempt=1)
        gh.fail("repo-create", "repo_create_name_taken.stderr", attempt=2)
        return gh

    @pytest.fixture
    def github(self, gh: FakeGh) -> GitHub:
        return gh.cli()


# --- the adapter on the real gh (live, reads only) --------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.skipif(not LIVE_GH.exists(), reason="gh not installed at ~/.local/bin/gh")
class TestGhCliLive(GitHubReadContract):
    """`pytest -m live tests/contract/test_github.py`: the logged-in account and one read of a missing repository.
    Nothing is created, changed or deleted."""

    @pytest.fixture
    def github(self) -> GitHub:
        return GhCli(LIVE_SEARCH_PATH)
