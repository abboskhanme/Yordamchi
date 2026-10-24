"""`GhCli` against a fake `gh` on PATH (`tests/integration/gh_harness.py`), replaying `tests/fixtures/github/`: the
exact argv, the cut-down child environment, the parsing of `auth status`, `repo view` and `repo create`, the error
mapping for every `gh` failure (CONVENTIONS §7), retries of reads only, and logs that never hold a token or `gh`'s
output (T52). Nothing here reaches GitHub.
"""

import logging
from pathlib import Path

import pytest

from tests.factories import FAKE_KEYS
from tests.integration.gh_harness import FakeGh, fake_gh, fixture
from yordamchi.adapters.github.cli import GhCli
from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    GitHubFailed,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.core.redact import LOG_FIELDS_ATTR
from yordamchi.ports.github import GitHubRepo

LOGGER = "yordamchi.adapters.github.cli"
TIMEOUT = 10.0
OWNER = "abboskhanme"
NAME = "KitobBot"
AUTH_ARGV = ["auth", "status", "--active", "--hostname=github.com", "--json", "hosts"]
VIEW_ARGV = ["repo", "view", f"{OWNER}/{NAME}", "--json", "nameWithOwner,url,isPrivate,defaultBranchRef"]


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
def gh(tmp_path: Path) -> FakeGh:
    return fake_gh(tmp_path / "gh")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    folder = tmp_path / "Projects" / NAME
    folder.mkdir(parents=True)
    return folder


def _fields(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, object]]]:
    return [(record.getMessage(), dict(getattr(record, LOG_FIELDS_ATTR, {}))) for record in caplog.records]


# --- auth_user -------------------------------------------------------------------------------------------------------
async def test_auth_user_reads_the_active_login_from_the_json_status(gh: FakeGh) -> None:
    gh.reply("auth-status", stdout=fixture("auth_status_ok.json"))
    assert await gh.cli().auth_user(timeout=TIMEOUT) == OWNER
    assert gh.calls() == [AUTH_ARGV]


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code"),
    [
        ("auth_status_no_hosts.json", None, 0),
        (None, "auth_status_logged_out.stderr", 1),
    ],
)
async def test_nobody_logged_in_is_none_not_an_error(
    gh: FakeGh, stdout: str | None, stderr: str | None, exit_code: int
) -> None:
    gh.reply(
        "auth-status",
        stdout=fixture(stdout) if stdout else b"",
        stderr=fixture(stderr) if stderr else b"",
        exit_code=exit_code,
    )
    assert await gh.cli().auth_user(timeout=TIMEOUT) is None


async def test_a_refused_token_is_auth_failed_and_not_retried(gh: FakeGh) -> None:
    gh.reply("auth-status", stdout=fixture("auth_status_bad_token.json"))
    with pytest.raises(AuthFailed) as caught:
        await gh.cli().auth_user(timeout=TIMEOUT)
    assert caught.value.params == {"provider": "GitHub"}
    assert len(gh.calls()) == 1


@pytest.mark.parametrize("name", ["auth_status_offline.json", "auth_status_timeout.json"])
async def test_a_token_check_that_cannot_reach_github_is_unavailable_and_retried(gh: FakeGh, name: str) -> None:
    gh.reply("auth-status", stdout=fixture(name))
    with pytest.raises(ProviderUnavailable):
        await gh.cli().auth_user(timeout=TIMEOUT)
    assert len(gh.calls()) == retry.DEFAULT_ATTEMPTS


# --- view_repo -------------------------------------------------------------------------------------------------------
async def test_view_repo_reads_privacy_url_and_default_branch(gh: FakeGh) -> None:
    gh.reply("repo-view", stdout=fixture("repo_view_private.json"))
    repo = await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT)
    assert repo == GitHubRepo(
        owner=OWNER, name=NAME, url=f"https://github.com/{OWNER}/{NAME}", private=True, default_branch="main"
    )
    assert gh.calls() == [VIEW_ARGV]


async def test_github_spelling_of_the_names_wins_and_an_empty_repo_has_no_default_branch(gh: FakeGh) -> None:
    gh.reply("repo-view", stdout=fixture("repo_view_empty.json"))
    repo = await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT)
    assert repo is not None
    assert (repo.owner, repo.name, repo.default_branch, repo.private) == ("AbboskhanMe", "kitobbot", None, True)


async def test_a_public_repo_reads_as_public(gh: FakeGh) -> None:
    gh.reply("repo-view", stdout=fixture("repo_view_public.json"))
    repo = await gh.cli().view_repo("cli", "cli", timeout=TIMEOUT)
    assert repo is not None
    assert repo.private is False


async def test_privacy_left_out_of_the_answer_counts_as_public(gh: FakeGh) -> None:
    gh.reply("repo-view", stdout=b'{"nameWithOwner":"abboskhanme/KitobBot"}\n')
    repo = await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT)
    assert repo is not None
    assert repo.private is False
    assert repo.url == f"https://github.com/{OWNER}/{NAME}"


async def test_a_repository_that_does_not_exist_is_none(gh: FakeGh) -> None:
    gh.fail("repo-view", "repo_view_not_found.stderr")
    assert await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT) is None
    assert len(gh.calls()) == 1


async def test_a_read_that_fails_on_the_network_is_retried_within_its_timeout(gh: FakeGh) -> None:
    gh.fail("repo-view", "network.stderr", attempt=1)
    gh.reply("repo-view", stdout=fixture("repo_view_private.json"), attempt=2)
    repo = await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT)
    assert repo is not None
    assert len(gh.calls()) == 2


@pytest.mark.parametrize(
    ("stderr", "exit_code", "error_class"),
    [
        ("bad_credentials.stderr", 1, AuthFailed),
        ("auth_required.stderr", 4, AuthFailed),
        ("unrecognised.stderr", 4, AuthFailed),  # exit 4 is gh's "needs a login", whatever it says
        ("rate_limited.stderr", 1, RateLimited),  # an HTTP 403 that is a rate limit, not a refusal
        ("network.stderr", 1, ProviderUnavailable),
        ("server_error.stderr", 1, ProviderUnavailable),
    ],
)
async def test_gh_failures_map_to_the_error_hierarchy(
    gh: FakeGh, stderr: str, exit_code: int, error_class: type[ExternalError]
) -> None:
    gh.fail("repo-view", stderr, exit_code=exit_code)
    with pytest.raises(error_class) as caught:
        await gh.cli().view_repo(OWNER, NAME, timeout=TIMEOUT)
    assert caught.value.params.get("provider") == "GitHub"
    expected_calls = retry.DEFAULT_ATTEMPTS if caught.value.retryable else 1
    assert len(gh.calls()) == expected_calls


# --- create_repo -----------------------------------------------------------------------------------------------------
async def test_create_repo_is_private_sets_origin_and_pushes_nothing(gh: FakeGh, source: Path) -> None:
    gh.reply("repo-create", stdout=fixture("repo_create_ok.stdout"))
    repo = await gh.cli().create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    assert repo == GitHubRepo(owner=OWNER, name=NAME, url=f"https://github.com/{OWNER}/{NAME}", private=True)
    [argv] = gh.calls()
    assert argv == ["repo", "create", f"{OWNER}/{NAME}", "--private", f"--source={source}", "--remote=origin"]
    assert "--push" not in argv
    assert "--public" not in argv


async def test_a_public_repo_is_only_ever_an_explicit_request(gh: FakeGh, source: Path) -> None:
    gh.reply("repo-create", stdout=fixture("repo_create_ok.stdout"))
    repo = await gh.cli().create_repo(OWNER, NAME, source=source, private=False, timeout=TIMEOUT)
    assert repo.private is False
    [argv] = gh.calls()
    assert "--public" in argv
    assert "--private" not in argv


async def test_a_create_without_a_printed_url_builds_it(gh: FakeGh, source: Path) -> None:
    repo = await gh.cli().create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    assert repo.url == f"https://github.com/{OWNER}/{NAME}"


async def test_a_create_is_never_retried(gh: FakeGh, source: Path) -> None:
    gh.fail("repo-create", "network.stderr")
    with pytest.raises(ProviderUnavailable):
        await gh.cli().create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    assert len(gh.calls()) == 1


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("repo_create_name_taken.stderr", "name_taken"),
        ("repo_create_not_a_repo.stderr", "not_a_repo"),
        ("repo_create_remote_exists.stderr", "remote_exists"),
        ("forbidden.stderr", "forbidden"),
        ("unrecognised.stderr", "failed"),
    ],
)
async def test_a_refused_create_is_github_failed_with_its_reason(
    gh: FakeGh, source: Path, stderr: str, reason: str
) -> None:
    gh.fail("repo-create", stderr)
    with pytest.raises(GitHubFailed) as caught:
        await gh.cli().create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    assert caught.value.reason == reason
    assert caught.value.retryable is False
    assert caught.value.params == {"operation": "gh_repo_create"}


@pytest.mark.parametrize(
    ("owner", "name"),
    [("-bad", NAME), (OWNER, "--public"), (OWNER, "a/b"), (OWNER, ".."), ("", NAME), (OWNER, ""), (OWNER, "a b")],
)
async def test_names_github_would_refuse_never_reach_gh(gh: FakeGh, source: Path, owner: str, name: str) -> None:
    with pytest.raises(ValueError, match="GitHub"):
        await gh.cli().view_repo(owner, name, timeout=TIMEOUT)
    with pytest.raises(ValueError, match="GitHub"):
        await gh.cli().create_repo(owner, name, source=source, timeout=TIMEOUT)
    assert gh.calls() == []


async def test_a_relative_source_is_refused(gh: FakeGh) -> None:
    with pytest.raises(ValueError, match="absolute"):
        await gh.cli().create_repo(OWNER, NAME, source=Path("Projects/KitobBot"), timeout=TIMEOUT)
    assert gh.calls() == []


# --- the process -----------------------------------------------------------------------------------------------------
async def test_a_missing_binary_is_not_installed(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    with pytest.raises(NotInstalled) as caught:
        await GhCli(str(empty)).auth_user(timeout=TIMEOUT)
    assert caught.value.params == {"binary": "gh"}


async def test_a_call_longer_than_its_timeout_is_call_timed_out(gh: FakeGh, source: Path) -> None:
    gh.reply("repo-create", sleep_s=5)
    with pytest.raises(CallTimedOut) as caught:
        await gh.cli().create_repo(OWNER, NAME, source=source, timeout=0.3)
    assert caught.value.params == {"operation": "gh_repo_create"}


async def test_the_child_never_inherits_another_token_host_or_repository(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GIT_DIR"):
        monkeypatch.setenv(name, "inherited")
    gh.reply("auth-status", stdout=fixture("auth_status_ok.json"))
    await gh.cli().auth_user(timeout=TIMEOUT)
    env = gh.env()
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GIT_DIR"} & set(env)
    assert env["PATH"] == gh.path
    assert (env["GH_PROMPT_DISABLED"], env["GH_NO_UPDATE_NOTIFIER"], env["GIT_TERMINAL_PROMPT"]) == ("1", "1", "0")


async def test_no_command_ever_asks_gh_for_a_token(gh: FakeGh, source: Path) -> None:
    gh.reply("auth-status", stdout=fixture("auth_status_ok.json"))
    gh.reply("repo-view", stdout=fixture("repo_view_private.json"))
    cli = gh.cli()
    await cli.auth_user(timeout=TIMEOUT)
    await cli.view_repo(OWNER, NAME, timeout=TIMEOUT)
    await cli.create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    for argv in gh.calls():
        assert "--show-token" not in argv
        assert "-t" not in argv
        assert argv[:2] != ["auth", "token"]


async def test_logs_and_errors_never_hold_gh_output_or_a_token(
    gh: FakeGh, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = FAKE_KEYS["github_token"]
    gh.reply("repo-create", stderr=f"HTTP 401: Bad credentials\nused token {token}\n".encode(), exit_code=1)
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(AuthFailed) as caught:
        await gh.cli().create_repo(OWNER, NAME, source=source, timeout=TIMEOUT)
    [(event, fields)] = _fields(caplog)
    assert (event, fields) == ("github_failed", {"operation": "gh_repo_create", "reason": "auth", "returncode": 1})
    assert token not in caplog.text
    assert token not in f"{caught.value} {caught.value!r} {caught.value.params}"
    assert "Bad credentials" not in caplog.text
