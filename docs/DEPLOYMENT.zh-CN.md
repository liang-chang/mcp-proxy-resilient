# Windows 便携部署

[中文首页](../README.zh-CN.md) · [English](../README.md)

## 下载与目录

从本项目 GitHub Releases 下载 Windows x64 ZIP 和 SHA256SUMS.txt。
例如版本 0.13.0+resilient.2 的 ZIP 为 mcp-proxy-resilient-0.13.0-r2-windows-x64.zip。
使用 Get-FileHash 核对摘要后完整解压：

~~~powershell
Get-FileHash ./mcp-proxy-resilient-0.13.0-r2-windows-x64.zip -Algorithm SHA256
~~~

建议程序与配置目录分开，以下路径仅为示例：

~~~text
C:/Tools/mcp-proxy/
  app/
    mcp-proxy-resilient.exe
    _internal/
    dependency-notices/
    start-proxy.cmd
    servers.example.json
    README.md
    README.zh-CN.md
    docs/
      DEPLOYMENT.zh-CN.md
      RELEASING.md
    LICENSE
    uv.lock
  servers.json
  tool-cache/
  logs/
~~~

程序自带 Python 和运行依赖，无需目标机器安装 Python、pip 或 uv。
必须保留 _internal；下游 MCP 服务、Node.js/Java 等运行时、外部 UI 和授权仍需自行准备。
包未做代码签名；Windows 系统组件仍需要。本包仅针对 Windows x64。

## 配置后端

将 servers.example.json 复制为自己的 servers.json，替换 local 的 command 和 args。
支持 stdio 和 HTTP 后端混合配置，参见首页。command 建议使用绝对路径。
env、headers 只接受字符串映射，不展开变量；cwd 适用于 stdio。
HTTP 项必须显式 transport 为 sse 或 streamablehttp。

不要把自己的 servers.json、凭证、终端录屏或缓存提交到仓库。
--pass-environment 会向子进程传递全部环境，应根据需要使用。
配置修改后重启代理；proxy_reconnect 不会重读文件或刷新静态 token/env。

## HTTP 方式

~~~powershell
& "C:/Tools/mcp-proxy/app/mcp-proxy-resilient.exe" --resilient --frontend http --host 127.0.0.1 --port 18080 --named-server-config "C:/Tools/mcp-proxy/servers.json" --tool-cache-dir "C:/Tools/mcp-proxy/tool-cache"
~~~

客户端连接 http://127.0.0.1:18080/servers/local/mcp。
SSE 地址为 /servers/local/sse。单个默认后端则使用 /mcp 或 /sse。
先启动代理再初始化客户端，下游应用可以稍后启动或授权。
如果端口已占用，先检查原进程或选择其他端口。

保持本地绑定；对外开放前需要设计认证、访问控制和凭证隔离。

## stdio 方式

将以下命令和参数配置为 MCP 客户端的 stdio 服务，由客户端自动启动：

~~~powershell
& "C:/Tools/mcp-proxy/app/mcp-proxy-resilient.exe" --resilient --frontend stdio --named-server-config "C:/Tools/mcp-proxy/servers.json" --server local --tool-cache-dir "C:/Tools/mcp-proxy/tool-cache"
~~~

每个代理进程只连接选中的一个后端，不启动 HTTP listener。
多个工具可以由客户端分别启动多个代理进程。
--server 不能混用位置目标；显式 stdio 不允许代理 --port，包括 --port 0。
下游自己的参数用 -- 隔开，例如 -- your-server --port 1234。

便携包附 start-proxy.cmd。如果使用这个脚本，把配置放在脚本同目录的 servers.json：

~~~powershell
./start-proxy.cmd
./start-proxy.cmd http 18081
./start-proxy.cmd stdio local
~~~

无参数时默认 HTTP 18080。stdio 模式的 stdout 是协议流，不要合并日志、echo 或 pause。
代理日志默认写 stderr；日志文件请另放 logs 目录，并自行安排轮转。
PowerShell 使用 ./ 或绝对路径避免调用 PATH 中旧程序；CMD 中不要用单引号包路径。

## 恢复验收

1. 后端未启动时，代理可握手，proxy_status 返回 RETRY_WAIT 等状态。
2. 初次无缓存仅有两个管理工具，其余目录为空。
3. 启动下游应用或完成授权，等待重试或调用 proxy_reconnect。
4. READY 且 catalog_stale=false 后执行只读业务查询。
5. 断开后端并恢复，确认客户端支持列表变化后的重新发现。

例如 terminal-mcp 的交互会话通常需要先运行 tmcp。
代理能够自动重试 MCP 子进程，但不会替你打开外部终端或登录云账号。
HTTP 401/403 停在 AUTH_REQUIRED，授权后显式重连。

每后端的工具调用、资源读取和 prompt 获取串行执行，活动请求最多等待 300 秒。
重连等待活动请求结束，未发送请求失败。失败/取消不自动重放，副作用不保证回滚。
读到缓存目录不等于后端在线。完整恢复语义和能力边界见首页。

## 升级与验证范围

先停止旧代理并备份配置，再替换整个 app 目录；不要覆盖正在运行的文件。
v2 元数据缓存可读旧 v1 工具缓存，旧程序不理解 v2 时会重新发现。
目录缓存最多 4 MiB，不保存正文、结果或连接凭证；目录元数据仍可能敏感。

Windows 为主要本机验收平台。发布流水线从 ZIP 解压到含空格的新路径，限制代理子进程 PATH，
验证不依赖目标环境的 Python/uv，并测试真实 stdio/SSE/Streamable HTTP 连接。
测试使用模拟后端；真实云授权、HTTPS/自定义 CA 和全新 Windows VM 需按部署环境另行验证。

重建方式与版本/tag 规则见 [RELEASING.md](RELEASING.md)。
