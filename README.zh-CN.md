# mcp-proxy-resilient

支持 stdio、SSE 和 Streamable HTTP 的可恢复 MCP 代理。下游应用尚未启动、授权失效或临时离线时，
客户端仍可完成代理握手；下游恢复后再发现工具、资源和 prompt。

基于 [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy)，由
[liang-chang](https://github.com/liang-chang) 独立维护，保留上游 Git 历史、MIT 许可证和原作者信息。

[English](README.md) · [发布下载](https://github.com/liang-chang/mcp-proxy-resilient/releases) · [变更记录](CHANGELOG.md)

## 主要功能

- HTTP/stdio 前后端四种组合共用恢复逻辑。
- 下游失败不阻塞代理初始化，各 Named Server 独立恢复。
- tools、resources、资源模板、prompts 完成全部分页发现后，统一发布完整目录。
- 目录有变化才推送对应的 list_changed 通知。
- proxy_status 查看状态，proxy_reconnect 在启动应用或完成授权后重新连接。
- 可选目录元数据缓存；失败或取消的业务请求不自动重放。

当前版本 0.13.0+resilient.2。需要显式传入 --resilient；未启用时保留原版两种传输方向的启动行为。

## 安装

### Windows 便携包

发布后，从 [Releases](https://github.com/liang-chang/mcp-proxy-resilient/releases) 下载 Windows x64 ZIP，
核对 SHA256 并完整解压。程序自带 Python 和依赖，目标机器无需安装 Python、pip 或 uv。
必须保留 exe 同级的 _internal、dependency-notices 和许可证，不能只复制 exe。

~~~powershell
.\mcp-proxy-resilient.exe --version
.\mcp-proxy-resilient.exe --help
~~~

下游 Node.js/Java/Python、MCP 服务本身、外部应用和授权仍由用户准备，建议配置绝对路径。

### Python 安装

源码或 wheel 安装要求 Python 3.10+。使用 uv 从本仓库安装：

~~~shell
uv tool install "git+https://github.com/liang-chang/mcp-proxy-resilient.git"
mcp-proxy-resilient --version
~~~

也可安装 Release 附带的 wheel：

~~~shell
python -m pip install ./mcp_proxy_resilient-0.13.0+resilient.2-py3-none-any.whl
~~~

发行包和命令名都是 mcp-proxy-resilient，内部导入名仍为 mcp_proxy。建议与上游发行包使用独立环境。
首版通过 GitHub 分发，不启用 PyPI、Docker Hub 或 GHCR 自动发布。

## 四种传输组合

前端是客户端连接代理的一端，后端是代理连接实际 MCP 服务的一端。

| 前端 | 后端 | 参数示例 |
|---|---|---|
| HTTP | stdio | --resilient --frontend http --port 18080 -- your-server |
| stdio | HTTP | --resilient --frontend stdio --transport streamablehttp URL |
| stdio | stdio | --resilient --frontend stdio -- your-server |
| HTTP | HTTP | --resilient --frontend http --port 18080 --transport streamablehttp URL |

HTTP 前端同时提供 /mcp 和 /sse；后端使用 --transport sse 或 streamablehttp，默认 SSE。
未指定 --frontend 时保留旧推断：URL 对应 stdio 前端，命令/Named 配置对应 HTTP 前端。
同类传输组合必须启用 --resilient。显式 stdio 不允许代理 --port/--sse-port；子进程参数放在 -- 后。

## Named Servers

将 [servers.example.json](servers.example.json) 复制为 servers.json，并替换示例命令。
示例包含一个 stdio 后端和一个默认禁用的 HTTP 后端，可按需增删。

HTTP 前端：

~~~shell
mcp-proxy-resilient --resilient --frontend http --host 127.0.0.1 --port 18080 --named-server-config servers.json
~~~

客户端连接 http://127.0.0.1:18080/servers/local/mcp，或 SSE 地址 /servers/local/sse。
每个后端独立重连。代理自身必须在客户端初始化前开始监听，下游应用可以稍后启动。

stdio 前端：将下面的命令及参数配置给 MCP 客户端，由客户端自动启动：

~~~shell
mcp-proxy-resilient --resilient --frontend stdio --named-server-config servers.json --server local
~~~

每个进程只启动选中的一个 Named Server，不启动 HTTP listener，也不会启动未选中的后端。
Windows 包附 start-proxy.cmd：双击默认启动 HTTP 18080；http PORT 指定端口；stdio NAME 选择后端。

恢复模式每项只能配置 command/args/env/cwd，或 url/transport/headers/verify_ssl。
HTTP 项必须显式 transport。env/headers 的值是实际字符串，不自动展开变量。
修改配置后需要重启代理，proxy_reconnect 不会重读文件。

## 恢复、通知与请求语义

首次离线且没有缓存时，tools/list 仅返回 proxy_status、proxy_reconnect，其余目录为空。
上线后按能力拉取所有分页，完整成功才替换目录；部分失败保留旧快照，catalog_stale=true。
下游丢失某项能力会清空该类目录。

tools/resources/prompts 声明 listChanged=true。模板变化属于 resources/list_changed。
只推送真正变化的类别，相同目录的重连不额外通知；后端自身列表变化也会触发重新发现。
客户端须支持重新发现。无状态 HTTP 不提供持久推送，建议默认有状态 HTTP、SSE 或主动请求列表。

连接/初始化/发现默认超时 10 秒（--connect-timeout）。可用性失败默认从 5 秒退避至最高 60 秒，
--retry-interval 调整初始等待及空闲 ping 间隔。明确的 HTTP 401/403 进入 AUTH_REQUIRED，
完成授权后调用 proxy_reconnect。HTTP 404 会话失效触发连接恢复。
代理不自动打开 GUI、登录或刷新静态 token/env。

工具调用、resources/read、prompts/get 共用每后端串行队列，最多 32 个待发送请求。
活动请求最多等待下游响应 300 秒，手动重连等待活动请求结束。下游有效协议/业务错误保持原样。
本地离线/繁忙/中断时，工具返回 isError，资源/prompt 返回 JSON-RPC 错误。
失败和取消的请求不自动重放；取消本地等待不保证下游操作终止或回滚，写操作结果不明时先检查真实状态。

恢复模式暂不支持资源订阅/正文变化推送、completion、sampling、elicitation。

## 缓存与隐私

可选 --tool-cache-dir ./tool-cache，缓存四种目录元数据；不指定时只存内存。
兼容读取 v1 工具缓存，新写 v2，最大 4 MiB；损坏或超限文件会忽略。
文件名按连接身份哈希，不保存连接凭证、调用结果、资源正文或 prompt 消息。
动态认证提供器不使用磁盘缓存。元数据本身可能有敏感描述或 URI，缓存仍需限制访问。
缓存不是授权或在线证明。

恢复模式抑制 HTTPX/SDK 原始网络日志，包括 DEBUG，避免 URL token、凭证头、正文和异常细节泄漏。
HTTP 默认绑定 127.0.0.1，开放到其他网络前需自行设计认证和访问控制。

## 开发与验证

~~~shell
uv sync --locked --python 3.13
uv run --locked pytest -q
uv run --locked mypy src
uv run --locked ruff check src tests scripts/portable_entry.py
uv run --locked ruff format --check src tests scripts/portable_entry.py
uv build
~~~

Windows 是主要本机验证平台，包含 x64 独立便携程序。CI 配置为 Windows/Ubuntu、Python 3.10/3.13，
实际远程验证结果以 Actions 为准。macOS 为实验性，尚未本机验证；首版不提供 Linux/macOS 独立二进制。

参见 [Windows 部署说明](docs/DEPLOYMENT.zh-CN.md) 和 [维护者发布流程](docs/RELEASING.md)。
Release 在测试通过后，构建并移位验证真实 exe，再向 GitHub 发布 ZIP、wheel、源码包和 SHA256。
自动化测试使用模拟后端，无需云账号凭证或交互终端应用。
