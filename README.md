# mcp-proxy-resilient

A recoverable Model Context Protocol (MCP) proxy for stdio, SSE, and Streamable HTTP.
The client can initialize while a backend is offline; the proxy discovers its tools,
resources, and prompts after the application starts or authorization is restored.

Independently maintained by [liang-chang](https://github.com/liang-chang), based on
[sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy). The original MIT license,
author attribution, and Git history are retained.

[中文说明](README.zh-CN.md) · [Releases](https://github.com/liang-chang/mcp-proxy-resilient/releases) · [Changelog](CHANGELOG.md)

## What it does

- All four stdio/HTTP frontend/backend combinations use the same recovery core.
- A failed backend does not prevent the proxy's MCP handshake or stop other named servers.
- Tools, resources, resource templates, and prompts are discovered across all pages and
  published as one complete catalog.
- Changed catalogs produce the corresponding list_changed notifications.
- proxy_status reports availability; proxy_reconnect reconnects after starting or
  authorizing the backend.
- Optional disk persistence retains catalog metadata across proxy restarts.
- Business requests are never automatically replayed after failure or cancellation.

The current version is 0.13.0+resilient.2. Pass --resilient to enable these behaviors.
Without it, the original two transport directions retain their legacy startup behavior.

## Install

### Windows portable distribution

Download the Windows x64 ZIP from [Releases](https://github.com/liang-chang/mcp-proxy-resilient/releases)
once a release is available, verify its SHA256, and extract the entire directory.
The bundle includes Python and runtime dependencies; the target machine needs no Python,
pip, or uv installation. Keep _internal and dependency-notices next to the executable.

~~~powershell
.\mcp-proxy-resilient.exe --version
.\mcp-proxy-resilient.exe --help
~~~

Backend programs and their runtimes (Node.js, Java, Python, etc.), UI applications, and
their credentials remain your responsibility. Prefer absolute backend command paths.

### Python installation

Python 3.10+ is required for a source or wheel installation. Install from this repository
with [uv](https://docs.astral.sh/uv/):

~~~shell
uv tool install "git+https://github.com/liang-chang/mcp-proxy-resilient.git"
mcp-proxy-resilient --version
~~~

Or install the wheel attached to a release:

~~~shell
python -m pip install ./mcp_proxy_resilient-0.13.0+resilient.2-py3-none-any.whl
~~~

The Python distribution and executable are named mcp-proxy-resilient; the import package
remains mcp_proxy. Use a separate environment from the upstream distribution.
This project currently distributes through GitHub; PyPI and container registry publication
are outside the initial release scope.

## Choose the frontend and backend

The frontend faces your MCP client; the backend is the actual MCP service.

| Frontend | Backend | Example |
|---|---|---|
| HTTP | stdio | --resilient --frontend http --port 18080 -- your-server |
| stdio | HTTP | --resilient --frontend stdio --transport streamablehttp URL |
| stdio | stdio | --resilient --frontend stdio -- your-server |
| HTTP | HTTP | --resilient --frontend http --port 18080 --transport streamablehttp URL |

HTTP frontends offer both /mcp (Streamable HTTP) and /sse (SSE).
HTTP backends accept --transport sse or --transport streamablehttp; the default is SSE.
When --frontend is omitted, a URL selects stdio and a command/named configuration selects HTTP.
Same-transport modes require --resilient. Explicit stdio rejects proxy --port/--sse-port;
put a child program's flags after --.

### Named Servers

Copy [servers.example.json](servers.example.json) to servers.json and replace the
placeholder command with your actual backend. It demonstrates both stdio and HTTP entries:

~~~json
{
  "mcpServers": {
    "local": {
      "command": "your-mcp-server",
      "args": [],
      "env": {}
    },
    "remote": {
      "enabled": false,
      "url": "http://127.0.0.1:19090/mcp",
      "transport": "streamablehttp",
      "headers": {}
    }
  }
}
~~~

Start a local HTTP frontend:

~~~shell
mcp-proxy-resilient --resilient --frontend http --host 127.0.0.1 --port 18080 --named-server-config servers.json
~~~

Clients connect to http://127.0.0.1:18080/servers/local/mcp or /servers/local/sse.
Each named backend reconnects independently. The HTTP proxy must already be listening
when the client initializes; backend availability is independent of that requirement.

For a stdio client, configure the following command and arguments as its MCP server:

~~~shell
mcp-proxy-resilient --resilient --frontend stdio --named-server-config servers.json --server local
~~~

The client starts this proxy itself; no HTTP listener is needed. Each stdio process selects
exactly one enabled named backend and does not start unselected backends.
The Windows bundle also includes start-proxy.cmd: no arguments starts HTTP on port 18080,
http PORT selects another port, and stdio NAME selects one backend.

Resilient configuration requires exactly one target per entry: command/args/env/cwd or
url/transport/headers/verify_ssl. HTTP entries require an explicit transport.
env and headers contain literal strings; variable substitution is not supported.
Changing configuration requires a proxy restart, not just proxy_reconnect.

## Recovery and safety

On a first offline start without a cache, tools/list offers only proxy_status and
proxy_reconnect; resource, template, and prompt lists are empty. Successful discovery
replaces the complete catalog. Partial or malformed discovery retains the old catalog
with catalog_stale=true. A backend losing a capability clears that category.

Tools, resources, and prompts declare listChanged=true. Template changes use
notifications/resources/list_changed. Only changed categories are announced; a reconnect
with an identical catalog does not emit an unconditional notification.
Clients must support rediscovery to see new definitions in the same session.
Stateless HTTP cannot provide durable server-initiated notifications; use the default
stateful HTTP, SSE, or request the lists explicitly.

Connection/initialization/discovery timeout is 10 seconds (--connect-timeout).
Availability retries start at 5 seconds (--retry-interval) and back off to at most 60 seconds.
Recognizable HTTP 401/403 enters AUTH_REQUIRED and waits for proxy_reconnect.
Streamable HTTP session termination (HTTP 404) invalidates the connection.
The proxy does not launch external application windows, log in, or refresh static tokens/env.

Calls, resources/read, and prompts/get share a serial queue per backend (32 pending requests).
An active request may wait up to 300 seconds; reconnect waits for it to finish.
Valid backend protocol/business errors are preserved. Local unavailability/busy/interruption
returns isError for tools and a JSON-RPC error for resources/prompts.
Failed or cancelled requests are never replayed automatically; cancellation cannot guarantee
that a backend operation stopped or its side effects were rolled back.

Resource subscriptions/content-update notifications, completion, sampling, and elicitation
are not implemented in resilient mode.

## Cache and privacy

Add --tool-cache-dir ./tool-cache for metadata persistence; otherwise the catalog is in memory.
The legacy flag name now covers all four catalog lists. v1 tools-only caches are readable;
new writes use v2. Corrupt or oversized caches are ignored (4 MiB limit).

Cache filenames hash connection identity. Credentials, tool results, resource bodies, and
prompt messages are not persisted; dynamic-auth providers do not use disk catalog caches.
Metadata itself can contain sensitive descriptions or URIs, so keep the directory private.
A cache is not proof of authorization or backend availability.
Resilient HTTP connections suppress raw third-party wire logs, including DEBUG, to avoid
leaking URL tokens, headers, response data, and exception details.
Bind local HTTP proxies to 127.0.0.1 unless you have designed authentication and access controls.

## Development and validation

~~~shell
uv sync --locked --python 3.13
uv run --locked pytest -q
uv run --locked mypy src
uv run --locked ruff check src tests scripts/portable_entry.py
uv run --locked ruff format --check src tests scripts/portable_entry.py
uv build
~~~

Windows is the primary locally validated platform, including the standalone x64 executable.
CI is configured to test source installations on Windows and Ubuntu with Python 3.10 and 3.13.
That configuration is not itself evidence of a successful remote CI run. macOS is experimental
and has not been validated locally; no macOS/Linux standalone binary is currently supplied.

See [Windows deployment](docs/DEPLOYMENT.zh-CN.md) and [release maintenance](docs/RELEASING.md).
The reusable CI workflow gates tagged releases; the release job builds and tests a relocated
portable executable before publishing the ZIP, wheel, sdist, and checksums to GitHub.
No cloud credentials or interactive terminal applications are needed for automated tests.
