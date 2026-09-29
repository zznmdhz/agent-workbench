# Workbench 底库与双机同步（v0.6.0）

## 保存范围

工作台只读发现本机 Codex、Claude Code 和 Hermes 的原始记录，并将可读对话正文、Token 观测、任务时间段、会话元数据、原始记录位置及文件操作索引写入自己的 SQLite 底库。Mac 默认为 `~/Library/Application Support/AgentWorkbench/data/agent-workbench.db`，Windows 默认为 `%LOCALAPPDATA%\AgentWorkbench\data\agent-workbench.db`。首次发现之前已被删除的 Agent 记录无法恢复；成功入库后，即使卸载 Agent 或其原始记录消失，工作台仍可查询已归档内容。

产出文件只保存路径、操作时间和证据；**不复制文件内容**。文件夹按钮只在原电脑上启用，且要求文件夹仍存在。Hermes 的原始对话位于共享 `state.db`，所以只给会话消息及工具字段的内容量，不将整个数据库体积错误分摊到单个会话。时间估算和 Hermes Token 日期限制沿用既有口径。

## 同步方式

两台电脑各自写自己的本地 SQLite。应用运行期间会定期发现数据；每次发现新事实时，工作台把变化打包为不可变的 `.json.gz` 文件，放到 Syncthing 同步目录中的 `AgentWorkbench-data-exchange/devices/<设备 ID>/packets/`。Mac 只写自己的设备目录，Windows 只写自己的设备目录；另一台电脑收到文件后将其导入自己的 SQLite。离线时文件在本机排队，下一次设备启动且同步目录可用时继续交换。两台电脑同时运行也不会共同写一个 SQLite 文件。

默认查找 Mac 的 `~/Sync_AI` 和 Windows 的 `B:\Sync_AI`；只有包含 Syncthing `.stfolder` 的目录会自动启用。若实际路径不同，在页面顶部“NAS 同步文件夹”填写该电脑上的同步目录并保存。两端应填写**同一 Syncthing 共享文件夹各自的本地路径**。不要直接同步运行中的 `agent-workbench.db`、`-wal` 或 `-shm` 作为双机共写数据库。页面“全部电脑（去重汇总）”合并两端记录；也可以选单台电脑。若把相同的原生 Agent 历史拷到两台电脑，汇总视图按 Agent、原生 ID 和事实 ID 去重；单机视图保留各自采集结果。

交换包含完整对话文本，请将 Syncthing 共享范围只给自己的设备，并保护 NAS 账户与磁盘。交换目录是同步副本，工作台本地 SQLite 是每台电脑的查询副本；保留交换目录即可在新装 Workbench 时重新导入已有包。安装程序卸载与 Agent 卸载不应被当作数据清理流程，清理前请单独备份本机底库及交换目录。

## 本版边界

Mac 已做底库回填、页面和隔离双设备包交换的基础验证。Windows 代码路径和打包版本已更新，Windows 实机发现、安装、Syncthing 传输和双机同时运行待用户回到 Windows 后验证。文件索引受 Agent 原始记录覆盖范围限制；Shell 或外部程序未留下可识别操作记录时，不会凭空推断产出文件。
