"""Capability-aware, atomic metadata snapshots shared by every proxy transport."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError

MAX_CATALOG_PAGES = 100
ADMIN_NAMES = {"proxy_status", "proxy_reconnect"}


@dataclass
class Catalog:
    """Discovery metadata only; never contains read/get/call results."""

    tools: list[types.Tool] = field(default_factory=list)
    resources: list[types.Resource] = field(default_factory=list)
    resource_templates: list[types.ResourceTemplate] = field(default_factory=list)
    prompts: list[types.Prompt] = field(default_factory=list)

    def validate(self) -> None:
        """Reject ambiguous identities before a snapshot is cached or published."""
        groups = [
            [t.name for t in self.tools],
            [str(r.uri) for r in self.resources],
            [t.uriTemplate for t in self.resource_templates],
            [p.name for p in self.prompts],
        ]
        if ADMIN_NAMES.intersection(groups[0]) or any(len(set(g)) != len(g) for g in groups):
            raise ValueError("Duplicate or reserved catalog identifier")

    def changed_kinds(self, previous: "Catalog") -> set[str]:
        """Resource templates share the resource-list invalidation notification."""
        changes = set()
        if self.tools != previous.tools:
            changes.add("tools")
        if (self.resources, self.resource_templates) != (
            previous.resources,
            previous.resource_templates,
        ):
            changes.add("resources")
        if self.prompts != previous.prompts:
            changes.add("prompts")
        return changes


async def _pages(fetch: Callable[[str | None], Awaitable[Any]], field_name: str) -> list[Any]:
    result: list[Any] = []
    cursor = None
    seen: set[str] = set()
    for _ in range(MAX_CATALOG_PAGES):
        page = await fetch(cursor)
        result.extend(getattr(page, field_name))
        cursor = page.nextCursor
        if cursor is None:
            return result
        if cursor in seen:
            raise ValueError("Invalid catalog pagination")
        seen.add(cursor)
    raise ValueError("Catalog pagination limit exceeded")


async def discover_catalog(
    session: ClientSession, capabilities: types.ServerCapabilities
) -> Catalog:
    """Build privately, publishing nothing until every supported directory succeeds."""
    catalog = Catalog()
    if capabilities.tools is not None:
        catalog.tools = await _pages(lambda c: session.list_tools(cursor=c), "tools")
    if capabilities.resources is not None:
        catalog.resources = await _pages(lambda c: session.list_resources(cursor=c), "resources")
        try:
            catalog.resource_templates = await _pages(
                lambda c: session.list_resource_templates(cursor=c),
                "resourceTemplates",
            )
        except McpError as exc:
            # Older resource servers may expose concrete resources but no template method.
            if exc.error.code != types.METHOD_NOT_FOUND:
                raise
    if capabilities.prompts is not None:
        catalog.prompts = await _pages(lambda c: session.list_prompts(cursor=c), "prompts")
    catalog.validate()
    return catalog
