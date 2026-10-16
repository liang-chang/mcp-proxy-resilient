# 更新日志

## 0.13.0+resilient.2 — 首个独立发布版本（2026-10-16）

基于上游 mcp-proxy 0.13.0，保留原有源码历史和 MIT 许可证。

- 独立选择 stdio/HTTP 前端，新增 stdio-to-stdio 和 HTTP-to-HTTP 支持。
- stdio 前端可通过 `--server` 选择 Named Server；未选中的后端不会启动。
- tools、resources、资源模板和 prompts 共用发现、原子目录快照、缓存及变更通知机制。
- `resources/read` 和 `prompts/get` 通过现有串行请求队列实时转发。
- 支持 HTTP 404 会话过期恢复，并保护可恢复模式下 HTTP 日志中的凭据信息。
- 提供 Windows x64 便携包，内置 Python、依赖声明及校验文件。
- 提供仅面向 GitHub 的自动发布流程、中英文文档和 Windows/Ubuntu 源码 CI。

可恢复模式暂不实现订阅、completion、sampling 或 elicitation。
Windows 是主要的本地验证平台；macOS 支持仍为实验性。

## 0.13.0+resilient.1 — 本地开发版本

- 初步实现仅支持 tools 的可恢复代理层，前端初始化不依赖后端是否可用。
- 提供管理工具 `proxy_status` 和 `proxy_reconnect`、有时间上限的启动等待、
  重试与退避机制，以及可选的目录缓存；不自动重放业务请求。
- 支持通过 SSE/Streamable HTTP 暴露多个命名 stdio 后端，以及反向代理。

上游版本说明见 [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy/releases)。
