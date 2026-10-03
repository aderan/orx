---
name: orx-pbv
description: Multi-round Plan-Build-Validate development loop on ORX — one Goal, a sliced master plan (Plan IR), one round per slice (self-contained round plan, build, dual-gate verify, close with report). Use when 用户给出要多轮推进的开发 goal/大任务，或提到 PBV 循环、Plan-Build-Validate、多轮切片开发，或要替换项目级 pbv-loop skill；不用于单轮问答、单次小改动或无需切片验证的任务。
---

# orx-pbv：多轮 PBV 开发循环

## 1. 定位

你是宿主 Controller，ORX 拥有全部执行状态（Goal/Plan/Task/Attempt/Evidence/
Verification）。本 skill 定义多轮 Plan-Build-Validate 开发循环；通用宿主循环
（status→dispatch→claim→complete→verify→done）遵循 orx-controller skill，
子代理纪律遵循 orx-agent skill。不新增状态存储，绝不直接写 `.orx/state.db`。

本 skill 与具体仓库无关：文中路径（reports/pbv/、docs/DEVELOPMENT_PLAN.md）
与门禁命令（`make verify`）是示例（StockMate 为样例仓库），每个仓库按 Round 0
重新发现。安装：`orx skill install orx-pbv`；更新：`orx skill update`。

## 2. Round 0：开始前检查

按序确认，任何一步失败先向用户报告再继续：

1. 目标项目已 `orx init` 且 `orx doctor` 通过（缺可选 CLI 是 warning；认证
   失败让用户先处理，不拿正式调用试错）。
2. profiles.toml 按角色配好：规划 = codex 只读 CLI profile；构建 = host 或
   CLI worker profile；审查 = `[verify]` profile。
3. 确认项目最强验收门禁命令：查 Makefile/scripts/CI，不要凭习惯写。
4. 确认项目任务规划文档（如 docs/DEVELOPMENT_PLAN.md）：主计划必须引用其
   条目；没有就问用户以什么为准。
5. 建留痕目录：`mkdir -p reports/pbv`。
6. 从项目 AGENTS.md / architecture.md（或等价文档）提取 ≤10 条硬约束
   （祈使句），Goal 建立时注入。

## 3. Goal 建立

goal 来自用户消息，不明确就问，不替用户猜。约束与验收在 `orx goal new`
一次性注入——内核会把它们组装进所有 planner/worker/verifier prompt：

```bash
orx goal new --objective "<目标>" \
  --constraint "<硬约束，可重复>" \
  --acceptance "<验收原文，可重复>" \
  --context "<补充上下文>"
```

验收原文逐字注入，不释义；内核 planner 契约要求验收逐字拷进 task
acceptance，逐字性从源头保证。

## 4. 主计划

`orx plan --depth light|standard|deep`（多切片大 goal 用 standard/deep）。
host 驱动返回 waiting planning assignment（prompt+schema）：把 prompt 原样
交给子代理运行，产出 Plan IR JSON 存文件，`orx plan submit --file plan.json`；
校验返回 errors 就按错误修复后重新 submit。

提交前后 Controller 按 references/plan-review.md 审核清单过一遍（小切片
护栏、preread、双门禁、线性依赖链、验收原文逐字）。不通过：把审核意见作为
反馈重跑规划（最多 2 次）；任务图本身错了 `orx replan`；仍不行自己改写
Plan IR 并提交、在轮报告注明"Controller 改写"。`orx replan` 只用于改图，
绝不用于修复回环。单切片小 goal：主计划即轮计划来源，跳过逐轮 planner 细化。

## 5. 每轮协议（对每个切片 task 循环）

一个 task = 一轮（N 只是报告文件编号）：

1. **Select**：`orx status --json` 确认下一个 runnable task；项目规划文档
   对应条目改 In Progress。
2. **Veto**：默认每轮 Build 开工前向用户确认一次；用户在 Goal 开始时明确
   授权自动推进则照办并记录（轮报告注明）。
3. **Plan**：按 references/round-plan.md 产出自包含轮计划
   `reports/pbv/round-N-plan.md`（planner 只读细化或 Controller 机械抽取）。
   轮计划路径 = 该 task preread 首项，Build 前必须落盘。
4. **Build**：`orx run` 停泊 host 任务（返回 payload：prompt/prompt_file/
   preread/isolation）→ `orx task claim T00X` → 子代理原样执行 prompt →
   `orx task complete T00X --evidence evidence.json`（evidence 格式见
   orx-controller）；失败则 `orx task fail T00X --reason "<原因>"`。prompt
   由内核组装（Goal 目标+Goal 约束+task 目标+scope.allowed+验收+preread
   "先读且只先读这些"+验证清单+重试时前次失败），主计划全文永不进入构建
   上下文。
5. **Validate**：`orx verify` 执行命令门禁并派发 agent 审查；host 驱动时
   把返回的 verifier prompt 原样交给子代理运行，裁决经
   `orx verify submit T00X --result pass|fail --entry '<exact entry>' --evidence <file>`
   交回。
6. **Gate**：双门禁全过（每条命令门禁 exit 0 且每条 agent 审查 pass）→
   task PASSED；任一失败进修复回环（见 6）。
7. **Close**：本地提交（不 push）→ 更新项目规划文档状态 → 按
   references/round-report.md 写 `reports/pbv/round-N.md`。Close 完成才
   开工下一片。
8. **Next**：回到 1；仅当 `orx status --json` 显示
   `"run": {"status": "done"}` 才是 run done，之后收尾（最终提交、handoff/
   总结、按 references/migration.md 停用旧入口）。

## 6. 修复回环与停止

- 每片预算 = 初始构建 + ≤2 次修复。agent 审查 fail：把问题清单写进 evidence
  文件，`orx verify submit T00X --result fail --entry '<exact entry>'
  --evidence <file>` → `orx task retry T00X`（failed → runnable，同一计划）
  → 重新 Build/Validate。命令门禁非 0 由内核直接判 task FAILED，无需
  Controller 交裁决。下一次 worker/verifier prompt 自动携带前次问题，不要复述。
- 预算耗尽即停：task 保持 failed，写轮报告，向用户报告；不 replan 不换图。
- 连续阻塞（auth/quota/环境）：停止循环，保留可恢复状态（恢复方法见
  references/migration.md）。
- 范围外失败（问题不在本片 diff，如既有提交遗留违规）：git blame 归属；确属
  范围外做最小修复、单独提交；Controller 直跑门禁命令复核并如实记进轮报告；
  不重跑全量 agent 审查。

## 7. 硬规则

- 构建与验证串行；一次只有一个 worker 在跑。
- 绝不 push；提交只进本地。
- 真实数据、生产流水线、历史研究产物不动。
- 空验证列表不满足 PBV：审核清单强制每片 ≥1 条项目最强命令门禁 + ≥1 条
  agent 独立验收审查。
- token 经济：轮计划自包含且短小；preread 点名（worker 先读且只先读这些）；
  Controller 不把主计划与大 diff 粘进自己上下文（引用文件路径、只收摘要）；
  一个 Goal 一个会话，收尾写 handoff 后开新会话。
- 用量以 `orx usage` 为准（时序看 `orx timeline`）；token 缺失记 unknown，
  不当零。
- host 驱动的隔离是提示词纪律（isolation=prompt_only），不是强制沙箱；只有
  codex CLI 驱动有强制隔离（planner/verifier 只读、worker workspace-write）。
