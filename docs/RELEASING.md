# 发布维护说明

本项目在 https://github.com/liang-chang/mcp-proxy-resilient 独立维护。
保留原有 Git 历史、原作者署名和 MIT 许可证；无须保留上游远端关联。
初期通过 GitHub 提供源代码、wheel/sdist 安装包和 Windows x64 便携 ZIP。
暂不启用 PyPI、Docker Hub、GHCR 发布，也不自动合并依赖更新 PR。

## 版本与发布产物

程序版本的唯一来源是 `src/mcp_proxy/__init__.py`，setuptools 会动态读取。
首个独立发布候选版本为 `0.13.0+resilient.2`，标签必须完整写为
`v0.13.0+resilient.2`，包括加号，以保留所基于的上游版本信息。
后续升级时，应同步更新源码版本、更新日志和安装示例中的文件名。
`uv.lock` 不固定本地可编辑项目的版本；依赖或元数据变化后，仍须执行
`uv lock`，并确认 `uv sync --locked` 成功。

首个候选版本包含以下产物：

- mcp_proxy_resilient-0.13.0+resilient.2-py3-none-any.whl
- mcp_proxy_resilient-0.13.0+resilient.2.tar.gz
- mcp-proxy-resilient-0.13.0-r2-windows-x64.zip
- ZIP 对应的 `.sha256` 校验文件，以及覆盖各发布产物的 `SHA256SUMS.txt`。

Windows 构建脚本会在打包前重新安装本地可编辑项目，否则可能出现可执行文件显示 r2，
但 `dist-info` 元数据仍为 r1 的情况。
ZIP 必须包含 Python DLL、`_internal`、依赖声明、文档和通用示例。
不得包含本机 `servers.json`、凭据、缓存、日志或终端记录。
内部代理计划和 worktree 已通过 `.gitignore` 排除，不纳入发布提交。

## 本地准备

使用隔离的功能 worktree，并保留无关改动。正式发布前，审查并提交需要发布的源码、
文档和工作流文件，合入 `main`，确认即将打标签的确切提交。
生成的 `dist`、`build` 文件不应提交到 Git。
使用 `git status --short --untracked-files=all` 检查文件，避免直接暂存整个工作目录。

在项目根目录执行：

~~~powershell
uv sync --locked --python 3.13
uv run --locked pytest -q
uv run --locked mypy src
uv run --locked ruff check src tests scripts/portable_entry.py
uv run --locked ruff format --check src tests scripts/portable_entry.py
uv build --out-dir dist/python
./scripts/build-windows.ps1
~~~

将 ZIP 解压到路径含空格的新目录，把 `MCP_PROXY_PORTABLE_EXE` 环境变量指向其中的
可执行文件，再运行 `uv run --locked pytest -q`。未设置此变量时，11 项 Windows
产物测试会跳过；仅源码测试通过，不代表便携产物验收通过。测试会将代理进程的
`PATH` 限制为 `System32`，并清除 Python 环境变量。

检查 wheel/sdist 元数据，将 wheel 安装到独立环境，确认安装后的
`mcp-proxy-resilient` 入口不依赖源码目录。
若源码或构建输入变化，必须重新构建并重跑产物测试后再发布。

CI 配置了 Windows/Ubuntu、Python 3.10 和 3.13 测试。应如实说明 Linux/macOS 验证情况：
配置任务、成功交叉构建、实际运行测试通过，是不同层次的证据。
macOS 支持仍为实验性；首个版本不提供 macOS/Linux 独立可执行包。

## 仓库配置与发布

本项目仅使用自己的 `origin` 远端；原工程来源通过项目文档和许可证说明。
先执行 `git remote -v` 检查配置。如果 `origin` 仍指向原工程，将其改为自己的仓库：

~~~shell
git remote set-url origin https://github.com/liang-chang/mcp-proxy-resilient.git
~~~

如果尚未配置 `origin`，则改用 `git remote add origin`；不要重复添加。
如果仍有 `upstream` 远端，并希望取消关联，可执行 `git remote remove upstream`。
删除远端不会清除原有提交历史，也不会改变 GitHub 上的 Fork 关系。
仓库创建、提交、推送、打标签和发布，均由维护者明确执行，与本地准备工作分开。

审查后的提交已合入 `main`，并确认可以发布后，先推送：

~~~shell
git push -u origin main
~~~

等待该 `main` 提交的 CI 通过，再打标签并推送。以下命令根据终端类型选择一组，
不要重复执行，也不要复制命令行提示符。

PowerShell：

~~~powershell
$version = uv run --locked python -c 'from mcp_proxy import __version__; print(__version__)'
git tag -a "v$version" -m "mcp-proxy-resilient $version"
git push origin "refs/tags/v$version"
~~~

Bash / Git Bash：

~~~bash
version=$(uv run --locked python -c 'from mcp_proxy import __version__; print(__version__)')
git tag -a "v$version" -m "mcp-proxy-resilient $version"
git push origin "refs/tags/v$version"
~~~

`.github/workflows/release.yml` 响应 `v*` 标签，首先调用同一提交中的可复用 CI 工作流。
随后核对标签与动态包版本是否一致，构建并测试移动位置后的 Windows ZIP，构建
wheel/sdist、生成校验文件，最后使用仓库级 `GITHUB_TOKEN` 发布 GitHub Release。
只有发布任务拥有 `contents: write` 权限，无须配置 PyPI 或容器仓库凭据。

在分支上手动触发 `workflow_dispatch` 时，只执行验证、构建并上传工作流产物；
只有标签引用才能进入发布任务。排查产物或 Actions 失败时，应核对对应的提交 SHA。
如果 Release 已存在，应先检查再决定是否重试；工作流不会静默覆盖已有 Release。

## 发布后检查

- 确认标签指向已审查的提交，且所有必需任务均通过。
- 下载已发布的 ZIP 和校验文件，核验后执行其中的 `--version`。
- 确认 wheel 元数据、包版本、标签、ZIP 文件名和发布说明一致。
- 实际发布后，将更新日志中的“未发布”状态改为正式发布日期。
- GitHub About 简介建议：支持 stdio 与 HTTP 的可恢复 MCP 代理，统一实现发现与重连。

工作流参考：[可复用工作流](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)、
[工作流产物](https://docs.github.com/en/actions/tutorials/store-and-share-data)、
[gh release create](https://cli.github.com/manual/gh_release_create).
