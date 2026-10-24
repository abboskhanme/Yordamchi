"""McpClient: validating and calling a remote MCP integration with vault headers (SPEC §3.2.10; P2).

The OAuth paste-back flow (`OAuthClientProvider`) is added with `adapters/mcp` in P2.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from yordamchi.core.json_value import JsonValue


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str
    read_only: bool  # the tool's `readOnlyHint`
    input_schema: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    is_error: bool
    text: str
    structured: JsonValue = None


class McpClient(Protocol):
    async def list_tools(self, url: str, headers: Mapping[str, str], *, timeout: float) -> list[McpTool]: ...

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float,
    ) -> McpToolResult: ...
