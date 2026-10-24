"""`OpenSshRemoteShell`: read-only server checks through the system OpenSSH client (UC-15; SPEC §3.2.4, §5.7).

Policy is not decided here. The caller classifies the command with the gate first, so only commands on
`gate.readonly_remote_commands` arrive. This adapter runs one, non-interactively, on an alias from the owner's
`~/.ssh/config`:
- `ssh -T -o BatchMode=yes`: no password, passphrase or host-key prompt, so a missing key fails at once; the
  `ConnectTimeout` is cut from the call's own `timeout`;
- the owner's config still applies (HostName, User, IdentityFile, ProxyJump), minus what a one-shot check must not
  do: port, agent and X11 forwarding, `LocalCommand`, a configured `RemoteCommand`, and becoming a ControlMaster
  that outlives the call;
- `--` before the alias, so ssh reads neither the alias nor the command as an option;
- through `core.proc`: argv only, its own process group, killed on timeout; the environment is cut down to what ssh
  needs (home, user, agent socket, locale, and PATH for ProxyJump).

Errors: `translate.py`. Retryable failures (connection refused or lost, unreachable, DNS down, connect timeout) are
retried via `core.retry` inside the call's `timeout`; that is safe because the commands are read-only.

Logs carry the alias, exit code and duration; a failure adds the error code, the snake_case reason, `retryable` and
the attempt count. Never the command, its output or stderr: any of them can hold a secret.
"""

import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Final

from yordamchi.adapters.ssh.translate import (
    OPERATION,
    RESOLVE_FAILED,
    SSH_FAILED_EXIT,
    classify_stderr,
    is_configured,
    settle_resolution,
    to_error,
)
from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, ExternalError, NotInstalled
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.remote_shell import RemoteResult

log = get_log(__name__)

SSH: Final = "ssh"
TIMED_OUT: Final = "timed_out"
NOT_INSTALLED: Final = "not_installed"
CONNECT_TIMEOUT_MAX_S: Final = 10
CONFIG_CHECK_TIMEOUT_S: Final = 3.0
# `Paths.shims` (core/paths.py) ends in this name; the gate shims must never stand in for the bot's own client.
SHIMS_DIR_NAME: Final = "shims"
# A `Host` alias: no user@, port, URI or option syntax.
_ALIAS: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,252}")
_ENV_KEYS: Final = frozenset({"HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "LANG", "PATH"})
_OPTIONS: Final = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ClearAllForwardings=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ForwardX11=no",
    "-o",
    "PermitLocalCommand=no",
    "-o",
    "RemoteCommand=none",
    "-o",
    "ControlMaster=no",
)


def connect_timeout_s(remaining_s: float) -> int:
    """Half of what is left of the call, 1–10 s: ssh then reports a classified connect timeout before the kill."""
    return max(1, min(CONNECT_TIMEOUT_MAX_S, int(remaining_s / 2)))


def ssh_argv(ssh: str, host: str, command: str, *, connect_timeout: int) -> list[str]:
    return [ssh, *_OPTIONS, "-o", f"ConnectTimeout={connect_timeout}", "--", host, command]


def check_alias(host: str) -> None:
    """A `servers.hosts` entry is an `~/.ssh/config` alias; anything else is a bug upstream, never sent to ssh."""
    if not _ALIAS.fullmatch(host):
        msg = "the host must be an ~/.ssh/config alias"
        raise ValueError(msg)


def ssh_env(env: Mapping[str, str]) -> dict[str, str]:
    """`core.proc.clean_env()` cut down to what the client needs; nothing else can reach a `SendEnv`."""
    return {name: value for name, value in env.items() if name in _ENV_KEYS or name.startswith("LC_")}


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class _Failure:
    error: ExternalError
    reason: str


def _failed(failures: list[_Failure], error: ExternalError, reason: str) -> ExternalError:
    """Remember why `error` is raised, so the final log line can name it."""
    failures.append(_Failure(error=error, reason=reason))
    return error


class OpenSshRemoteShell:
    """`RemoteShell` on the system OpenSSH client.

    `ssh` is the client: `/usr/bin/ssh` in production, or a name looked up on `path`. `path` is the child's PATH
    (ProxyJump runs `ssh` from it), without the gate shims: the bot's own checks never go through its gate socket.
    """

    def __init__(self, *, ssh: str, path: str) -> None:
        if any(PurePath(entry).name == SHIMS_DIR_NAME for entry in path.split(":") if entry):
            msg = "the ssh child PATH must not contain the gate shims"
            raise ValueError(msg)
        self._ssh = ssh
        self._path = path

    def _binary(self, failures: list[_Failure]) -> str:
        located = shutil.which(self._ssh, path=self._path)
        if located is None:
            raise _failed(failures, NotInstalled(binary=SSH), NOT_INSTALLED)
        return located

    async def run(self, host: str, command: str, *, timeout: float) -> RemoteResult:
        check_alias(host)
        if not command.strip():
            msg = "an empty command would open a login shell"
            raise ValueError(msg)
        started = time.monotonic()
        deadline = started + timeout
        failures: list[_Failure] = []  # every failed attempt, so the log names the final error's reason
        attempts = 0

        async def attempt() -> proc.ProcResult:
            nonlocal attempts
            attempts += 1
            return await self._attempt(host, command, deadline=deadline, failures=failures)

        try:
            result = await retrying(attempt, timeout=timeout, operation=OPERATION)
        except ExternalError as error:
            # An error no attempt raised is `core.retry`'s own: the budget ran out mid-attempt.
            reason = next((failure.reason for failure in failures if failure.error is error), TIMED_OUT)
            log.warning(
                "ssh_command_failed",
                host=host,
                code=error.code.value,
                reason=reason,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info("ssh_command_finished", host=host, exit_code=result.returncode, duration_ms=duration_ms)
        return RemoteResult(
            host=host,
            exit_code=result.returncode,
            stdout=_text(result.stdout),
            stderr=_text(result.stderr),
            duration_ms=duration_ms,
        )

    async def _attempt(self, host: str, command: str, *, deadline: float, failures: list[_Failure]) -> proc.ProcResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _failed(failures, CallTimedOut(operation=OPERATION), TIMED_OUT)
        ssh = self._binary(failures)
        argv = ssh_argv(ssh, host, command, connect_timeout=connect_timeout_s(remaining))
        try:
            result = await proc.run(argv, env=self._env(), timeout=remaining)
        except TimeoutError as exc:
            raise _failed(failures, CallTimedOut(operation=OPERATION), TIMED_OUT) from exc
        except OSError as exc:
            raise _failed(failures, NotInstalled(binary=SSH), NOT_INSTALLED) from exc
        if result.returncode != SSH_FAILED_EXIT:
            return result
        reason = classify_stderr(_text(result.stderr))
        if reason == RESOLVE_FAILED:
            reason = settle_resolution(configured=await self._is_configured(ssh, host, deadline=deadline))
        raise _failed(failures, to_error(reason, host=host), reason)

    async def _is_configured(self, ssh: str, host: str, *, deadline: float) -> bool:
        """`ssh -G` prints the resolved config without connecting. Unknown (it failed) counts as not configured."""
        remaining = min(deadline - time.monotonic(), CONFIG_CHECK_TIMEOUT_S)
        if remaining <= 0:
            return False
        try:
            result = await proc.run([ssh, "-G", "--", host], env=self._env(), timeout=remaining)
        except (TimeoutError, OSError):
            return False
        return result.returncode == 0 and is_configured(host, _text(result.stdout))

    def _env(self) -> dict[str, str]:
        return ssh_env(proc.clean_env(path=self._path))
