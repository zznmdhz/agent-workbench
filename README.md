# Agent Workbench

本机多 Agent 用量工作台。当前 **v0.4.1 是用量测试版**：只读扫描 Codex、Claude Code 原生会话日志，以及 Hermes 本机 `state.db`，不修改原始记录。源码可在 macOS 和 Windows 运行；当前公开安装包仅支持 Windows。

当前工作分支的本机预览已移除密码页，打开 `http://127.0.0.1:8765/` 即可查看仪表盘。此修改尚未打包发布；GitHub 上的 v0.4.1 安装包仍使用旧密码流程。

当前工作分支的仪表盘可按任意起止日期（最长十年）、Agent 与模型筛选，请求、输入、缓存读取／写入、输出、热力图、模型和会话联动展示。热力图提供年／月／周／日粒度，Codex 会话优先使用原生可读名称。Codex 与 Claude Code 可按请求时间归属；Hermes 当前只有会话／模型汇总，完整落在所选时间段内的记录会计入总数，但无法准确拆到每天；跨越查询边界的汇总记录会单独计数并暂不纳入。详情见[多 Agent 用量口径](docs/project/MULTI_AGENT_USAGE_V0.4.md)。价格、聊天正文、文件和跨设备管理尚未纳入此版本。

## Windows 安装与测试

从 [Releases](https://github.com/zznmdhz/agent-workbench/releases) 下载 `AgentWorkbench-Setup-0.4.1-Windows-x64.exe`，双击安装，从开始菜单打开 **Agent Workbench**。浏览器会自动打开本机页面，首次在网页创建至少 12 位密码。普通用户无需命令行。v0.4.1 起，安装版登录后会自动检查 GitHub 发布、下载并校验新安装包，然后关闭旧进程、静默安装并重新打开。安装器也会先通知运行中的工作台退出，再处理占用文件。v0.4.0 及更早版本尚无内置更新器，需要这一次手动安装 v0.4.1。详细步骤及验收清单见 [Windows 测试说明](docs/MVP_WINDOWS_TEST.md)。便携 ZIP 保持手动替换，不与安装版同时开启。

## macOS 源码预览

在 Mac 上完成下方依赖安装和构建后，使用独立测试数据库启动，避免影响已有数据：

```bash
uv run python packaging/desktop_entrypoint.py --db .local/mac-review/usage.db --no-browser
```

打开 `http://127.0.0.1:8765/`。默认读取 `~/.codex`、`~/.claude` 和 `~/.hermes/state.db`；如 Hermes 安装位置不同，可为启动命令设置 `HERMES_STATE_DB`。Mac 默认工作台数据库位于 `~/Library/Application Support/AgentWorkbench/data/`，但上述预览显式使用 `.local/` 中的独立数据库。源码预览不等于已提供 macOS 安装包或自动更新。

## 数据来源与对账

Codex 和 Claude 请求解析参考 MIT 许可的 [CC Switch](https://github.com/farion1231/cc-switch) 算法，并保留[上游许可](docs/third_party/CC_SWITCH_LICENSE.txt)。工作台独立读取 Agent 原生记录，不依赖 CC Switch 的安装或数据库。原 v0.3.0 范围见 [Codex MVP 规格](docs/project/CC_SWITCH_MVP.md)；旧版 PRD 保留在 `docs/planning/` 作为历史背景，不是当前安装版的功能承诺。

## 开发

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22+、pnpm。安装依赖并运行验证：

```bash
uv sync --frozen --group dev
uv run pytest -q
uv run ruff check src tests
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

默认私有数据库在 Windows 的 `%LOCALAPPDATA%\AgentWorkbench\data` 或 Mac 的 `~/Library/Application Support/AgentWorkbench/data/`，不会提交到仓库。本机 Codex/Hermes 原始数据不会随应用卸载而删除。

## 许可证

[MIT](LICENSE)
