# Agent Workbench

本机多 Agent 用量与会话工作台，当前版本 **v0.8.0**。只读发现 Codex、Claude Code、Hermes 的原始记录，将 Token、对话、用时和文件操作索引归档到自己的 SQLite 底库。无需 Electron，网页直接在浏览器中使用，Windows 本机访问无需密码。

## Windows 安装与日常使用

从 [v0.8.0 发布页](https://github.com/zznmdhz/agent-workbench/releases/tag/v0.8.0) 下载 `AgentWorkbench-Setup-0.8.0-Windows-x64.exe`，安装后从开始菜单打开 **Agent Workbench**。页面地址是 `http://127.0.0.1:8765/`。已有用户升级保留底库，首次回填可能需要等待。

后台随 Windows 登录启动；关闭浏览器仍继续采集。右下角通知区有常驻图标，Windows 可能把它放入“显示隐藏图标”。图标菜单支持打开、刷新、暂停、恢复、重启、日志和退出。浏览器标签页及页面状态条也显示运行／采集／离线状态和最近采集时间，可固定标签页方便访问。异常进程退出由当前用户的系统计划任务尝试恢复；正常退出则下次登录自动启动。

Windows 安装版在后台检查稳定版本并校验下载，更新前备份程序和底库，失败尝试回滚。便携 ZIP 使用自己的 `data/`，不注册登录任务、不自动替换程序，不与安装版共用端口。完整行为与限制见[后台运行及更新说明](docs/project/LOCAL_BACKGROUND_V0.8.md)，操作检查见[Windows 测试说明](docs/MVP_WINDOWS_TEST.md)。软件尚未做 Windows 代码签名。

## 数据与功能

- Token 的新输入、缓存读取、缓存写入、输出可逐项对账；年／月／周／日热力图和自选起止日期查询；Agent 快速切换、模型比较、趋势、缓存率和请求频率。
- 会话按日期／Agent／设备浏览，原生标题、关键节点及完整详情；标题／正文／文件路径搜索和多种排序。
- 文件操作历史及来源记录大小，支持定位本机仍存在的文件夹；文件实体不备份，单个会话的进程内存无法从日志推算。
- 可信任务边界与消息估算用时分开；导入合成的跨日任务不作为已证实运行时段。并行时长分别给出累计执行与去重后的自然经过时间。
- Hermes 只有会话／模型聚合 Token，不能可靠拆到日／小时；独立显示未分配量。未被源日志记录或归档的历史不能恢复，不能把空格子视为一定没使用。

归档后的历史在 Agent 卸载或源记录消失后仍可查询。两个设备各有自己的底库，通过同步文件夹交换设备专属数据包，不共写 SQLite。Windows 默认发现 `B:\Sync_AI`，Mac 默认 `~/Sync_AI`，页面可调整路径；见[底库与双机同步](docs/project/DURABLE_ARCHIVE_SYNC_V0.6.md)。开发源码应通过 Git 同步，依赖、构建目录和 `.git` 排除 Syncthing。

## macOS

在 Mac 上运行 `bash packaging/build_mac.sh` 可构建 `.app`。源码包含用户 LaunchAgent 的登录启动与异常恢复配置，但本次没有发布公证的 Mac 安装包，也没有完成 Mac 实机恢复及双机验收；不要将 Windows 发布结果视为 Mac 验收。当前通知区图标实现针对 Windows。

隔离源码预览（完成依赖安装及前端构建后）：

```bash
uv run python packaging/desktop_entrypoint.py --db .local/review/usage.db --port 8767 --no-browser --no-tray
```

打开 `http://127.0.0.1:8767/`，自定义数据库／端口不会注册安装版登录任务。Agent 路径可用 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`HERMES_STATE_DB` 调整。私有底库和原始正文不能上传至项目仓库。

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
