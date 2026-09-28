# Mac 会话筛选核验（v0.5.1，2026-09-28）

本轮只改两处：移除难读的逐条运行时间轴；会话记录增加独立的单日／日期范围选择与全部、Codex、Claude、Hermes 切换。时间汇总和 Token／时间热力图保留。对话详情按会话区所选范围读取文字消息。

| 检查 | 结果 |
| --- | --- |
| 自动测试 | `uv run pytest -q` 57 项通过；新增范围、Agent 过滤与正文范围测试 |
| 代码与前端 | `uv run ruff check src tests`、`pnpm --dir web build`、`git diff --check` 通过 |
| Mac 包 | `build_mac.sh`、`smoke_mac.sh`、`codesign --verify --deep --strict` 通过 |
| 已安装应用 | `~/Applications/AgentWorkbench.app` 的 `/health/ready` 返回 v0.5.1 |
| 本机真实数据 | 9 月 28 日会话接口返回 13 个会话：Codex 5、Claude 0、Hermes 8。数量随当天继续使用变化。 |
| 页面操作 | 页面显示独立会话控件与运行时间汇总，无逐条时间轴；“近 7 天”、手动日期、Codex 切换更新列表；打开会话后详情显示同一所选范围的文字消息。 |

本次验证基于这台 Mac 的本地来源。Windows 构建脚本和版本号已同步更新，但 Windows 安装、升级、自动更新和 UI 仍需在 Windows 实机验证。两机数据尚未自动同步。原始会话、数据库与截图未提交仓库。
