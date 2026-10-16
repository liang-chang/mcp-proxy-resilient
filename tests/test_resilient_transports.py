"""Opt-in recovery at the real transport and command-line boundaries."""
# ruff: noqa: PLR2004, SIM117

import asyncio
import sys
from pathlib import Path

import anyio
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session as connected

import mcp_proxy.mcp_server as module
from mcp_proxy.__main__ import _create_mcp_settings, _setup_argument_parser
from mcp_proxy.mcp_server import MCPServerSettings, run_mcp_server
from mcp_proxy.resilient import ResilienceSettings, ResilientProxy
from mcp_proxy.resilient_transports import http_connector, stdio_connector
from tests.test_mcp_server import BackgroundServer, make_background_server

SOURCE_DIR = str(Path(__file__).resolve().parents[1] / "src")


def test_cli_recovery_settings_are_opt_in() -> None:
    """Legacy startup remains default; recovery settings apply in both directions."""
    parser = _setup_argument_parser()
    args = parser.parse_args(
        ["--resilient", "--connect-timeout", "2", "--retry-interval", "1", "cmd"]
    )
    settings = _create_mcp_settings(args)
    assert settings.resilience.connect_timeout == 2
    assert settings.resilience.retry_interval == 1
    assert _create_mcp_settings(parser.parse_args(["cmd"])).resilience is None


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
async def test_real_http_backend_reconnect(transport: str) -> None:
    """Reconnect through a real HTTP listener for each supported protocol."""
    server = make_background_server()
    async with server.run_in_background():
        suffix = "/sse" if transport == "sse" else "/mcp"
        connector = http_connector(server.url + suffix, transport=transport)
        async with ResilientProxy(connector, ResilienceSettings()) as proxy:
            async with connected(proxy.app) as client:
                result = await client.call_tool("proxy_reconnect", {})
                assert not result.isError
                assert len((await client.list_tools()).tools) > 2
                assert not (await client.call_tool("proxy_reconnect", {})).isError


async def test_exited_stdio_child_does_not_kill_upstream() -> None:
    """Child exit does not prevent management tool requests."""
    params = StdioServerParameters(command=sys.executable, args=["-c", "pass"])
    with anyio.fail_after(10):
        async with ResilientProxy(stdio_connector(params), ResilienceSettings()) as proxy:
            async with connected(proxy.app) as client:
                result = await client.call_tool("proxy_reconnect", {})
                assert result.isError
                assert len((await client.list_tools()).tools) == 2


async def test_stdio_late_start_crash_no_replay_and_recovery(tmp_path: Path) -> None:
    """A write followed by process exit is never replayed after reconnect."""
    marker = tmp_path / "ready"
    fixture = Path(__file__).parent / "fixtures" / "recovery_server.py"
    params = StdioServerParameters(command=sys.executable, args=[str(fixture), str(marker)])
    with anyio.fail_after(20):
        async with ResilientProxy(
            stdio_connector(params),
            ResilienceSettings(retry_interval=60),
        ) as proxy:
            async with connected(proxy.app) as client:
                assert (await client.call_tool("proxy_reconnect", {})).isError
                marker.touch()
                assert not (await client.call_tool("proxy_reconnect", {})).isError
                result = await client.call_tool("write_once", {"crash": True})
                assert result.isError
                assert "Not replayed" in result.content[0].text
                assert not (await client.call_tool("proxy_reconnect", {})).isError
                assert marker.with_suffix(".calls").read_text().splitlines() == ["write"]
                assert not (await client.call_tool("write_once", {})).isError


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
async def test_actual_stdio_frontend_http_backend(transport: str) -> None:
    """Exercise the actual CLI subprocess with a real HTTP backend."""
    server = make_background_server()
    async with server.run_in_background():
        suffix = "/sse" if transport == "sse" else "/mcp"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_proxy", "--resilient", "--transport", transport, server.url + suffix],
            env={"PYTHONPATH": SOURCE_DIR},
        )
        with anyio.fail_after(20):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                assert not (await client.call_tool("proxy_reconnect", {})).isError
                result = await client.call_tool("echo", {"message": "live"})
                assert not result.isError
                assert result.content[0].text == "Echo: live"


async def test_named_server_isolation_and_http_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing named server leaves other routes and their backends operational."""
    servers = []

    def server_factory(config: uvicorn.Config) -> BackgroundServer:
        server = BackgroundServer(config)
        servers.append(server)
        return server

    monkeypatch.setattr(module.uvicorn, "Server", server_factory)
    fixture = Path(__file__).parent / "fixtures" / "recovery_server.py"
    ready = tmp_path / "good"
    ready.touch()
    late = tmp_path / "late"
    params = {
        name: StdioServerParameters(command=sys.executable, args=[str(fixture), str(marker)])
        for name, marker in [("good", ready), ("late", late)]
    }
    task = asyncio.create_task(
        run_mcp_server(
            MCPServerSettings("127.0.0.1", 0, resilience=ResilienceSettings()),
            named_server_params=params,
        )
    )
    try:
        with anyio.fail_after(20):
            while not servers or not servers[0].started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            url = servers[0].url
            async with (
                streamablehttp_client(url + "/servers/late/mcp") as (read, write, _),
                ClientSession(read, write) as client,
                streamablehttp_client(url + "/servers/good/mcp") as (good_read, good_write, _),
                ClientSession(good_read, good_write) as good_client,
            ):
                await client.initialize()
                await good_client.initialize()
                assert (await client.call_tool("proxy_reconnect", {})).isError
                assert not (await good_client.call_tool("proxy_reconnect", {})).isError
                assert not (await good_client.call_tool("write_once", {})).isError
                late.touch()
                assert not (await client.call_tool("proxy_reconnect", {})).isError
                assert not (await client.call_tool("write_once", {})).isError
                assert not (await good_client.call_tool("write_once", {})).isError
    finally:
        if servers:
            servers[0].should_exit = True
        await task
