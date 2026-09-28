# Mac 用量仪表盘续做验证（2026-09-28）

本记录对应交接文档 `AGENT_WORKBENCH_HANDOFF_2026-09-27.md` 的未完成改动。所有实际执行的检查均在 macOS 上完成；Windows 仅做跨平台代码路径的自动测试，尚无本轮 Windows 安装或浏览器实测，因此本轮未打包发布。

## 本轮完成

- Codex 会话优先读取原生 `threads.name`，索引文件作回退；缺标题时明确标示未命名会话。列表在视觉上截断长标题，完整标题可查看。
- 顶部 Agent 快捷切换、年／月／周／日热力图、自定义起止日期、日期下钻、模型筛选和会话说明已经连通。未来日期／小时用斜纹标识；Hermes 无法按日分配的用量单独列明。
- Mac 默认识别 `~/.hermes/state.db`，默认应用数据库使用 `~/Library/Application Support/AgentWorkbench/data/`。Windows 的 `%LOCALAPPDATA%` 路径和安装版更新逻辑保留。
- 测试和构建忽略同步软件生成的 `*.sync-conflict-*` 副本；副本本身未删除或合并。

## Mac 验证结果

| 检查 | 结果 |
| --- | --- |
| Python 测试 | `uv run pytest -q`：53 项通过，1 条第三方弃用警告 |
| 静态检查 | `uv run ruff check src tests` 通过 |
| 前端 | `pnpm --dir web build`，TypeScript 与 Vite 构建通过 |
| API | 独立数据库；非法热力图粒度返回 422；年、月、周、日、闰年、跨年、香港时区日界线有合成测试 |
| 本机来源 | 只读检查 Mac 上 Codex、Claude、Hermes 三种来源；来源状态均为 ready；热力图请求级量与 Hermes 未分配量可对上总卡片 |
| 浏览器 | Mac 本机源码预览；确认原生中文标题、30 天月视图、点击日期进入 24 小时视图、Agent 快捷切换、桌面及 390px 宽度布局 |

浏览器验收采用 `.local/mac-review/` 中的专用数据库和少量真实 Codex 会话链接。完整 Codex 原始日志体积较大，本轮未把全量导入速度或全量历史覆盖记为通过。私有数据库、原生会话内容和逐项数值不放入本报告。

## 待 Windows 实机回归

回到 Windows 后，基于当前分支构建测试版，用独立数据库验证安装版打开、Codex 原生标题、Hermes 来源、四种热力图粒度、Agent／模型筛选、窄屏布局和旧版自动更新路径。完成前不要将本轮源码预览称为 Windows 新版本已发布。
