# Agent Workbench 跨 Agent 工作台

一个部署在自己 NAS 上的个人工作台。Windows 与 macOS 的采集器只读访问本机 Codex、Hermes 记录，将可获得的会话、活动和用量同步到中心服务。网页按设备、Agent、模型和日期查看统计，并沿会话找到执行片段与文件线索。

项目按[产品与工程规划](docs/planning/02_EXECUTION_SPEC.md)开发。统计数据会标明来源、缺口和精度；历史来源没有保存的值不会被补造。

## 状态

**v0.2.0 是 Windows 单机安装版。** 启动后自动登记本机 Codex/Hermes 来源并以仅统计策略采集；会话、搜索、统计、来源状态和交接包可在网页使用。Windows 安装、登录与本机采集已测试。Mac 采集、NAS 容器和真实双机接续仍需目标环境，因此整个跨机 PRD 尚未验收。详见[Windows 使用说明](docs/WINDOWS.md)、[验证记录](docs/verification/README.md)和[剩余工作](docs/project/ROADMAP.md)。

## 本地开发

### Windows 安装版

[下载 Windows v0.2.0 安装程序](https://github.com/zznmdhz/agent-workbench/releases/tag/v0.2.0)。从开始菜单打开，首次在启动窗口设置管理员密码；工作台会自动打开浏览器、发现本机来源并开始仅统计采集。无需手动配对或复制来源 ID。另提供无需安装的便携 ZIP。数据与升级说明见 [Windows 使用说明](docs/WINDOWS.md)。

### 从源码运行

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js 22+/pnpm。依赖锁定在 `uv.lock` 与 `web/pnpm-lock.yaml`。

```powershell
uv sync --frozen
uv run awb --help
uv run pytest -q
uv run ruff check src tests
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

首次本地运行：

```powershell
uv run awb open --db .local/server.db
```

命令会在首次运行时引导设置密码并打开浏览器。然后在网页的“设备与设置”生成一次性配对码，再在采集机器运行：

```powershell
uv run awb pair http://127.0.0.1:8765 123456789
uv run awb add-source codex "$HOME\.codex\sessions" --policy stats_only
uv run awb add-source hermes "$HOME\AppData\Local\hermes\state.db" --policy stats_only
```

这里的 `123456789` 只是命令示例，实际使用网页生成的码。`add-source` 输出来源 ID；在网页确认来源、设备和采集策略后运行 `uv run awb collect-once` 或 `uv run awb collect`。跨机器需要可达的 HTTPS 地址；不要将开发用 HTTP 绑定到公网。macOS 根目录以当地实际安装路径为准，不能照抄 Windows 示例。详见[运行手册](docs/RUNBOOK.md)。

所有私有配置、队列、会话与诊断产物保存在 `.local/` 或自选数据目录，不进入仓库。不要把 Codex、Hermes 原目录、登录信息或真实聊天内容提交到公开仓库。

## 规划与项目管理

- [产品原始 PRD](docs/planning/跨Agent工作台_PRD_v0.1.md)
- [执行规格](docs/planning/02_EXECUTION_SPEC.md)
- [任务计划](docs/planning/03_IMPLEMENTATION.md)
- [验收规范](docs/planning/04_ACCEPTANCE.md)
- [验证记录](docs/verification/README.md)
- [运行手册](docs/RUNBOOK.md)
- [公开路线图](docs/project/ROADMAP.md)

[GitHub Issues](https://github.com/zznmdhz/agent-workbench/issues) 记录 13 项待完成开发与实机验收任务，[Milestones](https://github.com/zznmdhz/agent-workbench/milestones) 对应 P0、V0.1、V0.2、V0.3。公开仓库不承载用户设备的私人证据；真实探针报告在本机生成，只发布脱敏结论。

## 许可证

[MIT](LICENSE)
