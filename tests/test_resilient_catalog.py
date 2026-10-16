"""Directory recovery and forwarding over real MCP sessions."""
# ruff: noqa: SIM117, SLF001

import asyncio

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session as connected

from mcp_proxy.resilient import ResilienceSettings, ResilientProxy, _Call
from tests.test_resilient import Backend


class CatalogBackend(Backend):
    """Mutable resource/prompt backend without tools."""

    def __init__(self) -> None:
        """Install raw request handlers for deterministic protocol-level tests."""
        super().__init__()
        self.server.request_handlers.pop(types.ListToolsRequest)
        self.server.request_handlers.pop(types.CallToolRequest)
        self.title = "first"
        self.template = "file:///item/{id}"
        self.session = None
        self.fail = False
        self.arguments = None

        async def resources(_request: types.ListResourcesRequest) -> types.ServerResult:
            self.session = request_ctx.get().session
            return types.ServerResult(
                types.ListResourcesResult(
                    resources=[
                        types.Resource(name="item", uri="file:///item/1"),
                    ]
                )
            )

        async def templates(_request: types.ListResourceTemplatesRequest) -> types.ServerResult:
            return types.ServerResult(
                types.ListResourceTemplatesResult(
                    resourceTemplates=[
                        types.ResourceTemplate(name="items", uriTemplate=self.template),
                    ]
                )
            )

        async def prompts(_request: types.ListPromptsRequest) -> types.ServerResult:
            if self.fail:
                raise McpError(
                    types.ErrorData(code=types.INTERNAL_ERROR, message="discovery failed")
                )
            return types.ServerResult(
                types.ListPromptsResult(
                    prompts=[
                        types.Prompt(name="greet", description=self.title),
                    ]
                )
            )

        async def read(request: types.ReadResourceRequest) -> types.ServerResult:
            self.calls += 1
            return types.ServerResult(
                types.ReadResourceResult(
                    contents=[
                        types.TextResourceContents(uri=request.params.uri, text="body"),
                    ]
                )
            )

        async def prompt(request: types.GetPromptRequest) -> types.ServerResult:
            self.arguments = request.params.arguments
            if request.params.name == "invalid":
                raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="bad prompt"))
            return types.ServerResult(
                types.GetPromptResult(
                    messages=[
                        types.PromptMessage(
                            role="user", content=types.TextContent(type="text", text="hello")
                        ),
                    ]
                )
            )

        self.server.request_handlers.update(
            {
                types.ListResourcesRequest: resources,
                types.ListResourceTemplatesRequest: templates,
                types.ListPromptsRequest: prompts,
                types.ReadResourceRequest: read,
                types.GetPromptRequest: prompt,
            }
        )


async def test_offline_catalog_capabilities_and_late_recovery() -> None:
    """Initialize offline, then discover and forward all supported methods."""
    backend = CatalogBackend()
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        async with connected(proxy.app) as client:
            init = await client.initialize()
            assert init.capabilities.resources.listChanged is True
            assert init.capabilities.resources.subscribe is not True
            assert init.capabilities.prompts.listChanged is True
            assert init.capabilities.tools.listChanged is True
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resourceTemplates == []
            assert (await client.list_prompts()).prompts == []
            with pytest.raises(McpError) as error:
                await client.read_resource("file:///item/2")
            assert error.value.error.code == types.INTERNAL_ERROR
            backend.online = True
            await proxy.reconnect()
            assert proxy.status()["state"] == "READY"
            assert proxy.status()["resource_count"] == 1
            assert proxy.status()["resource_template_count"] == 1
            assert proxy.status()["prompt_count"] == 1
            assert proxy.status()["tool_count"] == 0
            assert not proxy.status()["catalog_stale"]
            assert len((await client.list_resources()).resources) == 1
            assert len((await client.list_resource_templates()).resourceTemplates) == 1
            assert len((await client.list_prompts()).prompts) == 1
            assert (await client.read_resource("file:///item/2")).contents[0].text == "body"
            assert (await client.get_prompt("greet", {"name": "Ada"})).messages[
                0
            ].content.text == "hello"
            assert backend.arguments == {"name": "Ada"}
            with pytest.raises(McpError, match="bad prompt") as error:
                await client.get_prompt("invalid")
            assert error.value.error.code == types.INVALID_PARAMS
            assert proxy.status()["state"] == "READY"


@pytest.mark.parametrize("category", ["resources", "prompts"])
async def test_non_tool_client_gets_category_notifications(category: str) -> None:
    """Resources/prompt users receive changes without ever listing tools."""
    backend = CatalogBackend()
    backend.online = True
    messages = asyncio.Queue()

    async def receive(message: object) -> None:
        if isinstance(message, types.ServerNotification):
            messages.put_nowait(type(message.root))

    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=0.02)) as proxy:
        await proxy.reconnect()
        async with connected(proxy.app, message_handler=receive) as client:
            await getattr(client, "list_" + category)()
            backend.title = "changed"
            await backend.session.send_prompt_list_changed()
            assert await asyncio.wait_for(messages.get(), 2) is types.PromptListChangedNotification
            backend.template = "file:///new/{id}"
            await backend.session.send_resource_list_changed()
            assert (
                await asyncio.wait_for(messages.get(), 2) is types.ResourceListChangedNotification
            )
            await proxy.reconnect()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(messages.get(), 0.05)
            backend.server.request_handlers.pop(types.ListPromptsRequest)
            await proxy.reconnect()
            assert await asyncio.wait_for(messages.get(), 2) is types.PromptListChangedNotification
            assert (await client.list_prompts()).prompts == []
            with pytest.raises(McpError) as error:
                await client.get_prompt("greet")
            assert error.value.error.code == types.METHOD_NOT_FOUND


async def test_failed_refresh_keeps_complete_snapshot_stale() -> None:
    """A later discovery failure must not publish earlier successful pages."""
    backend = CatalogBackend()
    backend.online = True
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=0.02)) as proxy:
        await proxy.reconnect()
        backend.template = "file:///new/{id}"
        backend.fail = True
        await backend.session.send_resource_list_changed()
        with anyio.fail_after(2):
            while not proxy.status()["catalog_stale"]:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        async with connected(proxy.app) as client:
            templates = (await client.list_resource_templates()).resourceTemplates
            assert templates[0].uriTemplate == "file:///item/{id}"


async def test_queue_full_disconnect_and_no_replay() -> None:
    """One uncertain read and an unsent prompt fail without being replayed."""
    backend = CatalogBackend()
    backend.online = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(_request: types.ReadResourceRequest) -> types.ServerResult:
        backend.calls += 1
        entered.set()
        await release.wait()
        raise McpError(types.ErrorData(code=types.CONNECTION_CLOSED, message="gone"))

    backend.server.request_handlers[types.ReadResourceRequest] = read
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        proxy._jobs = asyncio.Queue(maxsize=1)
        await proxy.reconnect()
        async with connected(proxy.app) as client:
            active = asyncio.create_task(client.read_resource("file:///item/1"))
            await asyncio.wait_for(entered.wait(), 2)
            queued = asyncio.create_task(client.get_prompt("greet"))
            with anyio.fail_after(2):
                while proxy._jobs.empty():  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            with pytest.raises(McpError, match="busy") as error:
                await client.get_prompt("greet")
            assert error.value.error.code == types.INTERNAL_ERROR
            release.set()
            failures = await asyncio.gather(active, queued, return_exceptions=True)
            assert all(isinstance(failure, McpError) for failure in failures)
            assert "Not replayed" in str(failures[0])
            assert "not sent" in str(failures[1])
            await proxy.reconnect()
            assert backend.calls == 1
            assert backend.arguments is None


async def test_cancelled_and_closing_requests_resolve() -> None:
    """The serial worker releases a cancelled request and resolves pending work on exit."""
    backend = CatalogBackend()
    backend.online = True
    entered = asyncio.Event()

    async def read(_request: types.ReadResourceRequest) -> None:
        entered.set()
        await asyncio.Event().wait()

    backend.server.request_handlers[types.ReadResourceRequest] = read
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        await proxy.reconnect()
        request = types.ReadResourceRequest(
            method="resources/read", params=types.ReadResourceRequestParams(uri="file:///item/1")
        )
        cancelled = asyncio.get_running_loop().create_future()
        proxy._jobs.put_nowait(_Call(request, None, cancelled))
        await asyncio.wait_for(entered.wait(), 2)
        cancelled.cancel()
        entered.clear()
        pending = asyncio.get_running_loop().create_future()
        proxy._jobs.put_nowait(_Call(request, None, pending))
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.get_running_loop().create_future()
        proxy._jobs.put_nowait(_Call(request, None, queued))
    assert cancelled.cancelled()
    assert pending.done()
    assert queued.done()
    assert isinstance(pending.exception(), McpError)
    assert isinstance(queued.exception(), McpError)
    assert backend.active == 0


async def test_slow_notification_client_does_not_delay_healthy_client() -> None:
    """Client sends run independently; a hung client cannot hold up a healthy one."""
    backend = CatalogBackend()
    slow_started, healthy_received = asyncio.Event(), asyncio.Event()

    class Slow:
        async def send_prompt_list_changed(self) -> None:
            slow_started.set()
            await asyncio.Event().wait()

    class Healthy:
        async def send_prompt_list_changed(self) -> None:
            healthy_received.set()

    slow, healthy = Slow(), Healthy()
    async with ResilientProxy(backend.connect, ResilienceSettings(retry_interval=60)) as proxy:
        proxy._clients.update([slow, healthy])
        proxy._changed_kinds.add("prompts")
        proxy._changed.set()
        await asyncio.wait_for(slow_started.wait(), 1)
        await asyncio.wait_for(healthy_received.wait(), 0.1)
