"""Transport adapters; all retry and lifecycle policy lives in resilient.py."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .backend import BackendParameters
from .httpx_client import custom_httpx_client
from .resilient import Connector, ResilienceSettings, ResilientProxy
from .transport_logging import private_transport_logs


def connector_for(params: BackendParameters) -> Connector:
    """Choose framing only; recovery policy remains in the shared facade."""
    if isinstance(params, StdioServerParameters):
        return stdio_connector(params)
    return http_connector(
        params.url,
        transport=params.transport,
        headers=params.headers,
        auth=params.auth,
        verify_ssl=params.verify_ssl,
    )


def cache_identity_for(name: str, params: BackendParameters) -> str | None:
    """Hash connection identity later; never persist credentials themselves."""
    if isinstance(params, StdioServerParameters):
        return name + ":" + params.model_dump_json()
    if params.auth is not None:
        return None  # Dynamic authentication has no stable principal identity.
    return (
        name
        + ":"
        + json.dumps(
            {
                "url": params.url,
                "transport": params.transport,
                "headers": params.headers,
                "verify_ssl": params.verify_ssl,
            },
            sort_keys=True,
        )
    )


def stdio_connector(params: StdioServerParameters) -> Connector:
    """Open a fresh subprocess and session on each attempt."""

    @asynccontextmanager
    async def connect(handler: MessageHandlerFnT) -> AsyncIterator[ClientSession]:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(
                read,
                write,
                message_handler=handler,
            ) as session,
        ):
            yield session

    return connect


def http_connector(
    url: str,
    *,
    transport: str,
    headers: dict[str, Any] | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> Connector:
    """Reopen HTTP sessions while reusing the configured authentication provider."""

    @asynccontextmanager
    async def connect(handler: MessageHandlerFnT) -> AsyncIterator[ClientSession]:
        async def check_session(response: httpx.Response) -> None:
            # bugfix: the SDK converts HTTP 404 to a normal JSON-RPC error. Raise
            # at the transport boundary so the owner fails queued work and reconnects.
            if response.request.method == "POST" and response.status_code == httpx.codes.NOT_FOUND:
                response.raise_for_status()

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            client = custom_httpx_client(headers, timeout, auth, verify_ssl)
            if transport == "streamablehttp":
                client.event_hooks["response"].append(check_session)
            return client

        with private_transport_logs():
            if transport == "sse":
                async with (
                    sse_client(url, headers=headers, auth=auth, httpx_client_factory=factory) as (
                        read,
                        write,
                    ),
                    ClientSession(read, write, message_handler=handler) as session,
                ):
                    yield session
            else:
                async with (
                    streamablehttp_client(
                        url, headers=headers, auth=auth, httpx_client_factory=factory
                    ) as (read, write, _),
                    ClientSession(read, write, message_handler=handler) as session,
                ):
                    yield session

    return connect


async def run_resilient_stdio(
    connector: Connector,
    settings: ResilienceSettings,
    *,
    cache_identity: str | None = None,
) -> None:
    """Expose the common facade on stdio without waiting for any backend."""
    async with (
        ResilientProxy(
            connector,
            settings,
            cache_identity=cache_identity,
        ) as proxy,
        stdio_server() as (read, write),
    ):
        await proxy.app.run(read, write, proxy.app.create_initialization_options())
