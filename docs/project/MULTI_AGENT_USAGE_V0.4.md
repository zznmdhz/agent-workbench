# v0.4.0 多 Agent 用量与时间查询口径

## 来源

| Agent | 原始来源 | 精度 | Token 归一化 |
| --- | --- | --- | --- |
| Codex | `~/.codex/sessions` 与 `archived_sessions` JSONL | 请求级，按 `token_count` 发生时间 | 原始 input 已含 cache read；新输入 = input − cache read，缓存写入未提供 |
| Claude Code | `~/.claude/projects/**/*.jsonl` 的 assistant `message.usage` | 请求级，按原始 timestamp；同一 message.id 选已结束／输出更完整的快照 | input、cache read、cache creation、output 分别计入 |
| Hermes | `%LOCALAPPDATA%/hermes/state.db` 的 `session_model_usage` | 会话／模型累计汇总，不是逐请求 | input、cache read、cache write、output 分别计入；api_call_count 是汇总调用数 |

所有来源只读。总处理量 = 新输入 + 缓存读取 + 缓存写入 + 输出；来源缺项按该来源已知语义处理，不估算不存在的值。Claude 有些原始请求没有缓存字段，来源卡显示缺失条数；这些总量只是已记录量，可能低于实际值。此总量是本机日志中的来源之和，不是订阅账单或跨设备全局配额。

## 日期

起止日期以香港时间解释，查询上限十年。1–31 天按日、32–180 天按周、更长按月显示请求级趋势。Hermes 每条汇总只有 first_seen 和 last_seen；只有两者完整落在所选时段内才纳入卡片和模型表。跨越区间边界的记录计入“暂未计入”数量，不把整条归到某一天。已经纳入的 Hermes 总量在趋势中列为“未分配日期”，与请求级趋势相加才等于总卡片。

来源卡显示原生记录的实际最早／最晚时间。早于此时间的选定月份为 0，不能解释为工具只保留 15 天。本机 2026-09-26 检查时，Codex 最早 2026-07-03、Claude 最早 2026-06-28、Hermes 最早 2026-08-09；这些日期取决于用户当前电脑，其他安装实例会不同。

## 去重与刷新

Codex 沿用 v0.3.0 的请求级去重与 fork 排除规则。Claude 依据 message.id 跨文件去重：有 stop_reason 的快照优先；同等结束状态选输出更完整的快照；最后丢弃所有计费维度均为 0 的请求。文件变化后原子重建 Claude 索引，原始 JSONL 不写入。Hermes 每次只读查询 `state.db` 的汇总表，避免把会话汇总误当每日增量。

## 验收与限制

以合成重复／跨日记录检验去重和边界；以真实本机来源验证三种 Agent 非零用量、来源日期、长区间筛选及总量算术。Hermes 缺少逐请求时间与数量时，不推断每日 Token。Claude Code 可能使用第三方模型；“Claude”表示 Agent 来源，不保证模型一定由 Anthropic 提供。
