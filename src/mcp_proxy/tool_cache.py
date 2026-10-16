"""Optional schema-only cache; never persists credentials or tool results."""

import hashlib
import json
import logging
import tempfile
from pathlib import Path

from mcp import types

from .catalog import Catalog

logger = logging.getLogger(__name__)
MAX_CACHE_BYTES = 4 * 1024 * 1024
CACHE_VERSION = 2


class ToolCache:
    """Atomically store one catalog under a hashed connection identity."""

    def __init__(self, directory: Path, identity: str) -> None:
        """Derive a filename without storing the connection configuration."""
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.path = directory / f"{digest}.json"

    def load(self) -> list[types.Tool]:
        """Keep the tools-only API available to older callers."""
        return self.load_catalog().tools

    def load_catalog(self) -> Catalog:
        """Ignore missing, invalid or oversized cache files."""
        try:
            with self.path.open("rb") as file:
                data = file.read(MAX_CACHE_BYTES + 1)
            if len(data) > MAX_CACHE_BYTES:
                return Catalog()
            payload = json.loads(data)
            if payload["version"] not in (1, CACHE_VERSION):
                return Catalog()
            catalog = Catalog(tools=[types.Tool.model_validate(item) for item in payload["tools"]])
            if payload["version"] == CACHE_VERSION:
                catalog.resources = [types.Resource.model_validate(r) for r in payload["resources"]]
                catalog.resource_templates = [
                    types.ResourceTemplate.model_validate(r) for r in payload["resource_templates"]
                ]
                catalog.prompts = [types.Prompt.model_validate(p) for p in payload["prompts"]]
            catalog.validate()
        except (OSError, ValueError, KeyError, TypeError):
            return Catalog()
        return catalog

    def save(self, tools: list[types.Tool]) -> None:
        """Keep the tools-only API available to older callers."""
        self.save_catalog(Catalog(tools=tools))

    def save_catalog(self, catalog: Catalog) -> None:
        """Cache failures never change backend availability."""
        temporary: str | None = None
        try:
            catalog.validate()
            # bugfix: MCP aliases such as _meta must round-trip as protocol fields, not extras.
            data = json.dumps(
                {
                    "version": CACHE_VERSION,
                    "tools": [t.model_dump(mode="json", by_alias=True) for t in catalog.tools],
                    "resources": [
                        r.model_dump(mode="json", by_alias=True) for r in catalog.resources
                    ],
                    "resource_templates": [
                        r.model_dump(mode="json", by_alias=True) for r in catalog.resource_templates
                    ],
                    "prompts": [p.model_dump(mode="json", by_alias=True) for p in catalog.prompts],
                }
            )
            if len(data.encode()) > MAX_CACHE_BYTES:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as file:
                temporary = file.name
                file.write(data)
            Path(temporary).replace(self.path)
        except (OSError, ValueError):
            logger.warning("Could not persist tool catalog")
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary catalog cache")
