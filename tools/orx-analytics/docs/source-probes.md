# 数据源探测基线（2026-10-06，本机实测）

本文保留当时的探测事实和调查进展。当前版本支持以
[compatibility.md](compatibility.md) 为准；下文旧版本 gate、旧 README
缺陷和机器上的行数均不是当前支持声明。

执行：ZCode 会话内逐源实测（见 Goal Phase 1）。能力矩阵与已知缺口。

## zcode — `~/.zcode/cli/db/db.sqlite`

- 只读 + WAL 读取正常；表齐全：`session`(942)、`turn_usage`(1008)、
  `model_usage`(30616)、`tool_usage`(37784)、`part`(237422)、`message`、
  `session_task_link` 等。
- 父子关系：`session.parent_id`；实测 `sess_64094b77…` 树 = 1+10。
- token：`turn_usage` 每 turn 一行（input/output/reasoning/cache_read/
  cache_creation + 请求/重试/工具计数）；`part.step-finish` 逐步
  `tokens{input,output,reasoning,cache{read,write}}` —— 两种口径实测逐位
  一致（R003 树 87,427,489）。
- `model_usage` 有 `logical_request_id`+`attempt_index`+status/时间，**无
  token 列**：请求级 token 不可得，只有 turn 级与 step 级。
- 会话↔项目：`session.directory` 精确路径。
- 缺口：无逐请求 token；无每会话取消/中断标志（靠 turn status 与最后消息
  推断，标记为推断）。

## Codex — `~/.codex/sessions/**/rollout-*.jsonl`

- JSONL 事件流：`session_meta`（含 cwd、id）、`turn_context`（cwd、model、
  effort）、`token_usage_record`（thread_id/turn_id/response_id +
  usage{input,cached_input,cache_write,output,reasoning_output,total}）、
  `event_msg`（task_started/task_complete/token_count）、`response_item`
  （消息正文）、`world_state`。
- 会话 id 即 `session_meta.payload.id`（与 ORX codex 适配器捕获的
  thread.started.thread_id 同源）。
- 增量读取：按文件 + 行偏移游标。
- 缺口：重试语义无显式字段（attempt 类概念不存在）；错误以 event 形式
  内嵌。R003 planner thread `01a10710-d575…` 的 rollout 文件在本机未找到
  （该 id 仅在 10/05 会话文本中被引用）——记录为缺口，不伪造。

## Cursor — `…/Cursor/user/globalStorage/state.vscdb`（1.36 GB）

- `composerHeaders`(208)：composerId、workspaceId、createdAt、lastUpdatedAt、
  isSubagent、subagentTypeName。
- `cursorDiskKV`(102814)：`composerData:*`(508，含 `usageData`、`modelConfig`、
  `createdAt`、`name`)；`bubbleData:*` 在本机 globalStorage 为 0。
- workspace 映射：`workspaceId` == `workspaceStorage/<hash>`，其
  `workspace.json.folder` 为项目 URI（实测 ORX=669d2969…）。
- 缺口：逐消息 token 明细在本机不可得（bubble 为空）；用量取 composer 级
  `usageData`，字段缺失记 unknown。1.36 GB 库查询必须走索引前缀。

## ORX — `<project>/.orx/state.db`

- 契约 `docs/observability-contract.md`（gate 收 {8,9,10,11}，第四/五修正案后
  工具同步于 2026-10-06）；本机 ORX 项目 schema v11（G007 起；R006 及更早库按其创建版本只读）。
- R003 复核：10 worker attempt `session_ref` 全 NULL；planner attempt 85 有
  codex thread id。
- 已知缺陷（待修）：README.md 两处仍写 "schema v7"（gate 实收 {8,9,10}）。

## 83.42M / 87.43M 差异调查基线

- 87.43M = `sess_64094b77` 树（11 会话）`turn_usage.input+output` 总和
  87,427,489；`step-finish` 逐步累计同为 87,427,489（互证）。
- 83.42M 仅存在于 Goal 文本引用，本机无原始报告工件；候选口径全部数值排除：
  仅子会话 78.96M；子会话+父第一轮 80.33M；树+planner(codex 观测 0.41M)
  87.84M；仅 input 87.00M；cache_read+output 85.94M。
- 结论口径：87.43M 已验证可复算；83.42M 出处未知，差异**保留未知**。
