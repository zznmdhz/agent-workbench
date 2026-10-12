# Agent Workbench 1.0 独立代码审查

审查日期：2026-10-12。审查基线：隔离 worktree 中正在集成的共享记忆实现。审查者没有修改实现源码，补充了 `tests/test_memory_review.py` 的跨模块场景。

## 当前结论

**APPROVE。**架构保持增量隔离，现有 API/统计/归档主体未被重写；范围权限、操作 DAG、删除墓碑、独立处理线程、MCP 官方 SDK、凭据独立存储、未来 HTTP 读取接口均已有具体实现。C1–C4 均已修复并复跑通过，当前没有剩余交付阻塞。下文保留初审问题及修复依据，不代表它们仍存在。

### C1：失败的 owner 更正仍写入原文证据（阻塞）

`memory_api.change` 在调用 `store.change` 前单独创建 owner source。UI 以旧 heads 提交更正时，change 正确返回 409，但刚创建的来源和词项已提交，正常 raw recall 仍能找到本次未保存更正的正文。

复现：独立 `test_review_stale_owner_correction_has_no_raw_recall_side_effect`。创建已确认记忆 -> 成功更正 -> 旧 heads 再提交不同正文 -> 返回 409 -> `store.sources()` 仍含拒绝正文。

修复要求：新 owner 来源与记忆操作置于同一 core 事务，heads 校验、来源准入、操作插入及投影一起成功或一起回滚；不能只在 API 提前比较 heads，跨进程并发仍可能发生在比较之后。

### C2：HTTP 与 MCP 召回预算范围不一致（阻塞）

HTTPAdapter 的实际可选预算上限为 32000 字节，但 HTTP `AgentRecall` 校验上限为 24000。使用 SDK 工具允许的较大预算，经真实 HTTP 服务会返回 422。

复现：独立 `test_review_http_adapter_real_service_budget_and_narrow_status` 使用真实 FastAPI 服务和 MockTransport 转发，scope/状态/inspect 均通过，budget=32000 召回失败。

修复要求：客户端、HTTP 模型、核心召回和 prompt adapter 使用一致边界；所有路径都应对合法预算成功，对超限输入明确限制。

### C3：每日模型限额消耗来源失败次数（阻塞，实施 Agent 已在修）

`claim_jobs` 先递增 attempts，随后模型预算耗尽被当作普通失败有限重试。连续三次预算失败会让尚未发起模型请求的来源永久 failed，第二天限额恢复后仍不再处理。同时已有规则候选在错误分支没有提交。

复现：独立 `test_review_budget_exhaustion_defers_without_permanent_failure` 在规则可提炼的来源上注入 BudgetExceededError；当前 attempts 为 1，没有保留预期规则结果。

修复要求：预算耗尽独立延期至下一设备 UTC 预算窗口，不计入来源失败 attempt；保留已产生的规则候选。真实网络/模型格式失败仍有限重试，并提供 owner 的失败任务重试途径或明确恢复行为。

### C4：同机不同进程被错误判为复制数据库（新增阻塞）

clone guard 的 `machine_binding` 包含 `uuid.getnode()`。在本次 Mac 的 Python 环境中，MAC 地址发现失败，该 API 使用随机 node fallback；因此 parent 与 child 的 platform/hostname 完全相同，node/binding 却不同。真实官方 SDK stdio 测试读取正常，但 `memory_propose` 返回 `replica_binding_mismatch_use_restore`。正常应用重启同样会禁用写入、同步与外部模型预算。

复现证据：独立 64 项 memory 套件 63 通过、1 失败，失败为 `test_real_official_mcp_stdio_initialize_list_and_tools` 的真实 subprocess 提议。另运行同一解释器父子进程对比，`uuid.getnode()` 均为 multicast 位已设置的随机 fallback，binding 不一致。

修复要求：使用稳定的 OS 机器身份，或者至少排除 `uuid.getnode()` 的随机 fallback；不得把正常重启视作克隆。需新增真实 subprocess 的 binding 一致性与 memory 写入测试，保留复制到不同机器的只读保护。

## 已修复并复核的前期问题

- MCP actor 统一为 `assistant_proposal`，不能通过工具 actor/parents/memory ID 升级权限。
- HTTP adapter 的预算字段与 HTTP 模型已有接线；较大预算范围仍需 C2 对齐。
- HTTP 的 status/inspect/source/profile 接受只缩小权限的 scopes，scope 不可扩张，越范围返回不可用。
- embedding 索引调用 kind 与预算账本统一，维度 0 规范为 provider 默认，不再导致无条件语义降级。
- `hybrid_recall` 在外部 embedding 延迟后以新投影替换旧 payload；独立慢请求更正场景通过。
- worker 检查 `has_join_key`，恢复缺凭据时停加工/同步。
- 复制 writer 身份不再悄悄继续旧流；正式 restore 重置 writer 域，桌面能加载已恢复库。
- SQLite journal 与原看板采用相同安全版本规则及 FULL synchronous。
- 缺父 delete 含完整 snapshot 及历史 blocked_sources，证据晚到时补 message key 抑制，原文/新 body snapshot 不复活。
- UI 空同步目录采用空字符串，API base 提示与模型 URL 拼接一致；恢复凭据入口已有补充。

## 已执行的审查证据

1. 初次新增 memory 五套测试：49 通过、1 失败（delete-first 原文泄漏）。该问题由 core Agent 修复并增加晚到证据测试；独立复核相应复杂场景已通过。
2. 独立跨模块审查七项：4 通过、3 失败，失败对应 C1–C3。
3. 已通过的独立场景：restore 保留逻辑记忆并可在新桌面加载；future schema bundle 不留半恢复目标；delete-first -> evidence 晚到 -> proposal 晚到 -> changed body 不复活；慢 embedding 期间 owner 更正返回新正式内容。
4. 代码检查：frozen macOS CLI 复制路径与 API agent-config 路径一致（Contents/Resources/cli/AgentWorkbenchCLI）；Windows console CLI 位于安装程序旁，不使用 windowed launcher 承担 stdio。实际冻结包运行由 root 验收。
5. 更新备份新增 memory online snapshot；原 backup 命令保留独立格式。程序回滚不覆盖新 memory 事件，unknown schema 只停记忆不关闭原看板。
6. 独立 HTTP 服务只提供 Agent 读/提议能力，Bearer 身份与固定 scopes；没有把 localhost owner 免登录功能暴露给反向代理。

## 实际部署与范围

目前 NAS 双端验证为隔离目录及事件包传输模拟，没有证明极空间真实传输或 Windows 安装恢复。Docker/compose 为未来部署模板，不能描述成已在远端服务器部署成功。Owner 管理由桌面 UI 完成；远端 HTTP 是读/提议接口，不是完整多租户/公开 SaaS 管理服务。

## 复审门槛

C1–C3 已修复，独立七项全部通过；owner 更正使用 core `change_owner_text` 同一事务，预算使用 `defer_job`，HTTP 上限统一。C4 改为稳定 OS 机器身份（Mac IOPlatformUUID、Windows MachineGuid、Linux machine-id，失败时稳定 hostname），排除随机 MAC fallback。

最终复审命令：

```sh
uv run --extra memory-mcp --group dev pytest -q tests/test_memory_review.py tests/test_memory_core.py tests/test_memory_sync.py tests/test_memory_processing.py tests/test_memory_mcp.py tests/test_memory_api.py
uv run --extra memory-mcp --group dev ruff check src/awb/memory tests/test_memory_review.py src/awb/memory_api.py src/awb/memory_http.py src/awb/memory_bundle.py
```

结果：**69 passed**，约 25.7 秒；相关 Ruff 全部通过。存在一条第三方 FastAPI/Starlette TestClient 弃用提示，未导致功能失败。真实 MCP stdio initialize/list/read/propose、双端 NAS 包模拟、并发与删除、预算次日恢复、原子恢复等场景均通过。

root 的完整原套件、生产前端构建与实际包运行验收仍是必要证据。本审查没有把 Windows/极空间/未来远端服务器的模板或模拟当成实机验收。

## 非阻塞产品与部署说明

- 每 route 每日限额还受全局 `model_daily_call_limit` 限制；UI 应显示实际总上限，以免只调整 route 值后误解有效预算。
- 删除/停用一条记忆会阻断其共享来源；同源支持的其他记忆可能同时不参与自动召回。管理页面保留历史，这个保守抑制效果应在说明中明确。
- future Docker 的 writer binding 在 machine-id 缺失时依赖 hostname，应为容器配置稳定 hostname。恢复在最终执行环境操作，目标为持久卷下的空子目录，不能把挂载点本身当作可原子 rename 的恢复目标。
- prompt 使用显式 `--query`，运行手册不能声称当前实现读取 stdin。

## 最终增量复核

root 完成上述产品建议后再次提交审核。本次只读复核结果仍为 **APPROVE**，没有新增发布阻塞。

- owner `retry_failed_jobs` 仅由管理 UI 调用，路由受 CSRF 保护；领取重试只针对本地 failed 且未被 source/message tombstone 抑制的来源，重置工作失败次数不清除模型账单及当日限额。
- NAS 包导出覆盖 rename 已落盘、SQLite 导出水位未提交的崩溃窗口。恢复先验 HMAC/摘要/空间/设备/包序号，从已落盘的同序号包恢复 source/operation checkpoint；新消息不能使旧序号包重新生成不同摘要。故障注入场景通过。
- prompt 现已支持纯文本 stdin 或唯一 `prompt` 字段 JSON，8KiB 输入及 2000 字符查询上限，读取 EOF 最多等待两秒；未知宿主信封、越权附加字段、坏 UTF-8、超量输入及未关闭管道分别给显式 gap。上文原有“仅 --query”建议已被此新增实现替代；手册与实现一致。
- 记忆 UI 已增加总预算上限和失败任务重试入口，恢复私有加入凭据使用已配置但缺密钥的空间分支；冲突解决按钮明确包含 owner 确认含义。
- 独立 HTTP 仍只有读/提议端点，来源/状态范围不能扩大，响应有 64KiB 上限。远端模板固定容器 hostname，数据目标改为 `/data/replica/memory.db`，手册说明在最终容器中恢复到可替换子目录，未宣称集中持续加工已完成。
- 删除说明已明确同源其他记忆可能随共享证据一起停止自动召回，原看板历史保留。

独立复核命令：

```sh
uv run --extra memory-mcp --group dev pytest -q tests/test_memory_core.py tests/test_memory_sync.py tests/test_memory_mcp.py tests/test_memory_review.py tests/test_memory_api.py
uv run --extra memory-mcp --group dev ruff check src tests
```

结果：**56 passed**，约 41.7 秒，全 `src/tests` Ruff 通过。该定向集合重点覆盖新增重试、导出崩溃恢复、stdin 及此前跨模块反例；没有无必要重复全部模型处理测试。

root 报告最新完整套件 **166 passed**（含原 87 项）、全源码 Ruff 和生产前端构建通过；这里区分 root 完整验证与独立审查者亲自运行的定向验证。Mac frozen app/CLI 的实际启动与 stdio 验收仍由 root 进行，不把该计划中测试写成已完成。


## Windows CI 增量复审

首次真实Windows CI检出两项中文stdin子进程测试失败：prompt输出经系统默认cp1252编码。修复改为UTF8 binary stdout，并对Windows stdin/stdout设O_BINARY；输入仍受8KiB/2000字符/2秒EOF边界。新增4项回归，包含强制cp1252且PYTHONUTF8=0的子进程。独立审查Agent亲自运行13项prompt定向测试通过，相关Ruff通过，结论APPROVE。

同时为Windows打包workflow的每个native command增加LASTEXITCODE检查，避免pytest失败被后续Ruff成功掩盖。修复后的跨平台CI run38212041701（commit c1f8a3bae03dc1745697b94ecaab13954701897b）中Windows/macOS/Linux后端与前端全部success；这是CI runner验证，不等于用户Windows机器自启动或真实NAS已验收。
