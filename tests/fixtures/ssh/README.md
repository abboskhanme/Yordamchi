# SSH fixtures

Used by `tests/integration/test_ssh.py` and `tests/contract/test_remote_shell.py` through
`tests/integration/ssh_harness.py`, whose fake `ssh` replays them. No test, and no recording here, ever connected
to a real host.

OpenSSH writes its own messages to stderr with CRLF line ends, so every `.stderr` file keeps `\r\n`. When ssh itself
fails it exits 255; every other status is the remote command's.

## Recorded on the owner's Mac, 2026-10-24 (OpenSSH_10.3p1, LibreSSL 3.3.6), without network

| File | Command | Notes |
|---|---|---|
| `connection_refused.stderr` | `ssh -F <tmp config> -T -o BatchMode=yes … -- bonvi-srv uptime`, the alias pointing at `127.0.0.1` port 1 | loopback only; exit 255 |
| `ssh_g_configured.stdout` | `ssh -F <tmp config> -G -- bonvi-srv` | config only, no connection: `hostname 127.0.0.1` differs from the alias |
| `ssh_g_unconfigured.stdout` | `ssh -F <tmp config> -G -- no-such-alias` | `hostname no-such-alias`: the alias is not defined; the user name is replaced by `owner` |

## Synthetic: the OpenSSH 10.x client's messages, verbatim in form, with made-up hosts

| File | Reason the adapter logs | Error |
|---|---|---|
| `resolve_failed.stderr` (macOS), `resolve_failed_linux.stderr` | `host_unknown` / `dns_failed`, settled by `ssh -G` | `ProviderUnavailable`, retryable only for `dns_failed` |
| `permission_denied.stderr`, `too_many_auth_failures.stderr` | `auth_refused` | `AuthFailed` |
| `host_key_unknown.stderr`, `host_key_changed.stderr` | `host_key_rejected` | `AuthFailed` |
| `proxyjump_refused.stderr` | `connection_refused` (the jump's cause wins over "Connection closed") | `ProviderUnavailable`, retryable |
| `connect_timed_out.stderr`, `banner_timed_out.stderr` | `connect_timed_out` | `CallTimedOut`, retryable |
| `no_route_to_host.stderr`, `network_unreachable.stderr` | `unreachable` | `ProviderUnavailable`, retryable |
| `kex_connection_closed.stderr` | `connection_lost` | `ProviderUnavailable`, retryable |
| `negotiation_failed.stderr` | `negotiation_failed` | `ProviderUnavailable` |
| `bad_config_permissions.stderr` | `config_invalid` | `ProviderUnavailable` |
| `unrecognised.stderr` | `unknown` | `ProviderUnavailable` |

Remote output, in Ubuntu 24.04 form: `uptime.stdout`, `df_h.stdout`, `systemctl_inactive.stdout` (exit 3). The
adapter passes it through untouched.
