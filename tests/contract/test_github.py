from pathlib import Path

import pytest

from tests.fakes.git import FakeGitHub
from yordamchi.ports.github import GitHub


class GitHubContract:
    @pytest.fixture
    def github(self) -> GitHub:
        raise NotImplementedError

    async def test_auth_user_is_a_name_or_none(self, github: GitHub) -> None:
        user = await github.auth_user(timeout=10.0)
        assert user is None or user

    async def test_create_private_repo_returns_its_url(self, github: GitHub, tmp_path: Path) -> None:
        url = await github.create_private_repo("abboskhanme", "ScratchRepo", source=tmp_path, timeout=30.0)
        assert url.endswith("/ScratchRepo")


class TestFakeGitHub(GitHubContract):
    @pytest.fixture
    def github(self) -> GitHub:
        return FakeGitHub()
