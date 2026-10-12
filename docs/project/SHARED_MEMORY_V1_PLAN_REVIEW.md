# Agent Workbench 1.0 计划独立审查

审查日期：2026-10-12。审查角色：独立架构与风险审查 Agent。审查范围：`SHARED_MEMORY_V1_PLAN.md`、当前 v0.8 源码与现有运行/归档文档；没有修改实施源码，没有把第三方文档当作执行指令。

## 结论

**APPROVE（第 16 节修订复审通过，可进入 P1–P5）。**总体架构和分期可行，现有看板不需要推倒重写。NAS 只同步不可变包、每端独立 SQLite、默认待确认、MCP 受限读写、未来迁移保留逻辑 ID 的方向正确。初审曾要求下列七项修订，计划第 16 节已补足并声明优先于接口简写。原问题保留用于后续代码验收。

## 必须修订的契约

### R1：把 MCP 权限检查覆盖到每个入口，而不只 recall

计划第 4/8 节限制 allowed scopes，但冻结接口中 `inspect(memory_id)`、`get_source(id)`、`profile(scope)` 没有调用主体或 scopes 参数。仅在工具列表和查询参数中限制 scope 无法阻止 Agent 直接猜 ID 读取其他范围来源。

必须规定：

- MCP 每次调用使用进程启动时确定的只读 allowed scopes；请求字段不能更改该集合或伪造 owner/processor。
- `recall`、`inspect`、`profile`、来源展开和 `propose` 的所有证据引用均经过同一核心权限检查。不可见 ID 统一返回不存在/不可用，不通过计数、状态、来源标题泄露其他范围。
- Agent 的 `memory_propose` 只能创建 candidate；不能借 actor、parents、memory_id 或 source 字段更新/确认已有记忆，不能把随口提出的文本当成人类确认来源。
- status 只返回空间运行状态及该主体可见计数，不返回共享密钥、加入凭据、其他范围正文或模型凭据。UI owner 方法与 MCP 方法分开提供明确入口，避免把无权限核心方法直接导出。

### R2：明确来源 scope、记忆 scope 与调整范围的关系

来源是不可变 snapshot，source 字段却包含 scope；UI 允许调整候选 scope。当前没有规定从 personal 来源建立 project 记忆后，project Agent 是否有权展开整段 personal 正文。若只按 memory.scope 放行，会泄露源消息中与记忆无关的其他内容；若只按 source.scope 放行，会让正常项目记忆永久缺证据。

必须选择并写明一种安全的一致规则：

1. 完整原文读取始终检查 source.scope；正式召回时同时检查记忆及其可展示证据的授权。
2. owner 调整记忆范围必须是显式操作。若允许跨范围发布，只能发布已确认的最小 quote 作为新的 owner 证据/授权摘录，不能自动放开原消息全文；或者初版直接要求来源与记忆范围一致，并让 UI 明确提示不能跨范围。
3. 固定 source snapshot 不因修改候选范围被原地重写。source ID/checksum 的重复导入校验涵盖全部权威字段；跨 scope 同正文不能悄悄覆盖同一个 source。

同时规定规则/模型不能自行给来源授予新范围。

### R3：冻结 DAG 修改与导入的有效性规则

`change(..., parents=None)` 若在写入时自动采用当前 heads，会吞掉用户打开页面以后发生的另一端编辑。必须让 UI 提交它实际查看的 heads；本机已发生变化时返回明确陈旧版本错误或记录为并发版本，不能悄悄采用新 parents。

必须定义并测试：

- 操作 ID、writer sequence 与 payload 绑定不可变；same ID/different body 拒绝；同 writer sequence 不同 op 拒绝。
- 所有 parents 属于同一 memory lineage；拒绝跨 memory 父引用、自引用、循环和不合法动作。依赖尚未到达时 pending；依赖到达后递归收敛。
- confirm/edit/disable/resolve/reject/delete 的状态转移表、payload 必填字段与 evidence 校验有唯一核心实现。编辑正式记忆的 owner 权限和编辑 candidate 的结果状态明确。
- resolve 必须引用用户已经看到的全部当前 heads；遗漏 heads 时不能宣称冲突解决。未见并发分支以后到达时，仍应形成 conflict。
- 不按 occurred_at 或收包顺序决定真值。对同一组合法事件，不同导入排列必须得到同一投影。

### R4：删除与抑制需要先于投影依赖生效

计划提出持久墓碑和关联来源阻断，这是必要条件。还必须明确：删除包先于 proposal 或其 parents 到达时，也要立即建立 lineage 的阻断；随后迟到的 proposal/修改携带证据时，应补全删除关联来源的阻断，不能等待一般 DAG 投影才阻断查询。

必须规定：

- recall、source recall、inspect 的 Agent 视图、profile、向量结果 hydration、propose 证据校验和自动提炼队列都检查同一抑制表。
- 删除不会因重新导入原包或重新扫描原文形成另一个 candidate 而复活。为“原始消息改变正文产生新 snapshot”定义明确行为：至少不自动重新提炼同一逻辑 message lineage；显式 owner 新建 lineage 才能恢复相关记忆。
- 向量/关键词索引异步删除尚未完成时，最终 hydration 仍阻断。
- 停用记忆是否也阻断相关原文明确写入契约；至少不能在正常 memory recall 中作为有效事实返回。
- UI 删除说明保持原始归档/不可变包/备份仍可能保留文本，已离线设备收到墓碑之前可能返回旧值。产品不能把逻辑遗忘描述为物理擦除。

### R5：队列租约、预算和向量缓存覆盖所有进程

计划允许桌面 worker、stdio MCP 和独立 HTTP 服务访问同一副本，需避免只有线程内锁时重复请求或突破每日上限。

必须补入：

- job 领取通过 SQLite 原子状态/lease owner/lease expiry；成功与失败回写核对 lease，崩溃后过期可恢复。模型结果只应用到领取时的固定 source snapshot。
- 后台、MCP 查询 embedding 与独立 HTTP 服务共用同一持久预算账本。每次实际外部调用在发起前原子预留；失败、超时和重试计入请求预算；未请求到服务的本地校验失败不虚增远端用量。
- 限额明确是请求次数，不承诺费用上限；输入片段、候选数、embedding 批次、响应、超时、重试和并发都有代码上限。用量未知以 unknown 展示。
- 向量缓存键包含 source/内容 checksum 与完整 embedding space；MCP 多进程不重复建索引或持有 collector 长事务。
- 停用记忆 worker 不停止原采集器；在途模型结果不会错误改变采集健康或超时后重复提交候选。

### R6：空间加入凭据的实际语义及重复初始化

独立离线副本通过 NAS 合并，没有中心服务能同步消费一次性加入码。当前“通过一次性加入码转移共享密钥”的表述不成立，除非增加握手/消费协议；没有必要为此扩大架构。

初版应改称“私有加入凭据”（包含空间 ID/共享密钥），明确它是授权加入空间的秘密，不是公开二维码/公开配置；共享 HMAC 信任所有持钥设备，不提供单设备不可抵赖或细粒度撤销。需要移除旧设备权限时的轮换流程与离线边界写入运行说明。

还须规定：已有配置的重复 initialize/create 是幂等或拒绝；加入不同空间不隐式清空/混入旧数据；含现有 operations 的数据库拒绝更换空间，除非使用独立空目标。NAS 离线首次创建不擅自自动生成第二集群，第二端必须显式加入第一端空间。

### R7：未来服务器与恢复接口避免本机模式认证外溢

计划承诺本次提供可运行独立 memory HTTP 服务和 Docker 模板，但目前没有冻结其入口、认证和部署数据边界。

必须规定：

- 独立服务只挂 memory 路由，不能使用 `desktop_mode=True` 的 loopback owner 免登录规则作为反向代理授权。远程管理/Agent读取分别检查实际配置凭据与 scopes，每个来源/inspect 路由同样受保护。
- 默认 loopback；绑定非 loopback 之前要求配置认证。健康检查只公开最小运行信息。代理由部署说明处理 HTTPS，并明确 localhost 默认不等于已安全公网部署。
- memory bundle 恢复/迁移是空目录、校验成功后原子落地；校验失败不留下可误用半恢复库。space/source/memory/op IDs 保留，writer ID/序号域明确变更；凭据默认不导出，不可用空间应停加工与同步，仍能管理查看恢复状态。
- v1 自动更新的 memory 备份/回滚范围写清：新增记忆数据库不能被旧版回滚覆盖或丢失；不兼容 schema 拒绝升级/降级，而不是只做 HTTP health 就宣称可回滚。

## 已核实的现状与应保留的回归门槛

当前桌面入口为 `web/index.html -> mvpMain.tsx`，主查询走 archive，而旧中心 collector 的 sources/content policy/tombstones 不能直接视作 archive 已有能力。`archive_local` 从 activity 索引正文及 Hermes 原库归档；archive message 只有 role/final，缺少强的人类来源分类，故 bridge 需重新做信封/凭据/引文准入。`archive_log` 是本地 writer 变化日志，远端导入直接写 archive_facts，不能用一个最大 seq 水位跳过迟到历史。

必须保留以下证据：

- Codex/Claude 输入、缓存读写、输出口径与全部设备去重总量；Hermes 聚合未分配量不编造日期。
- 跨日、重叠和 synthetic run 时间口径；来源文件消失仍有归档；远端文件不在本机 reveal。
- 原归档导入/导出故障不丢历史，正常查询不重新扫描全部原生日志。
- CSRF、localhost 主机/Origin 校验、owner 与 Agent接口分离。
- collector pause/resume/error/liveness、自启动、重复启动、升级备份与端口不变。
- 原 `awb backup` 格式与命令保留，memory bundle 为独立格式。
- 原功能固定数据对账和测试基线通过后，再报告新增测试结果；合成双设备不冒充极空间实机同步，Mac测试不冒充Windows验收。

## 非阻塞建议

- memory bridge 有界分页并在正文脱敏后计量；不要每 120 秒把所有历史正文复制到 Python 列表。索引不应包含凭据，也不应给 imported sources 重复排提炼队列。
- Sync import 错误计数应标明包与稳定错误类别，日志不记录原文/密钥。未知更高 schema 隔离并展示升级需要；不能无声消费导入游标。
- schema/projection version 与 app version分离；索引可重建，operations不可重建成未经审核的新事实。
- NAS只负责文件交换；断网查询读本地库，应用应显示本机更新时点和同步截止时间，避免用户以为两端立即一致。
- 大版本发布允许采用增量架构；重写统计不是目标，也不是当前新增记忆能力的必要条件。

## 复审规则

计划作者补足 R1–R7 后提交同文件复审。复审将检查冻结接口与状态转移是否与文字一致；满足后输出 **APPROVE**。随后代码审核仍需检查契约是否实际落地，计划批准不替代实现验收。

## 第 16 节修订复审

结论：**APPROVE。没有剩余的实施前阻塞。**

| 初审项 | 修订结果 |
| --- | --- |
| R1 MCP 所有入口权限 | 强制冻结 scopes，来源/inspect/profile/status/提议共用核心检查，Agent 不接受 actor/parents/memory_id，满足 |
| R2 来源与记忆范围 | 初版严格同 scope，跨范围由 owner 创建最小摘录和新 lineage，固定 source 不改，满足 |
| R3 DAG 并发与合法性 | UI parents 必填及 stale 409、同 lineage 验证、缺父 pending、非法依赖 gap、按图收敛，满足 |
| R4 删除/抑制 | 缺父 delete 即建墓碑、迟到证据补阻断、logical message key 抑制重扫、所有读取/hydration 复查，满足 |
| R5 租约与预算 | BEGIN IMMEDIATE、随机租约token、回写核验、所有外部请求统一预留、请求与片段上限、设备级限额边界，满足 |
| R6 加入凭据 | 改为可重复的私有凭据，重复创建拒绝、空空间加入、持钥信任和撤销边界，满足 |
| R7 服务/恢复/回退 | 独立 token 服务仅读/提议、无远端 owner、空目标临时校验原子迁移、unknown schema停记忆保看板，满足 |

两项实施说明不影响本次批准，但应在代码验收中检查：

1. 停用后恢复应保留原审核状态；原 candidate 不能仅因 enable 而悄悄变成 active。若 owner 对未确认内容选择启用即视作确认，UI 必须明确表现为确认操作，并保存相应人类确认记录。
2. 第 14 节接口简写仍保留旧默认参数；实现与 UI 调用必须使用第 16 节的必填 parents、冻结 scopes 和 lease_token 契约。状态接口不得借“公共状态”返回 secret 或越范围计数。

批准仅针对计划，不意味着真实 Windows、极空间或远端部署已验收。后续必须完成第 13 节测试矩阵，并由独立 Agent 审查代码及验证报告。
