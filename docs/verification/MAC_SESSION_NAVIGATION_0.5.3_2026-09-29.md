# Mac 会话导航基础核验（v0.5.3，2026-09-29）

本轮在 Mac 上验证会话列表大小与目录入口、详情摘要、关键节点切换、文件历史目录入口。Windows 路径有自动测试覆盖，尚未在 Windows 实机验证。

| 检查 | 结果 |
| --- | --- |
| 自动测试 | `uv run pytest -q`：63 项通过，包含关键节点筛选、目录解析、Mac／Windows 打开命令和本地 API 授权检查 |
| 静态与前端 | `uv run ruff check src tests`、`pnpm --dir web build`、`git diff --check` 通过 |
| Mac 安装包 | `packaging/build_mac.sh`、`packaging/smoke_mac.sh`、`codesign --verify --deep --strict` 通过 |
| 已安装应用 | `/Users/ethan/Applications/AgentWorkbench.app` 的 `/health/ready` 返回 v0.5.3；升级前保留本地数据库备份 |
| 真实数据 | 近 7 天列出 71 个会话；Hermes 一条 40 消息会话的关键节点模式显示 6 条；列表显示 Codex 原始记录大小、Hermes 消息与工具字段估计大小 |
| 页面基本操作 | 选中上述 Hermes 会话后，详情显示 3 轮用户提问、20 分钟 Agent 用时、5 个已确认文件；文件页保留 6 次确认写入记录。点击一条现存文件的“打开所在文件夹”，Finder 打开 `交付_智界RX车评脚本` 文件夹，文件未被直接打开 |

Hermes 所有会话共用 `state.db`；单会话消息与工具字段大小是内容估计，不是数据库文件的磁盘占用，也不是内存。文件操作仅覆盖可从原始记录确认的行为；Shell、外部程序或遗漏的日志可能产生未列出的文件。本轮是基础交付验证，具体内容及完整体验由用户手动验收。私有数据库、备份、对话正文与截图未上传 GitHub。
