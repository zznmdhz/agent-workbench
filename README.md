# Agent Workbench

各版本功能变化见 [更新日志](CHANGELOG.md)；已发布安装包与对应发布说明见 [GitHub Releases](https://github.com/zznmdhz/agent-workbench/releases)。开发测试版与已发布安装包的验证范围不同。

本机多 Agent 用量与会话工作台。当前源码为 **v0.7.0 测试版**：在 macOS 和 Windows 上只读发现 Codex、Claude Code、Hermes 的原始记录，并将对话、Token、用时和文件索引归档进 Workbench 自己的 SQLite 底库。归档后的历史记录在 Agent 卸载或源记录消失后仍可查询。产出文件本身不备份。

两台电脑各自保有本地底库，通过现有 Sync_AI／Syncthing 文件夹交换设备专属数据包；页面可查看全部电脑汇总，也可只看 Mac 或 Windows。两端不直接共写一个 SQLite 数据库。Mac 默认查找 `~/Sync_AI`，Windows 默认查找 `B:\Sync_AI`；实际路径不同可在页面顶部修改。配置、口径和恢复方式见[底库与双机同步](docs/project/DURABLE_ARCHIVE_SYNC_V0.6.md)。本轮只在 Mac 做基础验证，Windows 实机同步和安装待回到 Windows 后验证。

v0.7.0 延续 Token／运行时间热力图、按日期和 Agent 查会话、标题／正文／产出路径搜索、左右分栏、关键节点／完整详情、文件历史和打开所在文件夹。Hermes Token 仍只有会话／模型汇总，不能准确拆到每天。时间同时显示累计时长与并行去重后的自然经过时间，部分来源用时为估算。文件数只统计原始记录可确认的操作；详见[会话导航与文件口径](docs/project/SESSION_NAVIGATION_V0.5.3.md)和[会话与时间说明](docs/project/SESSION_TIME_V0.5.md)。新增多图模型分析、缓存率和请求频率视图；会话可按文字量、用时、记录大小、消息及文件数排序，并按标题／内容／文件路径高级检索。图表和筛选口径见[模型分析与会话检索](docs/project/MODEL_REPORT_AND_SESSION_FILTERS_V0.7.md)。本机页面地址为 `http://127.0.0.1:8765/`。

GitHub Releases 当前公开的 Windows 安装包为 v0.4.1；它的界面和登录流程与 v0.7.0 源码不同。

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
