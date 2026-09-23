# 官方资料核查与接入边界

核查日期：2026-09-23。本文供开发 Agent 选择适配器与执行 P0 探针使用；它是本次联网读取官方文档、Hermes 官方代码后的核查记录，不是实机验收报告。

已审阅原始《跨 Agent 工作台：产品需求与技术设计（PRD v0.1）》第 06、15 章及附录，并参考所引用聊天的正文。原 PRD 已正确指出新建 App Server 不等于订阅已有桌面窗口、历史缺失不能补造、活数据库不能跨机共写、Handoff 不能外推为任意任务热迁移等边界。以下内容补充执行细节，不将这些原有正确警告列为原文错误。

## 1. 已确认的官方事实与工程含义

### Codex

| 依据 | 已确认事实 | 对本项目的含义 |
| --- | --- | --- |
| OAI-01 | `thread/list`、`thread/read` 可读取存储线程；`thread/read` 不加载线程或订阅事件。运行通知包含 `turn/started`、`turn/completed`、`thread/tokenUsage/updated`。 | 历史读取与实时观察必须分别验收。不能把 API 存在写成能够监听现有桌面全部任务。 |
| OAI-01 | `thread/loaded/list` 列出服务器当前内存线程。`thread/turns/list`、`thread/items/list` 属实验接口，后者还要求存储支持分页。 | 运行状态有实例范围；长会话读取需能力检测和回退。按安装版生成协议 schema，保存其指纹。 |
| OAI-02 | Hooks 包含 `UserPromptSubmit`、`Stop`、`Interrupt`；轮级事件可带 `turn_id`。`Stop` 可触发继续执行，`Interrupt` 不覆盖子 Agent。 | 最终状态须与源终态核对；不能把一次 Stop 当成已确认最终完成。 |
| OAI-02 | Hooks 的 `transcript_path` 指向的日志格式不是稳定接口；部分工具路径没有工具 Hooks。非托管 Hooks 要经原生信任审核。 | 日志适配按版本保存脱敏 fixtures；Hooks 覆盖率需实测，不能宣称完整文件审计。采集 Hook 仅追加本地队列，不返回改变任务行为的指令。 |
| OAI-03 | 本地状态目录由 `CODEX_HOME` 决定，默认 `~/.codex`。 | 探测实际路径和运行环境；不硬编码桌面、CLI、WSL 共用一套目录。 |
| OAI-04 | Remote 使用连接主机的环境，要求主机在线、醒着及功能可用；Handoff 需要匹配的已保存 Git 项目，子目录须匹配，且会中断当前响应。 | 保留原 PRD 的分层设计。通用上下文包是跨 Agent 方案，原生远程和交接为条件增强。 |

OAI-01 的协议能力没有证明第三方新进程能订阅当前用户桌面实例的全部活动。任何这样的实现承诺都必须有当前安装版、具体连接方式和真实双会话探针证据。App Server 的 WebSocket 文档仍标记实验与不支持生产；本项目不以暴露其控制端口作为 NAS 采集架构。

### Hermes

| 依据 | 已确认事实 | 对本项目的含义 |
| --- | --- | --- |
| HER-01 | `get_hermes_home()` 解析上下文覆盖、`HERMES_HOME` 与平台默认；Windows 当前默认 `%LOCALAPPDATA%/hermes`，macOS/Linux 默认 `~/.hermes`。Profile 有独立数据库。 | 复制其路径解析规则或读取明确配置，不为采集而初始化会自动迁移的 Agent 数据库对象。 |
| HER-01 | 压缩可将旧消息标为非活动并插入保留上下文；正文和时间相同的跨代记录可以合法存在。部分回复正文在 `codex_message_items` 中。 | 保留原始主键、活动/压缩状态和消息侧字段；不能按正文加时间去重，也不能只读 `content`。 |
| HER-02 / HER-03 | `session_model_usage` 的维度键含会话、模型、供应商、端点、计费模式、任务；数据是累计量。历史迁移曾由会话总量播种该表。 | `first_seen/last_seen` 不等于逐请求时间；此表不能恢复完整每日分布，不能和 `sessions` 累计量再相加。旧播种记录不能当成真实模型切换明细。 |
| HER-01 / HER-03 | 在线存储文档的版本说明与迁移表存在不同步；代码也包含相同版本号但主键形状不同的修复。 | `schema_version` 仅为证据之一。必须探测列、主键与索引，生成结构指纹。 |
| HER-04 | Gateway、Plugin、Shell、Outbound Hooks 是不同机制；Gateway `agent:start/end` 不覆盖 CLI/TUI/Desktop，消息字段有截断。 | 不用 Gateway Hooks 作为全部入口的全文或耗时来源。对用户实际使用入口分别验收。 |
| HER-04 | `on_session_start` 只在新会话首轮触发；`on_session_end` 每轮终结触发，退出路径可能有精简字段；`post_llm_call` 仅成功轮触发。 | 候选轮次配对为 `pre_llm_call` 到 `on_session_end`，按 `turn_id` 核对，处理失败、中断和旧 payload；不能直接按 start/end 名字配对。 |
| HER-05 | 请求级观察点为 `pre_api_request`、`post_api_request`、`api_request_error`；辅助调用另有 `pre_auxiliary_call`、`post_auxiliary_call`。 | 安装版支持时，用请求关联字段统计用量与请求耗时；单独核对标题、压缩等辅助调用，避免漏算或重复。 |

最小字段探测清单：

- 消息：`id`、`session_id`、`role`、`content`、`timestamp`、`tool_calls`、`tool_call_id`、`tool_name`、`token_count`、`finish_reason`；可选 `active`、`compacted`、`observed`、`codex_message_items`、`display_metadata` 等。
- 用量累计：`session_id`、`model`、`billing_provider`、`billing_base_url`、`billing_mode`、`task`、各类 token、费用、`cost_status`、`cost_source`、`first_seen`、`last_seen`。不得把字段默认值零直接解释为实际观测为零。
- 实时请求：`session_id`、`turn_id`、`task_id`、`api_request_id`、`api_call_count`、`model`、`response_model`、`usage`、`api_duration`、重试与错误字段。是否存在、单位及覆盖范围以安装版探针为准。

### SQLite 与 Python

| 依据 | 已确认事实 | 对本项目的含义 |
| --- | --- | --- |
| DB-01 | WAL 要求参与进程在同一主机，不支持通过网络文件系统让多机共用。长读事务还可能阻碍 checkpoint。 | 中央数据库位于 NAS 本地持久卷，由服务访问；终端经 API 上传。采集源库使用短只读事务。 |
| DB-01 | 官方 WAL-reset 修复在 SQLite 3.51.3 及以后，另有 3.50.7 / 3.44.6 官方回补。该问题有多连接同时写入或 checkpoint 等触发条件。 | 本项目自带数据库运行时采用修复版本或有可验证修复证据的构建。检查实际链接的 SQLite 版本，不只看 Python 版本。不得因此自动修改源 Agent 的运行时或数据。 |
| DB-02 | Online Backup API 可在源数据库仍被使用时生成一致备份。 | 备份使用一致性接口并验证恢复；不以复制活跃 `.db` 单文件代替备份。 |
| DB-03 | FTS5 trigram 全文查询不能匹配短于 3 个 Unicode 字符的子串；某些 LIKE/GLOB 条件会退化为线性扫描。 | 中文一至二字搜索必须有明确限范围回退；不能只写“启用 FTS5 即支持全部中文搜索”。 |
| DB-04 | Python `sqlite3.connect()` 支持 `file:…?mode=ro` 配合 `uri=True` 打开只读数据库。 | 正确构造和转义本地文件 URI；权限或 WAL 配套文件问题应报能力受限，不能改用写模式迁移源库。 |

“只读”指不修改源业务数据及配置，不代表可忽视 SQLite WAL 的配套文件要求。活跃数据库不能伪装成 immutable；发生兼容问题时，记录错误并选择受支持的读取或一致快照路径。

## 2. 待实机验证项与 P0 证据

1. 记录每个实际使用的设备、执行环境、入口、Profile、程序版本、数据目录、数据库结构/协议 schema 指纹。
2. 历史读取一条已存在会话，核对正文、角色、消息数、模型、用量及源定位；明确归档、压缩、缺失范围。
3. 新增两轮及一次工具调用；再测追加输入、取消、失败、重启和两个已有桌面会话并行，证明采集覆盖现有使用方式。
4. 测模型切换、压缩、重试、子 Agent、辅助调用，以及 Hermes 转调用 Codex 时的计费覆盖与潜在重复。
5. 每项能力分别登记 `supported / estimated / missing / incompatible / unverified`、证据引用和验证版本。在线文档存在但实机未测必须是 `unverified`。
6. 原生 Remote / Handoff 只登记可用性，不作为 MVP 采集前提；真实跨机迁移留在相应增强阶段验收。

建议通过门槛：历史读取和新增轮次采集各有脱敏证据；事件能和源记录对账；未覆盖入口可列明；不可得的时间或用量明确降级。没有要求恢复原平台从未保存的历史。

## 3. 官方来源索引

以下链接均在本轮打开核查。在线页面与 `main` 代码可能继续变化；实施时保存访问日期及与安装版对应的 schema/代码提交，不把这里的在线当前状态当成用户已安装能力。

| ID | 官方来源 | 链接 |
| --- | --- | --- |
| OAI-01 | OpenAI / ChatGPT Learn：Codex App Server | [App Server](https://learn.chatgpt.com/docs/app-server) |
| OAI-02 | OpenAI / ChatGPT Learn：Hooks | [Hooks](https://learn.chatgpt.com/docs/hooks) |
| OAI-03 | OpenAI / ChatGPT Learn：Advanced Configuration | [Config and state locations](https://learn.chatgpt.com/docs/config-file/config-advanced) |
| OAI-04 | OpenAI / ChatGPT Learn：Remote Connections | [Remote / Handoff](https://learn.chatgpt.com/docs/remote-connections) |
| HER-01 | Nous Research：Session Storage | [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage) |
| HER-02 | NousResearch/hermes-agent：当前声明 schema | [hermes_state_common.py](https://github.com/NousResearch/hermes-agent/blob/main/hermes_state_common.py) |
| HER-03 | NousResearch/hermes-agent：迁移与结构修复 | [hermes_state_schema.py](https://github.com/NousResearch/hermes-agent/blob/main/hermes_state_schema.py) |
| HER-04 | Nous Research：Event Hooks | [Event Hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) |
| HER-05 | Nous Research：Plugin Hook reference | [Plugin Hooks](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#hook-reference) |
| DB-01 | SQLite：WAL 与 WAL-reset 修复 | [Write-Ahead Logging](https://www.sqlite.org/wal.html) |
| DB-02 | SQLite：Online Backup API | [Backup API](https://www.sqlite.org/backup.html) |
| DB-03 | SQLite：FTS5 trigram | [FTS5](https://www.sqlite.org/fts5.html) |
| DB-04 | Python：SQLite URI 只读模式 | [sqlite3](https://docs.python.org/3/library/sqlite3.html) |

原 PRD 中的 `developers.openai.com/codex/…` 来源在本次读取时重定向到相应 ChatGPT Learn 页面；这是来源地址变化，不改变其作为官方资料的性质。

## 4. 本轮没有验证的事项

没有验证 NAS 真实型号、CPU、内存、剩余资源、Docker 配置、联网通路或运行性能；没有验证用户两端 Agent 的版本、原始会话字段及实时事件覆盖。文档中的资源、延迟、容量等指标是工程预算和验收目标，不能写成已经实测。上述官方事实也不能替代部署验收和用户环境的兼容性报告。
