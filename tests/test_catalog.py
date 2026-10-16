"""Catalog discovery and persistence at real MCP and filesystem boundaries."""
# ruff: noqa: PLR2004

import json
from pathlib import Path

import pytest
from mcp import types
from mcp.server import Server
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session as connected

from mcp_proxy.catalog import Catalog, discover_catalog
from mcp_proxy.tool_cache import MAX_CACHE_BYTES, ToolCache


async def test_catalog_capability_pagination_and_atomicity() -> None:
    """A late failing page never becomes a partially published snapshot."""
    server = Server("catalog")
    fail = False

    async def resources(req: types.ListResourcesRequest) -> types.ServerResult:
        cursor = req.params.cursor if req.params else None
        if cursor and fail:
            raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message="broken page"))
        return types.ServerResult(
            types.ListResourcesResult(
                resources=[
                    types.Resource(name=cursor or "first", uri=f"test:///{cursor or 'first'}")
                ],
                nextCursor=None if cursor else "second",
            )
        )

    server.request_handlers[types.ListResourcesRequest] = resources
    caps = types.ServerCapabilities(resources=types.ResourcesCapability())
    async with connected(server) as client:
        result = await discover_catalog(client, caps)
        assert [r.name for r in result.resources] == ["first", "second"]
        assert result.tools == result.prompts == result.resource_templates == []
        fail = True
        with pytest.raises(McpError, match="broken page"):
            await discover_catalog(client, caps)
        assert [r.name for r in result.resources] == ["first", "second"]
        assert await discover_catalog(client, types.ServerCapabilities()) == Catalog()


@pytest.mark.parametrize("mode", ["duplicate", "cursor", "pages"])
async def test_discovery_rejects_invalid_pagination(mode: str) -> None:
    """Malformed downstream pages must not loop forever or publish duplicate names."""
    server = Server("bad-pages")

    async def prompts(req: types.ListPromptsRequest) -> types.ServerResult:
        cursor = req.params.cursor if req.params else None
        number = int(cursor or "0")
        return types.ServerResult(
            types.ListPromptsResult(
                prompts=[types.Prompt(name="same" if mode == "duplicate" else str(number))],
                nextCursor=("1" if mode == "cursor" else str(number + 1))
                if mode != "duplicate" or number == 0
                else None,
            )
        )

    server.request_handlers[types.ListPromptsRequest] = prompts
    async with connected(server) as client:
        with pytest.raises(ValueError, match=r"Duplicate|pagination|pages"):
            await discover_catalog(
                client, types.ServerCapabilities(prompts=types.PromptsCapability())
            )


def test_catalog_cache_v1_v2_and_no_content(tmp_path: Path) -> None:
    """Old tool schemas survive upgrade, while new catalogs retain only metadata."""
    cache = ToolCache(tmp_path, "backend-a")
    cache.path.write_text(json.dumps({"version": 1, "tools": [{"name": "old", "inputSchema": {}}]}))
    assert cache.load_catalog().tools[0].name == "old"
    assert cache.load_catalog().resources == []
    catalog = Catalog(
        resources=[types.Resource(name="doc", uri="test:///doc")],
        resource_templates=[types.ResourceTemplate(name="item", uriTemplate="test:///{id}")],
        prompts=[types.Prompt(name="review", arguments=[types.PromptArgument(name="code")])],
    )
    cache.save_catalog(catalog)
    payload = json.loads(cache.path.read_text())
    assert payload["version"] == 2
    assert set(payload) == {"version", "tools", "resources", "resource_templates", "prompts"}
    assert cache.load_catalog() == catalog
    assert ToolCache(tmp_path, "backend-b").load_catalog() == Catalog()
    assert catalog.changed_kinds(Catalog()) == {"resources", "prompts"}
    assert catalog.changed_kinds(catalog) == set()


@pytest.mark.parametrize("payload", [None, [], {"version": 7}, {"version": 2, "tools": None}])
def test_bad_catalog_cache_ignored(tmp_path: Path, payload: object) -> None:
    """Invalid persistence cannot prevent the proxy from starting."""
    cache = ToolCache(tmp_path, "backend")
    cache.path.write_text(json.dumps(payload))
    assert cache.load_catalog() == Catalog()


@pytest.mark.parametrize("kind", ["tools", "resources", "resource_templates", "prompts"])
def test_duplicate_cached_identifiers_ignored(tmp_path: Path, kind: str) -> None:
    """Each directory has its own unique identity field."""
    cache = ToolCache(tmp_path, "backend")
    payload = {"version": 2, "tools": [], "resources": [], "resource_templates": [], "prompts": []}
    item = {"name": "same", "inputSchema": {}, "uri": "test:///same", "uriTemplate": "test:///{id}"}
    payload[kind] = [item, item]
    cache.path.write_text(json.dumps(payload))
    assert cache.load_catalog() == Catalog()


def test_cache_limits_and_io_failure(tmp_path: Path) -> None:
    """Oversized or unwritable caches leave the caller's catalog untouched."""
    cache = ToolCache(tmp_path, "backend")
    cache.path.write_bytes(b"x" * (MAX_CACHE_BYTES + 1))
    assert cache.load_catalog() == Catalog()
    catalog = Catalog(prompts=[types.Prompt(name="p", description="x" * MAX_CACHE_BYTES)])
    cache.save_catalog(catalog)
    assert cache.load_catalog() == Catalog()
    blocker = tmp_path / "file-not-directory"
    blocker.touch()
    ToolCache(blocker, "backend").save_catalog(Catalog())
    assert catalog.prompts[0].name == "p"
