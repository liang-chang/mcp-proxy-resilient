"""Opt-in smoke tests for the relocated, frozen Windows distribution."""
# ruff: noqa: S603, PLR2004, ASYNC220

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from mcp_proxy import APP_NAME, __version__
from tests.test_mcp_server import make_background_server

EXE = os.environ.get("MCP_PROXY_PORTABLE_EXE", "")
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not EXE,
    reason="Windows portable tests require MCP_PROXY_PORTABLE_EXE",
)


def isolated_environment() -> dict[str, str]:
    """Exclude development interpreters from the proxy's environment."""
    env = dict(os.environ)
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    return env


def test_portable_identity(tmp_path: Path) -> None:
    """The relocated program starts without uv or Python on PATH."""
    result = subprocess.run(
        [EXE, "--version"],
        env=isolated_environment(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == f"{APP_NAME} {__version__}"
    result = subprocess.run(
        [EXE, "--help"],
        env=isolated_environment(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert "--resilient" in result.stdout
    assert "--frontend" in result.stdout
    for directory in ("_internal", "dependency-notices"):
        metadata = list(
            (Path(EXE).parent / directory).glob("mcp_proxy_resilient-*.dist-info/METADATA")
        )
        assert len(metadata) == 1
        assert f"Version: {__version__}\n" in metadata[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("transport", ["sse", "streamablehttp", "offline"])
async def test_portable_stdio_frontend(transport: str) -> None:
    """Frozen imports support both real HTTP backends and offline initialization."""
    server = make_background_server()
    async with server.run_in_background():
        suffix = "/sse" if transport == "sse" else "/mcp"
        url = "http://127.0.0.1:1/mcp" if transport == "offline" else server.url + suffix
        params = StdioServerParameters(
            command=EXE,
            args=[
                "--resilient",
                "--transport",
                "streamablehttp" if transport == "offline" else transport,
                url,
            ],
            env=isolated_environment(),
        )
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == APP_NAME
                assert initialized.serverInfo.version == __version__
                result = await client.call_tool("proxy_reconnect", {})
                assert bool(result.isError) == (transport == "offline")
                if transport == "offline":
                    assert len((await client.list_tools()).tools) == 2
                else:
                    result = await client.call_tool("echo", {"message": "portable"})
                    assert not result.isError
                    assert result.content[0].text == "Echo: portable"


async def test_portable_named_http_frontend(tmp_path: Path) -> None:
    """Frozen HTTP hosting starts an external stdio backend after late readiness."""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    marker = tmp_path / "ready"
    fixture = Path(__file__).parent / "fixtures" / "recovery_server.py"
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "late": {
                        "command": sys.executable,
                        "args": [str(fixture), str(marker)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with (tmp_path / "proxy.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                EXE,
                "--resilient",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--named-server-config",
                str(config),
                "--retry-interval",
                "60",
            ],
            cwd=tmp_path,
            env=isolated_environment(),
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            url = f"http://127.0.0.1:{port}/servers/late"
            with anyio.fail_after(30):
                async with httpx.AsyncClient() as http:
                    while True:
                        assert process.poll() is None
                        try:
                            if (await http.get(url + "/healthz")).is_success:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)
                async with (
                    streamablehttp_client(url + "/mcp") as (read, write, _),
                    ClientSession(read, write) as client,
                ):
                    await client.initialize()
                    assert (await client.call_tool("proxy_reconnect", {})).isError
                    marker.touch()
                    assert not (await client.call_tool("proxy_reconnect", {})).isError
                    assert not (await client.call_tool("write_once", {})).isError
        finally:
            process.terminate()
            process.wait(timeout=10)
