# 验证记录

阶段状态只在证据齐全时标为通过。Windows 本地探针、自动测试、模拟负载、macOS 实机、NAS 部署和真实双机接续分别记录；一个来源的验证不代表其他版本或入口也可用。

| 阶段 | 当前状态 | 必要证据 |
| --- | --- | --- |
| P0 来源探针 | Windows 历史读取通过，整体进行中 | 每个实际使用的来源各有历史与新任务脱敏证据 |
| V0.1 双机 MVP | 未验收 | Windows/Mac → NAS 同步、统计与页面、备份恢复 |
| V0.2 交接 | 未验收 | Mac 离线后 Windows 使用冻结包继续并关联新会话 |
| V0.3 资源 | 未验收 | 进程级采样与开销实测 |

本目录只保存可以公开的测试方法、虚构样例和脱敏结果。完整本机探针报告放在 `.local/reports/`，不上传 GitHub。

2026-09-23 本机验证：Windows Codex 15,761 条与 Hermes 22,042 条 stats_only 事件写入本地 outbox，合计 37,803 条，全部通过 Pydantic wire contract 验证；未传输到中心或 GitHub。`uv run pytest -q` 通过 4 个自动测试；`uv run ruff check src tests`、`cd web && pnpm build` 通过。此结果证明当前安装的数据可读取和标准化，不证明所有版本/入口、统计完整性或跨机部署。当前机器没有 Docker，也没有可访问的 Mac/NAS 验收环境。
