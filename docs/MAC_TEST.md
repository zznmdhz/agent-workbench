# Mac v0.5.0 测试说明

当前 Mac 应用由本仓库源码在本机生成，尚未作为 GitHub 安装包发布。它只读 Codex、Claude Code 和 Hermes 原始数据；索引保存在 `~/Library/Application Support/AgentWorkbench/data/`。

## 打开应用

1. 在仓库根目录运行 `bash packaging/build_mac.sh`，生成 `dist/mac/AgentWorkbench.app`。也可打开已安装的 `~/Applications/AgentWorkbench.app`。
2. 双击应用；浏览器应打开 `http://127.0.0.1:8765/`。若没有自动打开，可手动访问。
3. 页面左上角确认 `v0.5.0`。右上角“关闭工作台”可退出，再次双击即可重开。

首次读取大型 Codex 历史日志可能需要几十秒；以后按文件大小与修改时间增量更新。请等“同步中…”和“正在索引…”消失后核对结果。

## 建议核对

1. 在热力图区点“Token 用量／运行时间”，再切换“年／月／周／日”。同一日期范围和 Agent 筛选应保留；格子悬停或键盘聚焦应显示当前指标的数值。
2. 点一个日期，或在“每日会话与时间轴”选日期。核对当天 Codex、Claude、Hermes 的会话列表和时间轴；点会话查看当天用户／Agent 文字消息。长会话可翻页。
3. 对照“全部 Agent 累计”和“自然经过（并行去重）”。若有并行运行，前者可大于后者。“已证实”仅包含完整 Codex 顶层任务；估算片段在时间轴中有斜纹。
4. 按 Agent 筛选时间；模型筛选仅应用于 Token。Claude 本机原始记录没有覆盖的日期可能为零。Hermes 的 Token 汇总不能分配到逐日格子，但其消息可用于估算时间。
5. 检查跨午夜任务：它应分别出现在两天，且不会把整个会话从开始到结束都算作活跃。

当前数据只来自这台 Mac，不含 Windows 尚未同步的记录。计算规则和限制见[会话与时间口径](project/SESSION_TIME_V0.5.md)；跨设备方案见[用量口径](project/MULTI_AGENT_USAGE_V0.4.md)。反馈问题时记录日期、Agent、来源状态和页面现象；不要把原始会话或未脱敏截图提交到 GitHub。

## 开发者检查

`bash packaging/smoke_mac.sh` 使用隔离空来源和测试数据库检查打包应用的服务、页面与用量接口。Windows v0.5.0 的安装、升级和自动更新仍需 Windows 实机验证。
