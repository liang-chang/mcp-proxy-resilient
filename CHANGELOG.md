# Changelog

## 0.13.0+resilient.2 — first independent release (unreleased)

Based on upstream mcp-proxy 0.13.0. The original source history and MIT license are retained.

- Independent stdio/HTTP frontend selection, including stdio-to-stdio and HTTP-to-HTTP.
- Named stdio frontend selection with --server; unselected backends are not started.
- Shared discovery, atomic catalog snapshots, cache, and change notifications for tools,
  resources, resource templates, and prompts.
- Live resources/read and prompts/get forwarding through the existing serial request queue.
- HTTP 404 session-expiry recovery and credential-safe resilient HTTP logging.
- Windows x64 portable distribution with bundled Python, dependency notices, and checksums.
- GitHub-only release automation, bilingual documentation, and Windows/Ubuntu source CI.

Resilient mode does not implement subscriptions, completion, sampling, or elicitation.
Windows is the primary locally verified platform; macOS support remains experimental.

## 0.13.0+resilient.1 — local development build

- Initial tools-only recoverable facade, independent of backend availability.
- Management tools proxy_status and proxy_reconnect, bounded startup, retry/backoff,
  optional catalog cache, and no automatic request replay.
- Multiple named stdio backends served over SSE/Streamable HTTP and the reverse direction.

For upstream releases, see [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy/releases).
