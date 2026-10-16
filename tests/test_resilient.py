"""Recovery behavior exercised through real in-memory MCP sessions."""
# ruff: noqa: PLR2004, SIM117

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.server import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session as connected

from mcp_proxy.resilient import ResilienceSettings, ResilientProxy
from mcp_proxy.tool_cache import ToolCache


async def test_protocol_error_preserved_without_disconnecting_backend() -> None:
    """Malformed tool arguments must not take a healthy server offline."""
    backend = Backend()
    backend.online = True

    async def call(request: types.CallToolRequest) -> types.ServerResult:
        if request.params.arguments.get("invalid"):
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid argument x"))
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text="ok")],
            )
        )

    backend.server.request_handlers[types.CallToolRequest] = call
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        await proxy.reconnect()
        async with connected(proxy.app) as client:
            with pytest.raises(McpError, match="Invalid argument x"):
                await client.call_tool("echo", {"invalid": True})
            assert proxy.status()["state"] == "READY"
            assert not (await client.call_tool("echo", {})).isError
            assert backend.opens == 1


@pytest.mark.parametrize("contents", ["broken", "null", '{"version":1,"tools":null}'])
def test_invalid_cache_is_ignored(tmp_path: Path, contents: str) -> None:
    """Malformed persistence never prevents proxy startup."""
    cache = ToolCache(tmp_path, "backend")
    cache.path.write_text(contents)
    assert cache.load() == []


class Backend:
    """Controllable transport factory, retaining real MCP dispatch and framing."""

    def __init__(self) -> None:
        """Start offline with a real server implementing one business tool."""
        self.online = False
        self.opens = 0
        self.active = 0
        self.calls = 0
        self.server = Server("backend")

        @self.server.list_tools()
        async def tools() -> list[types.Tool]:
            return [types.Tool(name="echo", inputSchema={"type": "object"})]

        @self.server.call_tool()
        async def call(_name: str, _arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls += 1
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="business failure")],
                isError=True,
            )

    @asynccontextmanager
    async def connect(self, message_handler: MessageHandlerFnT) -> AsyncIterator[ClientSession]:
        """Count active connections and reject opens while offline."""
        self.opens += 1
        if not self.online:
            raise ConnectionError("offline")
        self.active += 1
        try:
            async with connected(self.server, message_handler=message_handler) as session:
                yield session
        finally:
            self.active -= 1


async def test_offline_handshake_then_manual_recovery_and_notification() -> None:
    """Initialize offline, discover tools later and preserve business errors unchanged."""
    backend = Backend()
    changed = asyncio.Event()

    async def message_handler(message: object) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root,
            types.ToolListChangedNotification,
        ):
            changed.set()

    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        async with connected(proxy.app, message_handler=message_handler) as client:
            init = await client.initialize()
            assert init.capabilities.tools.listChanged is True
            assert {t.name for t in (await client.list_tools()).tools} == {
                "proxy_status",
                "proxy_reconnect",
            }
            backend.online = True
            result = await client.call_tool("proxy_reconnect", {})
            assert not result.isError
            assert json.loads(result.content[0].text)["state"] == "READY"
            with anyio.fail_after(2):
                await changed.wait()
            assert "echo" in {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("echo", {})
            assert result.isError
            assert backend.calls == 1
            assert proxy.status()["state"] == "READY"
    assert backend.active == 0


async def test_concurrent_reconnect_is_single_flight() -> None:
    """Concurrent management requests open only one downstream connection."""
    backend = Backend()
    backend.online = True
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        results = await asyncio.gather(*(proxy.reconnect() for _ in range(10)))
        assert all(r["state"] == "READY" for r in results)
        assert backend.opens == 1
        assert backend.active == 1
    assert backend.active == 0


async def test_background_retry_recovers_without_upstream_reinitialize() -> None:
    """Background retry discovers a server that becomes available later."""
    backend = Backend()
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=0.02)) as proxy:
        async with connected(proxy.app) as client:
            await client.list_tools()
            backend.online = True
            with anyio.fail_after(2):
                while proxy.status()["state"] != "READY":  # noqa: ASYNC110 - bounded state polling.
                    await asyncio.sleep(0.01)
            assert "echo" in {t.name for t in (await client.list_tools()).tools}


async def test_hanging_connect_does_not_block_handshake_or_shutdown() -> None:
    """A hanging transport is bounded without breaking upstream initialization."""

    @asynccontextmanager
    async def hanging(_message_handler: MessageHandlerFnT) -> AsyncIterator[ClientSession]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
        yield  # pragma: no cover

    with anyio.fail_after(2):
        async with ResilientProxy(
            hanging,
            ResilienceSettings(connect_timeout=0.03, retry_interval=60),
        ) as proxy:
            async with connected(proxy.app) as client:
                result = await client.call_tool("proxy_reconnect", {})
                assert result.isError
                assert proxy.status()["state"] == "RETRY_WAIT"
                assert len((await client.list_tools()).tools) == 2


async def test_auth_failure_pauses_automatic_retries() -> None:
    """Recognizable authorization errors require explicit retry and do not leak URLs."""
    attempts = 0

    @asynccontextmanager
    async def unauthorized(_handler: MessageHandlerFnT) -> AsyncIterator[ClientSession]:
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", "https://example.invalid/?token=secret")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("secret", request=request, response=response)
        yield

    async with ResilientProxy(unauthorized, ResilienceSettings(retry_interval=0.01)) as proxy:
        status = await proxy.reconnect()
        assert status["state"] == "AUTH_REQUIRED"
        await asyncio.sleep(0.06)
        assert attempts == 1
        assert "secret" not in json.dumps(status)
        await proxy.reconnect()
        assert attempts == 2


async def test_optional_cache_is_scoped_and_survives_restart(tmp_path: Path) -> None:
    """Only the matching backend may reuse previously discovered tool schemas."""
    settings = ResilienceSettings(tool_cache_dir=tmp_path, retry_interval=60)
    backend = Backend()
    backend.online = True
    async with ResilientProxy(backend.connect, settings, cache_identity="backend-a") as proxy:
        await proxy.reconnect()
    backend.online = False
    async with ResilientProxy(backend.connect, settings, cache_identity="backend-a") as proxy:
        assert [t.name for t in proxy.tools] == ["echo"]
        assert proxy.status()["catalog_stale"]
    async with ResilientProxy(backend.connect, settings, cache_identity="backend-b") as proxy:
        assert proxy.tools == []


async def test_downstream_list_changed_refreshes_all_pages() -> None:
    """Downstream catalog changes replace all pages, including removed tools."""
    backend = Backend()
    backend.online = True
    catalog = ["one", "two"]
    backend_session = None

    async def list_tools(request: types.ListToolsRequest) -> types.ServerResult:
        nonlocal backend_session
        backend_session = request_ctx.get().session
        cursor = request.params.cursor if request.params else None
        index = int(cursor or 0)
        return types.ServerResult(
            types.ListToolsResult(
                tools=[types.Tool(name=catalog[index], inputSchema={"type": "object"})],
                nextCursor=str(index + 1) if index + 1 < len(catalog) else None,
            )
        )

    backend.server.request_handlers[types.ListToolsRequest] = list_tools
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=0.02)) as proxy:
        await proxy.reconnect()
        assert [t.name for t in proxy.tools] == ["one", "two"]
        catalog[:] = ["replacement"]
        await backend_session.send_tool_list_changed()
        with anyio.fail_after(2):
            while [t.name for t in proxy.tools] != catalog:  # noqa: ASYNC110 - bounded state polling.
                await asyncio.sleep(0.01)
