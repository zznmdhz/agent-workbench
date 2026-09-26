# Agent Workbench

Windows 本机 Codex 用量工作台。当前 **v0.3.0 是重构后的 MVP 测试版**，先核对 Token 和会话统计，再逐步恢复其他能力。它只读扫描本机 `~/.codex/sessions` 及 Codex 归档日志，不修改原始会话。

仪表盘按单日或近 7、10、15 天显示请求数、输入 Token、缓存读取、输出 Token、每日趋势、模型用量和活跃会话。点击会话可核对请求级用量。输入 Token 已包含缓存读取；“新输入”是两者的差值，不能把三项相加。价格、Hermes Token、聊天正文、文件和跨设备管理尚未纳入此 MVP，不应视为已完成。

## Windows 安装与测试

从 [Releases](https://github.com/zznmdhz/agent-workbench/releases) 下载最新的 `AgentWorkbench-Setup-0.3.0-Windows-x64.exe`，双击安装，从开始菜单打开 **Agent Workbench**。浏览器会自动打开本机页面，首次在网页创建至少 12 位密码。普通用户无需命令行或单独配置采集器。详细步骤及验收清单见 [Windows MVP 测试说明](docs/MVP_WINDOWS_TEST.md)。便携 ZIP 也可直接解压运行，但请勿与安装版同时开启。

## 数据来源与对账

Codex 请求解析参考 MIT 许可的 [CC Switch](https://github.com/farion1231/cc-switch) 算法，并保留[上游许可](docs/third_party/CC_SWITCH_LICENSE.txt)。工作台独立读取 Codex 原生日志，不依赖 CC Switch 的安装或数据库。具体口径、对账和后续阶段见 [MVP 规格](docs/project/CC_SWITCH_MVP.md)。旧版设计与 PRD 保留在 `docs/planning/` 作为历史背景；它们不是当前安装版的功能承诺。

## 开发

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22+、pnpm。安装依赖并运行验证：

```powershell
uv sync --frozen --group dev
uv run pytest -q
uv run ruff check src tests
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

私有数据库保存在 `%LOCALAPPDATA%\AgentWorkbench\data`，不会提交到仓库。本机 Codex/Hermes 原始数据不会随应用卸载而删除。

## 许可证

[MIT](LICENSE)
