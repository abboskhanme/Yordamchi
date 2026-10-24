"""`GhCli`: the `GitHub` port over the `gh` binary (CONVENTIONS §4, §6, §7; SPEC §3.2.8; NFR-SEC-11; T52).

Every call runs `gh` through `core.proc`: an argv list (never a shell), its own process group, the call's `timeout`,
and the environment of `proc.clean_env()` with these changes:

- no inherited `GH_*`, `GITHUB_*` or `GIT_*` variable: a stray `GH_TOKEN` or `GH_HOST` would act as another account or
  host than the owner's `gh auth login`, and a hook's `GIT_DIR` would point `--source` at another repository;
- `GH_PROMPT_DISABLED=1`, no update notices, no spinner, no colour, `GH_PAGER=cat`, `GIT_TERMINAL_PROMPT=0` and
  `LC_ALL=C`: `gh` never waits for a person, and its messages stay stable for the mapping below.

**Tokens.** Nothing here asks `gh` for a token: no `gh auth token`, no `--show-token`. The login is read with
`gh auth status --json hosts`, whose JSON has no token field. stdout is parsed and dropped; stderr only picks a
reason and is then dropped. Neither reaches a log line or an exception.

**Repositories.** `create_repo` passes `--private` unless the caller asks otherwise (no caller does), sets `origin`
on the local repository and pushes nothing. It is never retried: after a lost response a second create would fail with
"name already exists" or, worse, race the first. Reads (`auth_user`, `view_repo`) retry `retryable` errors through
`core.retry`, inside the same timeout. An owner or repository name that GitHub would refuse, or one that looks like an
option, raises `ValueError` before `gh` runs: names come from Yordamchi's own rows, so a bad one is a bug.

Errors: the binary missing → `NotInstalled(gh)`; the timeout → `CallTimedOut(gh_<op>)`; not logged in or a refused
login (exit 4, "gh auth login", HTTP 401) → `AuthFailed(GitHub)`; a rate limit → `RateLimited(GitHub)`; no network or
GitHub 5xx → `ProviderUnavailable(GitHub)`; anything else → `GitHubFailed(gh_<op>, reason)`.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from yordamchi.core import proc, retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    GitHubFailed,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.core.log import get_log
from yordamchi.ports.github import GitHubRepo

log = get_log(__name__)

BINARY: Final = "gh"
HOST: Final = "github.com"
REMOTE: Final = "origin"
PROVIDER: Final = "GitHub"  # the name the owner reads in `S.ERRORS` ("GitHub hozir javob bermayapti")
AUTH_EXIT: Final = 4  # `gh` exits 4 when a command needs a login it does not have

_ENV: Final[Mapping[str, str]] = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
    "GH_SPINNER_DISABLED": "1",
    "GH_PAGER": "cat",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
_DROPPED_PREFIXES: Final = ("GH_", "GITHUB_", "GIT_")
_VIEW_FIELDS: Final = "nameWithOwner,url,isPrivate,defaultBranchRef"
# GitHub's own rules: an account is 1–39 letters, digits or inner hyphens; a repository 1–100 of `[A-Za-z0-9._-]`.
_OWNER: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_NAME: Final = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}")
_URL: Final = re.compile(r"https://\S+")

# What a failure is, by `gh`'s message (lowercased), first match wins. A rate limit is an HTTP 403 too, so it comes
# before `forbidden`; the network kinds come after the reasons that name a repository.
_AUTH: Final = "auth"
_RATE: Final = "rate"
_DOWN: Final = "down"
_LOGGED_OUT: Final = "logged_out"
_REASONS: Final = (
    ("rate limit", _RATE),
    ("http 429", _RATE),
    ("not logged in", _LOGGED_OUT),
    ("not logged into", _LOGGED_OUT),
    ("gh auth login", _AUTH),
    ("authentication required", _AUTH),
    ("bad credentials", _AUTH),
    ("http 401", _AUTH),
    ("missing required scope", _AUTH),
    ("could not resolve to a repository", "not_found"),
    ("name already exists", "name_taken"),
    ("not a git repository", "not_a_repo"),
    ("unable to add remote", "remote_exists"),
    ("remote origin already exists", "remote_exists"),
    ("error connecting to", _DOWN),
    ("could not resolve host", _DOWN),
    ("no such host", _DOWN),
    ("connection refused", _DOWN),
    ("connection reset", _DOWN),
    ("i/o timeout", _DOWN),
    ("tls handshake", _DOWN),
    ("network is unreachable", _DOWN),
    ("http 500", _DOWN),
    ("http 502", _DOWN),
    ("http 503", _DOWN),
    ("http 504", _DOWN),
    ("http 403", "forbidden"),
    ("resource not accessible", "forbidden"),
)


def _reason(text: bytes | str) -> str:
    folded = (text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text).lower()
    return next((reason for needle, reason in _REASONS if needle in folded), "failed")


def _error(operation: str, reason: str) -> ExternalError:
    if reason in {_AUTH, _LOGGED_OUT}:
        return AuthFailed(provider=PROVIDER)
    if reason == _RATE:
        return RateLimited(provider=PROVIDER)
    if reason == _DOWN:
        return ProviderUnavailable(provider=PROVIDER)
    return GitHubFailed(operation=operation, reason=reason)


def _checked_owner(owner: str) -> str:
    if not _OWNER.fullmatch(owner):
        msg = "refusing a GitHub owner that GitHub would not accept"
        raise ValueError(msg)
    return owner


def _checked_name(name: str) -> str:
    if not _NAME.fullmatch(name) or name in {".", ".."}:
        msg = "refusing a GitHub repository name that GitHub would not accept"
        raise ValueError(msg)
    return name


def _object(data: bytes) -> dict[str, object]:
    try:
        parsed: object = json.loads(data)
    except ValueError:
        return {}
    return {str(key): value for key, value in parsed.items()} if isinstance(parsed, dict) else {}


def _active_login(data: bytes, host: str) -> str | None:
    """`gh auth status --active --hostname <host> --json hosts`: `{"hosts": {host: [{active, login, state, error}]}}`.
    The active account whose token checked out → its login. A check that timed out or could not reach GitHub →
    `ProviderUnavailable`; one that was refused → `AuthFailed`. No account → None."""
    hosts = _object(data).get("hosts")
    entries = hosts.get(host) if isinstance(hosts, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("active") is False:
            continue
        login, state = entry.get("login"), entry.get("state")
        if state == "success" and isinstance(login, str) and _OWNER.fullmatch(login):
            return login
        if state == "timeout":
            raise ProviderUnavailable(provider=PROVIDER)
        detail = entry.get("error")
        reason = _reason(detail) if isinstance(detail, str) else _AUTH
        raise ProviderUnavailable(provider=PROVIDER) if reason == _DOWN else AuthFailed(provider=PROVIDER)
    return None


def _repo(data: bytes, *, owner: str, name: str, host: str) -> GitHubRepo:
    """`gh repo view --json nameWithOwner,url,isPrivate,defaultBranchRef`. GitHub's own spelling of the names wins; a
    field it left out falls back to what was asked, and privacy unknown counts as public (the safe reading)."""
    fields = _object(data)
    full = fields.get("nameWithOwner")
    if isinstance(full, str) and full.count("/") == 1:
        owner, name = full.split("/")
    url = fields.get("url")
    branch = fields.get("defaultBranchRef")
    branch_name = branch.get("name") if isinstance(branch, dict) else None
    return GitHubRepo(
        owner=owner,
        name=name,
        url=url if isinstance(url, str) and url.startswith("https://") else f"https://{host}/{owner}/{name}",
        private=fields.get("isPrivate") is True,
        default_branch=branch_name if isinstance(branch_name, str) and branch_name else None,
    )


def _created_url(stdout: bytes) -> str | None:
    """`gh repo create` prints the new repository's URL (non-TTY: alone on its line)."""
    found = _URL.findall(stdout.decode("utf-8", errors="replace"))
    return found[-1].rstrip(".") if found else None


class GhCli:
    """`GitHub` port adapter. `search_path` is the child PATH built at startup (`proc.build_search_path`), which puts
    `~/.local/bin` (where `gh` lives) first."""

    def __init__(self, search_path: str, *, binary: str = BINARY, host: str = HOST) -> None:
        self._search_path = search_path
        self._binary = binary
        self._host = host

    def _env(self) -> dict[str, str]:
        env = proc.clean_env(path=self._search_path, extra=_ENV)
        return {name: value for name, value in env.items() if not name.startswith(_DROPPED_PREFIXES) or name in _ENV}

    async def _attempt(self, operation: str, args: tuple[str, ...], *, timeout: float) -> tuple[bytes, str | None]:
        """One `gh` run: (stdout, None) on exit 0, else (stdout, the failure's reason); an exit code never raises."""
        try:
            result = await proc.run([self._binary, *args], env=self._env(), timeout=timeout)
        except TimeoutError:
            raise CallTimedOut(operation=operation) from None
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            raise NotInstalled(binary=BINARY) from None
        except OSError:
            raise GitHubFailed(operation=operation, reason="failed") from None
        if result.returncode == 0:
            return result.stdout, None
        reason = _AUTH if result.returncode == AUTH_EXIT else _reason(result.stderr)
        log.warning("github_failed", operation=operation, reason=reason, returncode=result.returncode)
        return result.stdout, reason

    async def auth_user(self, *, timeout: float) -> str | None:
        operation = "gh_auth_status"
        args = ("auth", "status", "--active", f"--hostname={self._host}", "--json", "hosts")

        async def attempt() -> str | None:
            stdout, reason = await self._attempt(operation, args, timeout=timeout)
            if reason == _LOGGED_OUT:
                return None
            if reason is not None:
                raise _error(operation, reason)
            return _active_login(stdout, self._host)

        return await retry.retrying(attempt, timeout=timeout, operation=operation)

    async def view_repo(self, owner: str, name: str, *, timeout: float) -> GitHubRepo | None:
        operation = "gh_repo_view"
        full = f"{_checked_owner(owner)}/{_checked_name(name)}"
        args = ("repo", "view", full, "--json", _VIEW_FIELDS)

        async def attempt() -> GitHubRepo | None:
            stdout, reason = await self._attempt(operation, args, timeout=timeout)
            if reason == "not_found":
                return None
            if reason is not None:
                raise _error(operation, reason)
            return _repo(stdout, owner=owner, name=name, host=self._host)

        return await retry.retrying(attempt, timeout=timeout, operation=operation)

    async def create_repo(
        self, owner: str, name: str, *, source: Path, private: bool = True, timeout: float
    ) -> GitHubRepo:
        if not source.is_absolute():
            msg = "the source of a new repository must be an absolute path"
            raise ValueError(msg)
        full = f"{_checked_owner(owner)}/{_checked_name(name)}"
        visibility = "--private" if private else "--public"
        args = ("repo", "create", full, visibility, f"--source={source}", f"--remote={REMOTE}")
        operation = "gh_repo_create"
        stdout, reason = await self._attempt(operation, args, timeout=timeout)  # once: a create is not idempotent
        if reason is not None:
            raise _error(operation, reason)
        url = _created_url(stdout) or f"https://{self._host}/{owner}/{name}"
        log.info("github_repo_created", private=private)
        return GitHubRepo(owner=owner, name=name, url=url, private=private)
