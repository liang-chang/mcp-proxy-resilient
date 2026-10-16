"""Shared recovery facade, independent of upstream/downstream transport choices."""

import asyncio
import contextlib
import json
import logging
import math
import weakref
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from mcp import types
from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions, request_ctx
from mcp.shared.exceptions import McpError

from . import APP_NAME, __version__
from .catalog import ADMIN_NAMES, Catalog, discover_catalog
from .tool_cache import ToolCache

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

logger = logging.getLogger(__name__)
Connector = Callable[[MessageHandlerFnT], AbstractAsyncContextManager[ClientSession]]
ForwardRequest = types.CallToolRequest | types.ReadResourceRequest | types.GetPromptRequest
ForwardResult = types.CallToolResult | types.ReadResourceResult | types.GetPromptResult


@dataclass(frozen=True)
class ResilienceSettings:
    """Bounds shared by both proxy directions."""

    connect_timeout: float = 10
    retry_interval: float = 5
    max_retry_interval: float = 60
    tool_cache_dir: Path | None = None

    def __post_init__(self) -> None:
        """Reject intervals that would cause retry loops or unbounded startup waits."""
        for value in (self.connect_timeout, self.retry_interval, self.max_retry_interval):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Recovery intervals must be positive and finite")


class _CatalogServer(Server[object]):
    def get_capabilities(
        self,
        notification_options: NotificationOptions,  # noqa: ARG002
        experimental_capabilities: dict[str, dict[str, Any]],  # noqa: ARG002
    ) -> types.ServerCapabilities:
        return types.ServerCapabilities(
            tools=types.ToolsCapability(listChanged=True),
            resources=types.ResourcesCapability(listChanged=True, subscribe=False),
            prompts=types.PromptsCapability(listChanged=True),
        )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
    )


def _auth_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return True
    return any(_auth_error(child) for child in getattr(exc, "exceptions", ()))


@dataclass
class _Call:
    request: ForwardRequest
    progress: Any
    result: asyncio.Future[ForwardResult]

    def fail(self, message: str) -> None:
        """Tools use isError; other methods use JSON-RPC errors."""
        if not self.result.done():
            if isinstance(self.request, types.CallToolRequest):
                self.result.set_result(_error(message))
            else:
                self.result.set_exception(
                    McpError(
                        types.ErrorData(
                            code=types.INTERNAL_ERROR,
                            message=message,
                        )
                    )
                )


class ResilientProxy:
    """Own one downstream connection and keep the upstream tools server alive.

    A single worker owns all transport context managers and serializes business calls.
    This is intentional: reconnect cannot race a call or replay an uncertain write.
    """

    def __init__(
        self,
        connector: Connector,
        settings: ResilienceSettings,
        *,
        cache_identity: str | None = None,
    ) -> None:
        """Create the facade without opening a downstream connection."""
        self.connector = connector
        self.settings = settings
        self.app = _CatalogServer(
            APP_NAME,
            version=__version__,
            instructions=(
                "Recoverable tools proxy. Use proxy_status to inspect availability and "
                "proxy_reconnect after starting or authorizing the downstream application."
            ),
        )
        self._cache = (
            ToolCache(settings.tool_cache_dir, cache_identity)
            if settings.tool_cache_dir is not None and cache_identity is not None
            else None
        )
        self.catalog = self._cache.load_catalog() if self._cache else Catalog()
        self._capabilities = types.ServerCapabilities()
        self._changed_kinds: set[str] = set()
        self.state = "DISCONNECTED"
        self.last_error: str | None = None
        self.attempts = 0
        self._clients: weakref.WeakSet[ServerSession] = weakref.WeakSet()
        self._jobs: asyncio.Queue[_Call | None] = asyncio.Queue(maxsize=32)
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._refresh = asyncio.Event()
        self._reconnect: asyncio.Future[dict[str, Any]] | None = None
        self._reconnect_attempt = 0
        self._closing = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._notifier: asyncio.Task[None] | None = None
        self.app.request_handlers[types.ListToolsRequest] = self._list_tools
        self.app.request_handlers[types.CallToolRequest] = self._call_tool
        self.app.request_handlers[types.ListResourcesRequest] = self._list_resources
        self.app.request_handlers[types.ListResourceTemplatesRequest] = (
            self._list_resource_templates
        )
        self.app.request_handlers[types.ListPromptsRequest] = self._list_prompts
        self.app.request_handlers[types.ReadResourceRequest] = self._forward_request
        self.app.request_handlers[types.GetPromptRequest] = self._forward_request

    @property
    def tools(self) -> list[types.Tool]:
        """Retain the tools-only accessor for existing callers."""
        return self.catalog.tools

    async def __aenter__(self) -> "ResilientProxy":  # noqa: PYI034
        """Start independent supervisor and notification tasks."""
        self._worker = asyncio.create_task(self._run())
        self._notifier = asyncio.create_task(self._notify())
        return self

    async def __aexit__(self, *args: object) -> None:
        """Stop all owned tasks and resolve outstanding requests."""
        self._closing.set()
        for task in (self._worker, self._notifier):
            if task is not None:
                task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(
                *(t for t in (self._worker, self._notifier) if t is not None),
                return_exceptions=True,
            )
        self.state = "DISCONNECTED"
        self._fail_queued()
        self._finish_reconnect()

    def status(self) -> dict[str, Any]:
        """Return diagnostics without configuration secrets or arbitrary exception text."""
        return {
            "state": self.state,
            "last_error": self.last_error,
            "connection_attempts": self.attempts,
            "tool_count": len(self.tools),
            "resource_count": len(self.catalog.resources),
            "resource_template_count": len(self.catalog.resource_templates),
            "prompt_count": len(self.catalog.prompts),
            "catalog_stale": self.state != "READY",
        }

    async def reconnect(self) -> dict[str, Any]:
        """Coalesce simultaneous reconnect requests into one attempt."""
        if self._reconnect is None:
            self._reconnect = asyncio.get_running_loop().create_future()
            self._reconnect_attempt = self.attempts + (self.state != "CONNECTING")
            if self.state == "READY":
                # Drop queued, unsent requests rather than executing them on a new session.
                self._fail_queued()
                self._jobs.put_nowait(None)
            # bugfix: joining an active attempt must not schedule a second attempt.
            if self.state != "CONNECTING":
                self._wake.set()
        return await asyncio.shield(self._reconnect)

    def _finish_reconnect(self) -> None:
        if self._reconnect is not None and (
            self.attempts >= self._reconnect_attempt or self._closing.is_set()
        ):
            self._reconnect.set_result(self.status())
            self._reconnect = None

    async def _list_tools(self, _request: types.ListToolsRequest) -> types.ServerResult:
        self._clients.add(request_ctx.get().session)
        admin = [
            types.Tool(
                name="proxy_status",
                description="Inspect downstream connection and last error.",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(
                name="proxy_reconnect",
                description="Reconnect after starting/authorizing the downstream application.",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(destructiveHint=False),
            ),
        ]
        return types.ServerResult(types.ListToolsResult(tools=admin + self.tools))

    async def _call_tool(self, request: types.CallToolRequest) -> types.ServerResult:
        ctx = request_ctx.get()
        self._clients.add(ctx.session)
        name = request.params.name
        if name in ADMIN_NAMES:
            if request.params.arguments:
                return types.ServerResult(_error("Management tools accept no arguments"))
            status = await self.reconnect() if name == "proxy_reconnect" else self.status()
            return types.ServerResult(
                types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(status))],
                    isError=name == "proxy_reconnect" and status["state"] != "READY",
                )
            )
        if self.state != "READY":
            return types.ServerResult(
                _error(
                    f"Downstream {self.state}; request not sent. Start/authorize the app, "
                    "then call proxy_reconnect.",
                )
            )
        if name not in {t.name for t in self.tools}:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Unknown tool"))
        meta = dict(request.params.meta) if request.params.meta else {}
        token = meta.get("progressToken")

        async def progress(value: float, total: float | None, message: str | None) -> None:
            if token is not None:
                await ctx.session.send_progress_notification(
                    token,
                    value,
                    total,
                    message,
                    related_request_id=str(ctx.request_id),
                )

        future: asyncio.Future[ForwardResult] = asyncio.get_running_loop().create_future()
        try:
            self._jobs.put_nowait(_Call(request, progress if token is not None else None, future))
        except asyncio.QueueFull:
            return types.ServerResult(_error("Proxy busy; request not sent"))
        return types.ServerResult(await future)

    async def _list_resources(self, _request: types.ListResourcesRequest) -> types.ServerResult:
        self._clients.add(request_ctx.get().session)
        return types.ServerResult(types.ListResourcesResult(resources=self.catalog.resources))

    async def _list_resource_templates(
        self,
        _request: types.ListResourceTemplatesRequest,
    ) -> types.ServerResult:
        self._clients.add(request_ctx.get().session)
        return types.ServerResult(
            types.ListResourceTemplatesResult(
                resourceTemplates=self.catalog.resource_templates,
            )
        )

    async def _list_prompts(self, _request: types.ListPromptsRequest) -> types.ServerResult:
        self._clients.add(request_ctx.get().session)
        return types.ServerResult(types.ListPromptsResult(prompts=self.catalog.prompts))

    async def _forward_request(
        self,
        request: types.ReadResourceRequest | types.GetPromptRequest,
    ) -> types.ServerResult:
        self._clients.add(request_ctx.get().session)
        if self.state != "READY":
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"Downstream {self.state}; request not sent",
                )
            )
        capability = (
            self._capabilities.resources
            if isinstance(request, types.ReadResourceRequest)
            else self._capabilities.prompts
        )
        if capability is None:
            raise McpError(
                types.ErrorData(
                    code=types.METHOD_NOT_FOUND,
                    message="Downstream capability unavailable",
                )
            )
        future: asyncio.Future[ForwardResult] = asyncio.get_running_loop().create_future()
        try:
            self._jobs.put_nowait(_Call(request, None, future))
        except asyncio.QueueFull:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message="Proxy busy; request not sent",
                )
            ) from None
        return types.ServerResult(await future)

    async def _message(self, message: object) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root,
            (
                types.ToolListChangedNotification,
                types.ResourceListChangedNotification,
                types.PromptListChangedNotification,
            ),
        ):
            # Do not send a request from the SDK receive loop: it must read the response.
            self._refresh.set()

    async def _discover(self, session: ClientSession) -> None:
        catalog = await discover_catalog(session, self._capabilities)
        changed = catalog.changed_kinds(self.catalog)
        self.catalog = catalog
        if changed:
            self._changed_kinds.update(changed)
            self._changed.set()
        if self._cache:
            self._cache.save_catalog(catalog)

    async def _notify_client(self, client: "ServerSession", kinds: set[str]) -> None:
        try:
            with anyio.fail_after(2):
                if "tools" in kinds:
                    await client.send_tool_list_changed()
                if "resources" in kinds:
                    await client.send_resource_list_changed()
                if "prompts" in kinds:
                    await client.send_prompt_list_changed()
        except Exception:  # noqa: BLE001 - isolate failed client notifications.
            self._clients.discard(client)

    async def _notify(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            kinds, self._changed_kinds = self._changed_kinds, set()
            # Independent sends keep one slow client from delaying healthy clients.
            async with anyio.create_task_group() as group:
                for client in list(self._clients):
                    group.start_soon(self._notify_client, client, kinds)

    def _fail_queued(self) -> None:
        while not self._jobs.empty():
            job = self._jobs.get_nowait()
            if job is not None and not job.result.done():
                job.fail("Downstream disconnected; queued request not sent")

    async def _serve(self, session: ClientSession) -> None:
        while True:
            if self._refresh.is_set():
                self._refresh.clear()
                with anyio.fail_after(self.settings.connect_timeout):
                    await self._discover(session)
            try:
                job = await asyncio.wait_for(self._jobs.get(), self.settings.retry_interval)
            except asyncio.TimeoutError:
                with anyio.fail_after(self.settings.connect_timeout):
                    await session.send_ping()
                continue
            if job is None:
                return
            if job.result.cancelled():
                continue
            await self._execute_call(session, job)

    def _call_failed(self, job: _Call) -> None:
        # bugfix: publish failure before replying so immediate reconnect requests
        # join the NEXT attempt rather than the failing session's cleanup.
        self.state = "RETRY_WAIT"
        job.fail(
            "Downstream call failed; outcome may be unknown. Not replayed. "
            "Check the app before retrying a write."
        )

    async def _execute_call(self, session: ClientSession, job: _Call) -> None:
        try:
            with anyio.CancelScope() as scope:

                def cancel(future: asyncio.Future[ForwardResult]) -> None:
                    if future.cancelled():
                        scope.cancel()

                job.result.add_done_callback(cancel)
                try:
                    result: ForwardResult
                    if isinstance(job.request, types.CallToolRequest):
                        result = await session.call_tool(
                            job.request.params.name,
                            job.request.params.arguments or {},
                            meta=dict(job.request.params.meta) if job.request.params.meta else None,
                            progress_callback=job.progress,
                            read_timeout_seconds=timedelta(seconds=300),
                        )
                    elif isinstance(job.request, types.ReadResourceRequest):
                        # bugfix: parsed upstream requests retain JSON-RPC envelope extras;
                        # rebuild the method envelope so the downstream owns its request id.
                        result = await session.send_request(
                            types.ClientRequest(
                                types.ReadResourceRequest(
                                    method="resources/read",
                                    params=job.request.params,
                                )
                            ),
                            types.ReadResourceResult,
                            request_read_timeout_seconds=timedelta(seconds=300),
                        )
                    else:
                        result = await session.send_request(
                            types.ClientRequest(
                                types.GetPromptRequest(
                                    method="prompts/get",
                                    params=job.request.params,
                                )
                            ),
                            types.GetPromptResult,
                            request_read_timeout_seconds=timedelta(seconds=300),
                        )
                finally:
                    job.result.remove_done_callback(cancel)
                if not job.result.done():
                    job.result.set_result(result)
        except McpError as exc:
            # bugfix: a valid JSON-RPC error is not evidence of transport failure.
            # Only the SDK's connection-closed/response-timeout errors invalidate it.
            if exc.error.code in {types.CONNECTION_CLOSED, httpx.codes.REQUEST_TIMEOUT}:
                self._call_failed(job)
                raise
            if not job.result.done():
                job.result.set_exception(exc)
        except Exception:
            self._call_failed(job)
            raise
        finally:
            job.fail("Call interrupted; outcome may be unknown")

    async def _run(self) -> None:
        delay = self.settings.retry_interval
        try:
            while not self._closing.is_set():
                self._wake.clear()
                self.state = "CONNECTING"
                self.attempts += 1
                try:
                    # bugfix: transport scopes must be opened/closed by the same task;
                    # keeping the deadline scope outside them also makes timeout cleanup safe.
                    with anyio.fail_after(self.settings.connect_timeout) as deadline:
                        async with self.connector(self._message) as session:
                            init = await session.initialize()
                            self._capabilities = init.capabilities
                            await self._discover(session)
                            deadline.deadline = math.inf
                            self.state = "READY"
                            self.last_error = None
                            delay = self.settings.retry_interval
                            self._finish_reconnect()
                            await self._serve(session)
                    self.state = "DISCONNECTED"
                except Exception as exc:  # noqa: BLE001
                    self.state = "AUTH_REQUIRED" if _auth_error(exc) else "RETRY_WAIT"
                    # Exception strings can contain URLs, tokens or subprocess environment.
                    self.last_error = (
                        "HTTP authorization required" if _auth_error(exc) else type(exc).__name__
                    )
                    logger.warning("Downstream unavailable: %s", self.last_error)
                    self._finish_reconnect()
                self._fail_queued()
                if self._closing.is_set():
                    break
                if self.state == "DISCONNECTED":
                    continue  # Explicit reset: cleanup completed before the next open.
                if self.state == "AUTH_REQUIRED":
                    await self._wake.wait()
                else:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), delay)
                    delay = min(delay * 2, self.settings.max_retry_interval)
        finally:
            self.state = "DISCONNECTED"
            self._fail_queued()
