# Agent Workbench

Windows 本机多 Agent 用量工作台。当前 **v0.4.0 是用量测试版**：只读扫描 Codex、Claude Code 原生会话日志，以及 Hermes 本机 `state.db`，不修改原始记录。

仪表盘可按任意起止日期（最长十年）、Agent 与模型筛选，请求、输入、缓存读取／写入、输出、趋势、模型和会话联动展示。Codex 与 Claude Code 可按请求时间归属；Hermes 当前只有会话／模型汇总，完整落在所选时间段内的记录会计入总数，但无法准确拆到每天；跨越查询边界的汇总记录会单独计数并暂不纳入。详情见[多 Agent 用量口径](docs/project/MULTI_AGENT_USAGE_V0.4.md)。价格、聊天正文、文件和跨设备管理尚未纳入此版本。

## Windows 安装与测试

从 [Releases](https://github.com/zznmdhz/agent-workbench/releases) 下载 `AgentWorkbench-Setup-0.4.0-Windows-x64.exe`，双击安装，从开始菜单打开 **Agent Workbench**。浏览器会自动打开本机页面，首次在网页创建至少 12 位密码。普通用户无需命令行。详细步骤及验收清单见 [Windows 测试说明](docs/MVP_WINDOWS_TEST.md)。便携 ZIP 也可直接解压运行，但请勿与安装版同时开启。

## 数据来源与对账

Codex 和 Claude 请求解析参考 MIT 许可的 [CC Switch](https://github.com/farion1231/cc-switch) 算法，并保留[上游许可](docs/third_party/CC_SWITCH_LICENSE.txt)。工作台独立读取 Agent 原生记录，不依赖 CC Switch 的安装或数据库。原 v0.3.0 范围见 [Codex MVP 规格](docs/project/CC_SWITCH_MVP.md)；旧版 PRD 保留在 `docs/planning/` 作为历史背景，不是当前安装版的功能承诺。

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
