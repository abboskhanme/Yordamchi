"""Subprocesses and the environment children inherit (CONVENTIONS §6; SPEC §2.5 steps 2–4; STACK §4.4).

- `refused_credentials()` / `check_startup_credentials()`: the refuse-list. Any credential source that outranks the
  keychain login (an API key, a token, a proxy, another config dir, an `apiKeyHelper`) stops the bot with exit code
  `EXIT_REFUSED` (78). This module and its test are the only places those variable names appear (CONVENTIONS §13).
- `scrub_environ()`: removes `CLAUDECODE` and every `CLAUDE_CODE_*` from `os.environ`, because the Agent SDK merges
  `os.environ` into each `claude` child and `env=` can only add. Run the credential check first: some refused names
  (`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_*`) would otherwise be scrubbed before they are seen.
- `build_search_path()` / `clean_env()`: the child PATH, in SPEC order, and the environment for every child.
- `run()`: argv only, never a shell; its own process group; a timeout or cancellation kills the group
  (TERM, grace, KILL) and raises `TimeoutError` / `CancelledError`. Adapters translate that into `CallTimedOut`.
  `run_sync()` is the same for the `bots/cli` entry points, which run without an event loop.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from yordamchi.core.paths import Paths

EXIT_REFUSED: Final = 78  # EX_CONFIG (SPEC §2.5 step 3)

# STACK §4.4 (wider than NFR-SEC-04). Any ANTHROPIC_* is refused too: SPEC §5.3 "Never: ANTHROPIC_*".
REFUSED_ENV_VARS: Final = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CONFIG_DIR",
    }
)
REFUSED_ENV_PREFIX: Final = "ANTHROPIC_"
REFUSED_SETTINGS_KEYS: Final = ("apiKeyHelper",)

# CLAUDE_PID / CLAUDE_EFFORT leak from a parent Claude session into the child CLI (docs/SPIKES.md, T10 item 6).
SCRUBBED_ENV_VARS: Final = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})
SCRUBBED_ENV_PREFIX: Final = "CLAUDE_CODE_"
# Yordamchi's own variables (env-file path, data-dir override) never reach a child unless passed as `extra`.
PRIVATE_ENV_PREFIX: Final = "YORDAMCHI_"

SYSTEM_PATH: Final = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
LOGIN_SHELL: Final = "/bin/zsh"
LOGIN_SHELL_TIMEOUT_S: Final = 5.0
REQUIRED_BINARIES: Final = ("claude", "git")  # missing → fatal
OPTIONAL_BINARIES: Final = ("ffmpeg", "gh", "node")  # missing → degraded capability, shown in `holat?`
KILL_GRACE_S: Final = 0.5


# --- credential refusal (SPEC §2.5 step 3) -------------------------------------------------------------------------
def is_refused_env(name: str) -> bool:
    return name in REFUSED_ENV_VARS or name.startswith(REFUSED_ENV_PREFIX)


def is_scrubbed_env(name: str) -> bool:
    return name in SCRUBBED_ENV_VARS or name.startswith(SCRUBBED_ENV_PREFIX)


def refused_in_claude_settings(settings: Mapping[str, object], *, source: str) -> list[str]:
    """Refused entries of one Claude settings file: `apiKeyHelper`, and any refused name under `env`."""
    found = [f"{source}:{key}" for key in REFUSED_SETTINGS_KEYS if key in settings]
    env = settings.get("env")
    if isinstance(env, Mapping):
        found.extend(f"{source}:env.{name}" for name in sorted(map(str, env)) if is_refused_env(name))
    elif env is not None:
        found.append(f"{source}:env")  # malformed: it cannot be verified, so it is refused
    return found


def refused_credentials(
    environ: Mapping[str, str], settings: Mapping[str, Mapping[str, object] | None] | None = None
) -> tuple[str, ...]:
    """Names (never values) of every refused credential source. `settings` maps a source label to its parsed JSON;
    `None` as a value means the file exists but could not be read, which is refused (fail closed)."""
    found = [f"env:{name}" for name in sorted(environ) if is_refused_env(name)]
    for source, parsed in (settings or {}).items():
        if parsed is None:
            found.append(f"{source}:unreadable")
        else:
            found.extend(refused_in_claude_settings(parsed, source=source))
    return tuple(found)


def read_settings_file(path: Path) -> Mapping[str, object] | None:
    """Parsed JSON object, or `None` when the file exists but is not a readable JSON object."""
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


def check_credentials(settings_files: Iterable[Path], *, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The startup check: the process environment plus each existing settings file (`~/.claude/settings.json` at
    start; a project's `.claude/settings*.json` before its session opens, SPEC §5.3)."""
    env = os.environ if environ is None else environ
    parsed = {path.name: read_settings_file(path) for path in settings_files if path.exists()}
    return refused_credentials(env, parsed)


def scrub_environ(environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Remove the nested-session variables (`SCRUBBED_ENV_VARS`, `CLAUDE_CODE_*`; SPEC §2.5 step 2); returns them."""
    env = os.environ if environ is None else environ
    removed = tuple(sorted(name for name in env if is_scrubbed_env(name)))
    for name in removed:
        del env[name]
    return removed


# --- child PATH and environment (SPEC §2.5 step 4) -----------------------------------------------------------------
def build_search_path(paths: Paths, login_shell_path: str | None) -> str:
    """Shims, `~/.local/bin`, `DATA/bin`, the login-shell PATH, then the system dirs; first occurrence wins."""
    candidates: list[str] = [str(paths.shims), str(paths.local_bin), str(paths.bin)]
    if login_shell_path:
        candidates.extend(login_shell_path.split(":"))
    candidates.extend(SYSTEM_PATH)
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in candidates:
        normalized = entry.rstrip("/") or entry
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ":".join(ordered)


def clean_env(
    *, path: str, extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment for a child: `base` (default `os.environ`) without scrubbed, refused or Yordamchi-private
    names, with `PATH` set, then `extra` on top. A refused name in `extra` is a bug and raises `ValueError`."""
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if not (is_scrubbed_env(name) or is_refused_env(name) or name.startswith(PRIVATE_ENV_PREFIX))
    }
    env["PATH"] = path
    for name, value in (extra or {}).items():
        if is_refused_env(name):
            msg = f"refusing to pass the credential variable {name} to a child process"
            raise ValueError(msg)
        env[name] = value
    return env


@dataclass(frozen=True, slots=True)
class BinaryReport:
    found: Mapping[str, str]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]

    @property
    def fatal(self) -> bool:
        return bool(self.missing_required)


def find_binaries(
    path: str, *, required: Sequence[str] = REQUIRED_BINARIES, optional: Sequence[str] = OPTIONAL_BINARIES
) -> BinaryReport:
    found: dict[str, str] = {}
    for name in (*required, *optional):
        located = shutil.which(name, path=path)
        if located is not None:
            found[name] = located
    return BinaryReport(
        found=found,
        missing_required=tuple(name for name in required if name not in found),
        missing_optional=tuple(name for name in optional if name not in found),
    )


# --- running a child -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProcResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


async def _kill_group(process: asyncio.subprocess.Process, grace_s: float) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        async with asyncio.timeout(grace_s):
            await process.wait()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    kill_grace_s: float = KILL_GRACE_S,
) -> ProcResult:
    """Run `argv` to completion in its own process group and return its output."""
    if not argv:
        msg = "argv must not be empty"
        raise ValueError(msg)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate(stdin)
    except (TimeoutError, asyncio.CancelledError):
        await _kill_group(process, kill_grace_s)
        raise
    return ProcResult(
        argv=tuple(argv),
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _kill_group_sync(process: "subprocess.Popen[bytes]", grace_s: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_sync(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = True,
    kill_grace_s: float = KILL_GRACE_S,
) -> ProcResult:
    """`run` for the `bots/cli` entry points, which have no event loop (a shim, the pre-push hook): the same argv-only,
    own-process-group contract, and a timeout kills the group and raises `TimeoutError`. `env=None` inherits the
    caller's environment (a git hook runs in the session's); `capture=False` leaves stdout and stderr on the caller's,
    so a chained hook talks to git directly."""
    if not argv:
        msg = "argv must not be empty"
        raise ValueError(msg)
    started = time.monotonic()
    piped = subprocess.PIPE if capture else None
    with subprocess.Popen(  # an argv list, never a shell; leaving the block closes the pipes and reaps the child
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=piped,
        stderr=piped,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group_sync(process, kill_grace_s)
            process.communicate()  # drain what the killed group left in the pipes
            raise TimeoutError(argv[0]) from None
    return ProcResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout or b"",
        stderr=stderr or b"",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def login_shell_path(*, env: Mapping[str, str], timeout: float = LOGIN_SHELL_TIMEOUT_S) -> str | None:
    """The owner's login-shell PATH (`/bin/zsh -lc 'printf %s "$PATH"'`), or `None` if it cannot be read in time."""
    try:
        result = await run([LOGIN_SHELL, "-lc", 'printf %s "$PATH"'], env=env, timeout=timeout)
    except (TimeoutError, OSError):
        return None
    if result.returncode != 0:
        return None
    # Profile scripts may print lines first; printf writes the PATH last, without a newline.
    text = result.stdout.decode("utf-8", errors="replace").rsplit("\n", 1)[-1].strip()
    return text or None
