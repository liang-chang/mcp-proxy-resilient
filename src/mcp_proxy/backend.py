"""Backend descriptions independent of the client-facing transport."""

from dataclasses import dataclass, field
from typing import Literal

import httpx
from mcp.client.stdio import StdioServerParameters


@dataclass
class HttpServerParameters:
    """HTTP connection settings; authentication providers are kept in memory."""

    url: str
    transport: Literal["sse", "streamablehttp"]
    headers: dict[str, str] = field(default_factory=dict)
    auth: httpx.Auth | None = None
    verify_ssl: bool | str | None = None


BackendParameters = StdioServerParameters | HttpServerParameters
