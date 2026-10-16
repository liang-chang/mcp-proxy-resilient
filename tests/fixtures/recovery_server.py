"""Subprocess fixture: unavailable until a marker exists; crash after a write on demand."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

marker = Path(sys.argv[1])
if not marker.exists():
    sys.exit(1)

app = Server("recovery-fixture")


@app.list_tools()
async def tools() -> list[types.Tool]:
    """Advertise the crash-after-write tool."""
    return [types.Tool(name="write_once", inputSchema={"type": "object"})]


@app.call_tool()
async def call(_name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Record a side effect before optionally terminating without a response."""
    output = marker.with_suffix(".calls")
    with output.open("a") as file:
        file.write("write\n")
    if arguments.get("crash"):
        os._exit(1)
    return [types.TextContent(type="text", text="ok")]


async def main() -> None:
    """Run the fixture over real process stdin/stdout."""
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


@app.list_resources()
async def resources() -> list[types.Resource]:
    """Expose metadata independently of resource bodies."""
    return [types.Resource(name="item", uri="file:///item/1")]


@app.list_resource_templates()
async def templates() -> list[types.ResourceTemplate]:
    """Allow reading an expanded URI not present in resources/list."""
    return [types.ResourceTemplate(name="items", uriTemplate="file:///item/{id}")]


@app.list_prompts()
async def prompts() -> list[types.Prompt]:
    """Expose one prompt."""
    return [types.Prompt(name="greet")]


async def read(request: types.ReadResourceRequest) -> types.ServerResult:
    """Return content through the raw protocol handler."""
    return types.ServerResult(
        types.ReadResourceResult(
            contents=[
                types.TextResourceContents(uri=request.params.uri, text="body"),
            ]
        )
    )


@app.get_prompt()
async def prompt(_name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    """Check argument forwarding across the process boundary."""
    assert arguments == {"name": "Ada"}
    return types.GetPromptResult(
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text="hello")),
        ]
    )


app.request_handlers[types.ReadResourceRequest] = read
asyncio.run(main())
