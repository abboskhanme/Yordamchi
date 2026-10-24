"""`FakeMcpClient`: an in-memory MCP server registry (the fixture MCP server with OAuth arrives with P2)."""

from collections.abc import Mapping

from yordamchi.core.errors import AuthFailed, ProviderUnavailable
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.mcp import McpClient, McpTool, McpToolResult


class FakeMcpClient:
    def __init__(self) -> None:
        self.servers: dict[str, list[McpTool]] = {}
        self.required_headers: dict[str, Mapping[str, str]] = {}
        self.results: dict[tuple[str, str], McpToolResult] = {}
        self.calls: list[tuple[str, str, Mapping[str, JsonValue]]] = []

    def add_server(self, url: str, tools: list[McpTool], *, headers: Mapping[str, str] | None = None) -> None:
        self.servers[url] = tools
        if headers is not None:
            self.required_headers[url] = headers

    def _check(self, url: str, headers: Mapping[str, str]) -> None:
        if url not in self.servers:
            raise ProviderUnavailable(provider=url, retryable=False)
        required = self.required_headers.get(url)
        if required is not None and any(headers.get(key) != value for key, value in required.items()):
            raise AuthFailed(provider=url)

    async def list_tools(self, url: str, headers: Mapping[str, str], *, timeout: float) -> list[McpTool]:
        self._check(url, headers)
        return list(self.servers[url])

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float,
    ) -> McpToolResult:
        self._check(url, headers)
        self.calls.append((url, name, dict(arguments)))
        return self.results.get((url, name), McpToolResult(is_error=False, text="ok"))


_: McpClient = FakeMcpClient()
