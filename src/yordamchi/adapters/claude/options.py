"""`SessionSpec` → `ClaudeAgentOptions` (SPEC §5.3; STACK §2.1, §4.2, §4.4; docs/SPIKES.md N7–N9).

Shared by the executor (T10) and the assistant pool (T12). Pure: it only builds values.

Invariants the adapter enforces whatever the spec says:
- `cli_path` is the installed `~/.local/bin/claude` symlink, never the SDK's bundled copy (NFR-SEC-05, NFR-REL-08);
- never `--bare` (it skips the keychain login) and never a credential variable: `core.proc.clean_env` drops the
  inherited ones (`CLAUDE_CODE_*`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `ANTHROPIC_*`, Yordamchi's own) and raises on any
  refused name in the additions. `options.env` is that full clean environment, PATH with the shims first;
- `ENABLE_CLAUDEAI_MCP_SERVERS=false`: no claude.ai account connectors in a dev session (NFR-SEC-07, N7);
- `SendMessage` and `ListAgents` are always disallowed: every SDK session opens a peer socket (N8);
- `setting_sources` is always passed explicitly (the SDK default flipped between versions);
- `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`: the `session_state_changed` frames that tell when a turn is over;
- git: `core.hooksPath` points at the release's githooks via `GIT_CONFIG_*`, and git/gh never prompt.

MCP servers are passed as `--mcp-config` JSON, which `ps` shows, so an HTTP server carries only its URL and a
`headersHelper` command; tokens never appear here (STACK §4.2).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import HookEvent

from yordamchi.core import proc
from yordamchi.core.errors import ConfigInvalid
from yordamchi.ports.agent import SessionSpec

CLI_RELATIVE_PATH: Final = Path(".local/bin/claude")
ALWAYS_DISALLOWED: Final = ("SendMessage", "ListAgents")
REFUSED_EXTRA_ARGS: Final = frozenset({"bare", "dangerously-skip-permissions"})
ENFORCED_ENV: Final = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
}
QUIET_GIT_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
CLAUDE_CODE_PRESET: Final = "claude_code"

type CanUseTool = Callable[..., Any]


def default_cli_path(home: Path) -> Path:
    """`~/.local/bin/claude`: the symlink, so a CLI auto-update takes effect on the next spawn (SPEC §5.3)."""
    return home / CLI_RELATIVE_PATH


def git_hooks_env(githooks: Path) -> dict[str, str]:
    """Point every git call of the session at the release's hooks without touching any repo config."""
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(githooks)}


def session_env(
    spec: SessionSpec,
    *,
    search_path: str,
    githooks_dir: Path | None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The child's whole environment: clean base, PATH with shims first, git quieting, the spec's additions, then the
    enforced values on top (a spec cannot turn account connectors back on)."""
    extra: dict[str, str] = dict(QUIET_GIT_ENV)
    if githooks_dir is not None:
        extra.update(git_hooks_env(githooks_dir))
    extra.update(spec.env)
    extra.update(ENFORCED_ENV)
    return proc.clean_env(path=search_path, extra=extra, base=base)


@dataclass(frozen=True, slots=True)
class Launch:
    """How this spawn identifies its session: a fresh pre-assigned id, or a resume."""

    session_id: str
    resume: bool


def system_prompt(spec: SessionSpec) -> dict[str, str]:
    """Dev sessions keep the `claude_code` preset; `system_append` holds only the fixed parts (SPEC §5.4)."""
    prompt: dict[str, str] = {"type": "preset", "preset": CLAUDE_CODE_PRESET}
    if spec.system_append:
        prompt["append"] = spec.system_append
    return prompt


def mcp_config(spec: SessionSpec, sdk_servers: Mapping[str, Any]) -> dict[str, Any]:
    servers: dict[str, Any] = {
        name: {"type": "http", "url": server.url, "headersHelper": server.headers_helper}
        for name, server in spec.mcp_servers.items()
    }
    for server_id in spec.sdk_servers:
        if server_id not in sdk_servers:
            raise ConfigInvalid(key="sdk_servers", problem="unknown_server")
        servers[server_id] = sdk_servers[server_id]
    return servers


def disallowed_tools(spec: SessionSpec) -> list[str]:
    return list(dict.fromkeys((*spec.disallowed_tools, *ALWAYS_DISALLOWED)))


def build_options(
    spec: SessionSpec,
    *,
    cli_path: Path,
    env: Mapping[str, str],
    launch: Launch,
    hooks: Mapping[HookEvent, list[HookMatcher]],
    can_use_tool: CanUseTool,
    sdk_servers: Mapping[str, Any] | None = None,
    stderr: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    refused = sorted(REFUSED_EXTRA_ARGS & set(spec.extra_args))
    if refused:
        raise ConfigInvalid(key="extra_args", problem="refused_flag")
    return ClaudeAgentOptions(
        cli_path=str(cli_path),
        cwd=str(spec.cwd),
        model=spec.model,
        effort=cast("Any", spec.effort),
        system_prompt=cast("Any", system_prompt(spec)),
        setting_sources=list(spec.setting_sources),
        tools=list(spec.tools) if spec.tools is not None else None,
        allowed_tools=list(spec.allowed_tools),
        disallowed_tools=disallowed_tools(spec),
        permission_mode=spec.permission_mode,
        mcp_servers=cast("Any", mcp_config(spec, sdk_servers or {})),
        strict_mcp_config=spec.strict_mcp,
        env=dict(env),
        max_turns=spec.max_turns,
        extra_args=dict(spec.extra_args),
        hooks=cast("Any", dict(hooks)),
        can_use_tool=can_use_tool,
        session_id=None if launch.resume else launch.session_id,
        resume=launch.session_id if launch.resume else None,
        stderr=stderr,
    )
