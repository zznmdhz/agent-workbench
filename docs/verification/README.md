# 验证记录

阶段状态只在证据齐全时标为通过。Windows 本地探针、自动测试、模拟负载、macOS 实机、NAS 部署和真实双机接续分别记录；一个来源的验证不代表其他版本或入口也可用。

| 阶段 | 当前状态 | 必要证据 |
| --- | --- | --- |
| P0 来源探针 | Windows 历史读取通过，整体进行中 | 每个实际使用的来源各有历史与新任务脱敏证据 |
| V0.1 双机 MVP | 未验收 | Windows/Mac → NAS 同步、统计与页面、备份恢复 |
| V0.2 交接 | 未验收 | Mac 离线后 Windows 使用冻结包继续并关联新会话 |
| V0.3 资源 | 未验收 | 进程级采样与开销实测 |

本目录只保存可以公开的测试方法、虚构样例和脱敏结果。完整本机探针报告放在 `.local/reports/`，不上传 GitHub。

2026-09-23 本机验证：Windows Codex 15,761 条与 Hermes 22,042 条 stats_only 事件写入本地 outbox，合计 37,803 条，全部通过 Pydantic wire contract 验证；未传输到中心或 GitHub。`uv run pytest -q` 通过 5 个自动测试；`uv run ruff check src tests`、`cd web && pnpm build` 通过。GitHub CI 在 Windows/macOS/Linux 上通过代码测试；这不等于对应机器已运行真实 Agent 来源。此结果证明当前 Windows 安装的数据可读取和标准化，不证明所有版本/入口、统计完整性或跨机部署。当前机器没有 Docker，也没有可访问的 Mac/NAS 验收环境。

2026-09-26 Windows v0.2.0 验证：`uv run pytest -q` 通过 8 项测试，`uv run ruff check src tests` 与 `pnpm --dir web build` 通过。Inno Setup 安装程序构建成功，在本机静默安装到测试目录后，安装版 EXE 的 `/health/ready`、网页首页和单 owner 登录通过。用虚构 Codex JSONL 测试自动发现、上传、会话与消息投影，待传归零。

本机真实来源仅以 `stats_only` 策略导入测试数据库：38,537 条事件、314 条会话、30,015 条消息、608 条轮次和 7,023 条用量观察完成投影；待传 0、隔离 0。统计接口在 2026-09-23 香港时区返回 HTTP 200，并给出独立的并行时间并集与累计执行时间。运行中的数据库成功生成一致性备份；停止写入后再次备份并恢复，`events/sessions/messages/runs/usage_observations/devices/sources` 七张表的记录数量与源库一致。私有测试库、outbox、备份和日志均保存在 `.local/`，不发布。

Windows 安装版未测试系统登录后自启动、自动更新或代码签名，也没有目标 Mac/NAS 环境。v0.2.0 的通过结论仅覆盖当前 Windows 单机发行范围，不等于原 PRD 的跨机完整验收。

2026-09-26 Windows v0.2.1 首次使用体验验证：`uv run pytest -q` 通过 14 项测试，`ruff` 和 Web 构建通过。全新静默安装后，安装版 `AgentWorkbench.exe` 为 Windows GUI 子系统（PE subsystem 2），启动时未创建控制台；浏览器实测直接显示“创建管理员密码”页面。以独立测试数据库完成首次设置、登录会话与网页关闭接口验证；关闭后进程退出。便携包已独立解压，确认双击启动走便携数据目录、显示首次设置状态，并可从网页关闭。`AgentWorkbenchCLI.exe --help` 已在安装目录验证。密码重设的数据保留逻辑有自动化测试，最终安装版的重设快捷方式已检查；未对用户现有密码执行重设。
