from collections.abc import AsyncIterator

import pytest

from tests.fakes.mcp import FakeMcpClient
from tests.integration.mcp_harness import mcp_fixtures
from yordamchi.adapters.mcp.client import McpHttpClient
from yordamchi.core.errors import AuthFailed, ExternalError
from yordamchi.ports.mcp import McpClient, McpTool

URL = "https://mcp.example.test/mcp"
HEADERS = {"Authorization": "Bearer fixture-token"}


class McpClientContract:
    """Run against the fixture MCP server: one read tool, one write tool (UC-35)."""

    @pytest.fixture
    def client(self) -> McpClient:
        raise NotImplementedError

    async def test_list_tools_marks_read_only_tools(self, client: McpClient) -> None:
        tools = {tool.name: tool for tool in await client.list_tools(URL, HEADERS, timeout=25.0)}
        assert tools["read_status"].read_only
        assert not tools["write_note"].read_only

    async def test_call_tool_returns_a_result(self, client: McpClient) -> None:
        result = await client.call_tool(URL, HEADERS, "read_status", {}, timeout=25.0)
        assert not result.is_error

    async def test_an_unreachable_server_is_an_external_error(self, client: McpClient) -> None:
        with pytest.raises(ExternalError):
            await client.list_tools("https://nowhere.example.test/mcp", HEADERS, timeout=25.0)

    async def test_wrong_credentials_fail_auth(self, client: McpClient) -> None:
        with pytest.raises(AuthFailed):
            await client.list_tools(URL, {"Authorization": "Bearer wrong"}, timeout=25.0)


class TestFakeMcpClient(McpClientContract):
    @pytest.fixture
    def client(self) -> McpClient:
        fake = FakeMcpClient()
        fake.add_server(
            URL,
            [McpTool("read_status", "status", read_only=True), McpTool("write_note", "note", read_only=False)],
            headers=HEADERS,
        )
        return fake


class TestMcpHttpClient(McpClientContract):
    """The real adapter, offline against the in-process fixture MCP server (tests/integration/mcp_harness.py)."""

    @pytest.fixture
    async def client(self) -> AsyncIterator[McpClient]:
        async with mcp_fixtures(static_host="mcp.example.test", static_token="fixture-token") as fixtures:
            yield McpHttpClient(transport=fixtures.transport)
