# GitHub (`gh`) fixtures

Used by `tests/integration/test_github.py` and `tests/contract/test_github.py` through
`tests/integration/gh_harness.py`, whose fake `gh` replays them. No offline test reaches GitHub, and no recording
here created, changed or deleted anything: only read-only `gh` commands were run to record them.

`gh` writes its own messages to stderr with LF line ends and exits 1 on most failures, 4 when a command needs a login.
No file holds a token: `gh auth status --json hosts` has no token field, and the tests that check a token never
reaches a log build the token in code (`tests.factories.FAKE_KEYS`), so the pre-commit secret scan stays quiet.

## Recorded on the owner's Mac, 2026-10-24 (gh 2.101.0), read-only

| File | Command | Notes |
|---|---|---|
| `auth_status_ok.json` | `gh auth status --active --hostname=github.com --json hosts` | exit 0; the JSON carries no token |
| `repo_view_not_found.stderr` | `gh repo view abboskhanme/yordamchi-nonexistent-probe-t52 --json …` | exit 1 |
| `repo_view_public.json` | `gh repo view cli/cli --json nameWithOwner,url,isPrivate,defaultBranchRef` | exit 0; a public repo |

## Synthetic: `gh` 2.x messages, verbatim in form, with made-up names

| File | Reason the adapter logs | Error |
|---|---|---|
| `auth_status_no_hosts.json`, `auth_status_logged_out.stderr` | `logged_out` | `auth_user` → None |
| `auth_status_bad_token.json`, `bad_credentials.stderr`, `auth_required.stderr` (exit 4) | `auth` | `AuthFailed(GitHub)` |
| `auth_status_offline.json`, `auth_status_timeout.json`, `network.stderr`, `server_error.stderr` | `down` | `ProviderUnavailable(GitHub)`, retryable |
| `rate_limited.stderr` (an HTTP 403) | `rate` | `RateLimited(GitHub)`, retryable |
| `repo_view_private.json`, `repo_view_empty.json` | — | a private repo; one with no commit yet (`defaultBranchRef.name` empty) |
| `repo_create_ok.stdout` | — | the URL `gh repo create` prints when stdout is not a terminal |
| `repo_create_name_taken.stderr` | `name_taken` | `GitHubFailed` |
| `repo_create_not_a_repo.stderr` | `not_a_repo` | `GitHubFailed` |
| `repo_create_remote_exists.stderr` | `remote_exists` | `GitHubFailed` |
| `forbidden.stderr` | `forbidden` | `GitHubFailed` |
| `unrecognised.stderr` | `failed` | `GitHubFailed` |
