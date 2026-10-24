"""OpenSSH client failures → the `ExternalError` hierarchy (CONVENTIONS §7).

`ssh` exits with the remote command's status, or with 255 when ssh itself failed (ssh(1), EXIT STATUS). So only a
255 is an error here; any other status, non-zero included, is the remote command's answer. A 255 is classified by the
OpenSSH client's own messages on stderr (OpenSSH 10.3p1; samples in `tests/fixtures/ssh/`), first match wins.

`resolve_failed` is not final: macOS says "nodename nor servname provided, or not known" both for an alias that
`~/.ssh/config` does not define and for a configured host while DNS is down. `shell.py` asks `ssh -G` (config only,
no network) and settles it as `host_unknown` or `dns_failed`.

Interim mapping until `core/errors.py` has SSH codes (pending wiring): `provider` is the alias, so the owner reads
"bonvi-srv hozir javob bermayapti" or "bonvi-srv: kalit yoki login rad etildi". The snake_case `reason` is for logs.
Stderr itself is never copied into an error or a log: a login banner or a remote error can echo anything.
"""

import re
from typing import Final

from yordamchi.core.errors import AuthFailed, CallTimedOut, ExternalError, ProviderUnavailable

OPERATION: Final = "ssh"
SSH_FAILED_EXIT: Final = 255

RESOLVE_FAILED: Final = "resolve_failed"
HOST_UNKNOWN: Final = "host_unknown"
DNS_FAILED: Final = "dns_failed"
HOST_KEY_REJECTED: Final = "host_key_rejected"
AUTH_REFUSED: Final = "auth_refused"
CONNECTION_REFUSED: Final = "connection_refused"
CONNECT_TIMED_OUT: Final = "connect_timed_out"
UNREACHABLE: Final = "unreachable"
CONNECTION_LOST: Final = "connection_lost"
NEGOTIATION_FAILED: Final = "negotiation_failed"
CONFIG_INVALID: Final = "config_invalid"
UNKNOWN: Final = "unknown"

# Order matters: through ProxyJump the jump's own failure ("Connection refused", "Host key verification failed")
# comes first and "Connection closed by UNKNOWN port 65535" follows it; the cause must win over the symptom.
_STDERR_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        RESOLVE_FAILED,
        re.compile(
            r"Could not resolve hostname|nodename nor servname|Name or service not known"
            r"|Temporary failure in name resolution|No address associated with hostname",
            re.IGNORECASE,
        ),
    ),
    (
        HOST_KEY_REJECTED,
        re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED", re.IGNORECASE),
    ),
    (
        AUTH_REFUSED,
        re.compile(
            r"Permission denied \(|Too many authentication failures|No supported authentication methods",
            re.IGNORECASE,
        ),
    ),
    (CONNECTION_REFUSED, re.compile(r"Connection refused", re.IGNORECASE)),
    (
        CONNECT_TIMED_OUT,
        re.compile(r"Operation timed out|Connection timed out|timed out during banner exchange", re.IGNORECASE),
    ),
    (UNREACHABLE, re.compile(r"No route to host|Network is unreachable|Host is down", re.IGNORECASE)),
    (
        CONNECTION_LOST,
        re.compile(r"Connection closed by|Connection reset by|kex_exchange_identification|Broken pipe", re.IGNORECASE),
    ),
    (
        NEGOTIATION_FAILED,
        re.compile(r"Unable to negotiate|no matching (?:host key type|key exchange method|cipher|MAC)", re.IGNORECASE),
    ),
    (
        CONFIG_INVALID,
        re.compile(r"Bad owner or permissions|Bad configuration option|Unsupported option|line \d+: ", re.IGNORECASE),
    ),
)


def classify_stderr(stderr: str) -> str:
    """The reason for an exit-255 failure, from the client's stderr."""
    for reason, pattern in _STDERR_PATTERNS:
        if pattern.search(stderr):
            return reason
    return UNKNOWN


def to_error(reason: str, *, host: str) -> ExternalError:
    """The error a settled reason raises (settle `resolve_failed` first). Transient network trouble is retryable;
    what the owner must fix (alias, key, host key, config) is not."""
    if reason in {HOST_KEY_REJECTED, AUTH_REFUSED}:
        return AuthFailed(provider=host)
    if reason == CONNECT_TIMED_OUT:
        return CallTimedOut(operation=OPERATION)
    if reason in {DNS_FAILED, CONNECTION_REFUSED, UNREACHABLE, CONNECTION_LOST}:
        return ProviderUnavailable(provider=host, retryable=True)
    return ProviderUnavailable(provider=host, retryable=False)  # host_unknown, negotiation, config, unknown


def settle_resolution(*, configured: bool) -> str:
    """A resolution failure of an alias the config maps to a host name is DNS trouble; else the alias is unknown."""
    return DNS_FAILED if configured else HOST_UNKNOWN


def configured_hostname(ssh_g_output: str) -> str | None:
    """The `hostname` line of `ssh -G <alias>` (the resolved config); `None` if there is none."""
    for line in ssh_g_output.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "hostname" and value.strip():
            return value.strip()
    return None


def is_configured(alias: str, ssh_g_output: str) -> bool:
    """True when `~/.ssh/config` maps the alias to another host name. Without a `HostName`, ssh resolves the alias
    itself (lower-cased in `-G` output), so a resolution failure then means the alias is unknown."""
    hostname = configured_hostname(ssh_g_output)
    return hostname is not None and hostname.lower() != alias.lower()
