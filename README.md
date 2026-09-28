# Agent Workbench

各版本功能变化见 [更新日志](CHANGELOG.md)；已发布安装包与对应发布说明见 [GitHub Releases](https://github.com/zznmdhz/agent-workbench/releases)。开发测试版与已发布安装包的验证范围不同。

本机多 Agent 用量与会话工作台。当前开发分支为 **v0.5.2 测试版**：只读扫描 Codex、Claude Code 原生会话日志，以及 Hermes 本机 `state.db`，不修改原始记录。代码同时支持 macOS 和 Windows；v0.5.2 在 Mac 上构建和验证，Windows 安装包仍待实机验证。GitHub Releases 当前公开的 Windows 安装包为 v0.4.1。

v0.5.2 本机应用已移除密码页，打开 `http://127.0.0.1:8765/` 即可查看仪表盘。已发布的 v0.4.1 Windows 安装包仍使用旧密码流程。

v0.5.2 仪表盘可按日期、Agent 与模型筛选 Token 用量，并通过两个按钮切换 Token／运行时间热力图；两个视图共用年／月／周／日导航。会话记录区可独立选一天或日期范围，并快速切换全部、Codex、Claude、Hermes；左侧搜索标题、对话内容和文件路径，右侧查看文字消息、原始日志位置及有证据的文件操作。时间同时显示各 Agent 运行区间相加的累计时长与并行去重后的自然经过时间。Codex 有完整任务事件时使用源记录时长；缺事件的 Codex、Claude 和 Hermes 仅能按消息推算，界面会标明估算。Hermes Token 仍只有会话／模型汇总，不能准确拆到每天。完整口径见[会话与时间说明](docs/project/SESSION_TIME_V0.5.md)。当前数字只来自运行应用的这台电脑，Mac 与 Windows 尚未同步；跨设备方案见[多 Agent 用量口径](docs/project/MULTI_AGENT_USAGE_V0.4.md)。价格、完整工具轨迹和跨设备管理尚未纳入此版本。文件记录仅覆盖可核实的工具操作及明确标出的待核实路径，不能保证找全所有产物；存储大小是原始记录的磁盘大小，并非会话 RAM。详情见[会话文件与搜索说明](docs/project/SESSION_INSPECTOR_V0.5.2.md)。

## Windows 安装与测试

从 [Releases](https://github.com/zznmdhz/agent-workbench/releases) 下载 `AgentWorkbench-Setup-0.4.1-Windows-x64.exe`，双击安装，从开始菜单打开 **Agent Workbench**。浏览器会自动打开本机页面，首次在网页创建至少 12 位密码。普通用户无需命令行。v0.4.1 起，安装版登录后会自动检查 GitHub 发布、下载并校验新安装包，然后关闭旧进程、静默安装并重新打开。安装器也会先通知运行中的工作台退出，再处理占用文件。v0.4.0 及更早版本尚无内置更新器，需要这一次手动安装 v0.4.1。详细步骤及验收清单见 [Windows 测试说明](docs/MVP_WINDOWS_TEST.md)。便携 ZIP 保持手动替换，不与安装版同时开启。

## macOS 应用测试

在 Mac 上运行 `bash packaging/build_mac.sh` 可生成 `dist/mac/AgentWorkbench.app`。双击应用会在后台启动本机服务并打开浏览器；关闭时在页面点击“关闭工作台”。首次读取历史日志可能需要一些时间，尤其是大体积 Codex 会话。应用默认读取 `~/.codex`、`~/.claude` 和 `~/.hermes/state.db`，数据保存在 `~/Library/Application Support/AgentWorkbench/data/`。详细操作见 [Mac 测试说明](docs/MAC_TEST.md)。当前 Mac 应用是本机构建的未公证测试版，尚未发布为 GitHub 安装包。

如需隔离数据库的源码预览，在 Mac 上完成下方依赖安装和构建后运行：

```bash
uv run python packaging/desktop_entrypoint.py --db .local/mac-review/usage.db --no-browser
```

打开 `http://127.0.0.1:8765/`。如 Hermes 安装位置不同，可在启动前设置 `HERMES_STATE_DB`。上述预览使用 `.local/` 中的独立数据库；Mac 测试版尚无自动更新。

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
