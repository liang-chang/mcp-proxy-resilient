"""Real CLI/frozen-process matrix with isolated named backends."""
# ruff: noqa: S603, ASYNC220, SIM117

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from tests.test_mcp_server import BackgroundServer, create_starlette_app
from tests.test_portable import isolated_environment
from tests.test_resilient_catalog import CatalogBackend


@asynccontextmanager
async def proxy_client(
    command: str,
    args: list[str],
    frontend: str,
    tmp_path: Path,
    env: dict[str, str],
) -> AsyncIterator[ClientSession]:
    """Own one CLI process and clean it up after closing the upstream session."""
    if frontend == "stdio":
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            yield client
        return
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with (tmp_path / "proxy.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [command, *args, "--port", str(port)],
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            url = f"http://127.0.0.1:{port}/servers/selected"
            async with httpx.AsyncClient() as http:
                while True:
                    assert process.poll() is None
                    try:
                        if (await http.get(url + "/healthz")).is_success:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.05)
            async with streamablehttp_client(url + "/mcp") as (read, write, _):
                async with ClientSession(read, write) as client:
                    yield client
        finally:
            process.terminate()
            # bugfix: Unix shutdown may send DELETE to the backend hosted on this loop.
            # Keep that loop running while waiting for the proxy to finish cleanup.
            await asyncio.to_thread(process.wait, timeout=10)


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "portable"])
@pytest.mark.parametrize("frontend", ["stdio", "http"])
@pytest.mark.parametrize("backend", ["stdio", "sse", "streamablehttp"])
async def test_named_transport_matrix(
    tmp_path: Path,
    frontend: str,
    backend: str,
    frozen: bool,
) -> None:
    """Verify independent transport selection in both source and frozen distributions."""
    exe = os.environ.get("MCP_PROXY_PORTABLE_EXE")
    if frozen and (sys.platform != "win32" or not exe):
        pytest.skip("Windows portable tests require MCP_PROXY_PORTABLE_EXE")
    fixture = Path(__file__).parent / "fixtures" / "recovery_server.py"
    marker = tmp_path / "ready"
    unselected = tmp_path / "unselected"
    remote = CatalogBackend()
    server = BackgroundServer(uvicorn.Config(create_starlette_app(remote.server), port=0))
    async with server.run_in_background():
        selected = (
            {"command": sys.executable, "args": [str(fixture), str(marker)]}
            if backend == "stdio"
            else {
                "url": server.url + ("/sse" if backend == "sse" else "/mcp"),
                "transport": backend,
            }
        )
        entries = {"selected": selected}
        if frontend == "stdio":
            entries["never-start"] = {
                "command": sys.executable,
                "args": ["-c", f"from pathlib import Path; Path({str(unselected)!r}).touch()"],
            }
        else:
            entries["offline"] = {"command": sys.executable, "args": ["-c", "pass"]}
        config = tmp_path / "servers.json"
        config.write_text(json.dumps({"mcpServers": entries}), encoding="utf-8")
        args = [
            "--resilient",
            "--frontend",
            frontend,
            "--named-server-config",
            str(config),
            "--retry-interval",
            "60",
        ]
        if frontend == "stdio":
            args += ["--server", "selected"]
        env = isolated_environment() if frozen else dict(os.environ)
        if not frozen:
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")  # noqa: ASYNC240
            args = ["-m", "mcp_proxy", *args]
        with anyio.fail_after(30):
            async with proxy_client(
                exe if frozen else sys.executable, args, frontend, tmp_path, env
            ) as client:
                init = await client.initialize()
                assert init.capabilities.resources.listChanged
                assert init.capabilities.prompts.listChanged
                if backend == "stdio":
                    assert (await client.call_tool("proxy_reconnect", {})).isError
                    assert (await client.list_resources()).resources == []
                    assert (await client.list_prompts()).prompts == []
                    marker.touch()
                assert not (await client.call_tool("proxy_reconnect", {})).isError
                assert len((await client.list_resources()).resources) == 1
                assert len((await client.list_resource_templates()).resourceTemplates) == 1
                assert len((await client.list_prompts()).prompts) == 1
                assert (await client.read_resource("file:///item/2")).contents[0].text == "body"
                assert (await client.get_prompt("greet", {"name": "Ada"})).messages[
                    0
                ].content.text == "hello"
                assert not unselected.exists()
