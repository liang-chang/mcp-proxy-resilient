"""Real HTTP evidence for credential-safe logs and expired-session recovery."""
# ruff: noqa: SIM117, SLF001

import asyncio
import logging

import anyio
import pytest
import uvicorn
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session as connected
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mcp_proxy.resilient import ResilienceSettings, ResilientProxy
from mcp_proxy.resilient_transports import http_connector
from mcp_proxy.transport_logging import private_transport_logs
from tests.test_mcp_server import BackgroundServer, create_starlette_app
from tests.test_resilient_catalog import CatalogBackend


def test_lazily_created_transport_logger_is_private(caplog: pytest.LogCaptureFixture) -> None:
    """Lazy transport imports must not bypass filters; unrelated/legacy logs remain visible."""
    with private_transport_logs():
        logging.getLogger("httpcore.synthetic_lazy_transport").warning("synthetic-lazy-secret")
        logging.getLogger("unrelated-service").warning("unrelated visible")
    logging.getLogger("httpcore.synthetic_lazy_transport").warning("legacy visible")
    assert "synthetic-lazy-secret" not in caplog.text
    assert "unrelated visible" in caplog.text
    assert "legacy visible" in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
async def test_http_credentials_never_logged(
    level: int, transport: str, caplog: pytest.LogCaptureFixture
) -> None:
    """All transport loggers and exception paths must keep configured secrets private."""
    backend = CatalogBackend()
    app = create_starlette_app(backend.server)
    rejected = False
    seen = []

    async def gate(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]).get(b"x-access-token"))
            if rejected:
                await Response(status_code=403)(scope, receive, send)
                return
        await app(scope, receive, send)

    server = BackgroundServer(uvicorn.Config(gate, port=0, access_log=False))
    caplog.set_level(level)
    async with server.run_in_background():
        connector = http_connector(
            server.url
            + ("/sse" if transport == "sse" else "/mcp")
            + "?token=synthetic-query-secret",
            transport=transport,
            headers={"X-Access-Token": "synthetic-header-secret"},
        )
        async with ResilientProxy(connector, ResilienceSettings(retry_interval=60)) as proxy:
            assert (await proxy.reconnect())["state"] == "READY"
            rejected = True
            assert (await proxy.reconnect())["state"] == "AUTH_REQUIRED"
    assert b"synthetic-header-secret" in seen
    # Inspect only proxy/client-side logs, not the independently hosted downstream's logs.
    client_logs = "\n".join(
        record.getMessage() + (record.exc_text or "")
        for record in caplog.records
        if record.name.startswith(("mcp_proxy", "mcp.client", "httpx", "httpcore"))
    )
    assert "synthetic-query-secret" not in client_logs
    assert "synthetic-header-secret" not in client_logs


async def test_expired_http_session_fails_queue_without_replay() -> None:
    """HTTP 404 invalidates the connection without treating a business code as transport loss."""
    backend = CatalogBackend()
    app = create_starlette_app(backend.server)
    expired = False
    entered, release = asyncio.Event(), asyncio.Event()
    expired_posts = []

    async def gate(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and expired and scope["method"] == "POST":
            expired_posts.append(1)
            entered.set()
            await release.wait()
            await Response(status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)

    server = BackgroundServer(uvicorn.Config(gate, port=0))
    async with server.run_in_background():
        async with ResilientProxy(
            http_connector(server.url + "/mcp", transport="streamablehttp"),
            ResilienceSettings(retry_interval=60),
        ) as proxy:
            await proxy.reconnect()
            async with connected(proxy.app) as client:
                expired = True
                active = asyncio.create_task(client.get_prompt("greet"))
                await asyncio.wait_for(entered.wait(), 2)
                queued = asyncio.create_task(client.read_resource("file:///item/2"))
                with anyio.fail_after(2):
                    while proxy._jobs.empty():  # noqa: ASYNC110
                        await asyncio.sleep(0.01)
                release.set()
                results = await asyncio.gather(active, queued, return_exceptions=True)
                assert all(isinstance(result, McpError) for result in results)
                assert proxy.status()["state"] != "READY"
                assert len(expired_posts) == 1
                expired = False
                assert (await proxy.reconnect())["state"] == "READY"
                assert (await client.get_prompt("greet")).messages[0].content.text == "hello"
