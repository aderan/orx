---
name: orx-pbv
description: 可选的逐轮交付配方。用户明确要求 PBV 或逐轮验收、提交并留痕时使用。
disable-model-invocation: true
---

# orx-pbv

使用 `orx-controller` 接纳 Goal 并推进同一个 Run。PBV 增加逐轮收口约定，适合每轮都需要留下独立验收证据和可回退本地提交的工作。

1. **切片**。确定项目验收命令、约束和规划依据。用 [计划检查表](references/plan-review.md) 审核 Plan IR，确保每片范围可独立交付，依赖和验收条目明确。若约定每轮需用户确认，在派工前确认。已有自动推进授权时直接按授权执行。
2. **轮计划**。开工前把 [轮计划](references/round-plan.md) 写到本轮任务的首个 `preread` 路径。Plan IR 决定范围和验收，轮计划补充实施背景。需要改范围或依赖时通过 Controller 重规划。
3. **双门禁**。按 `orx-controller` 的通用派发和验收流程执行本片，使用 ORX 返回的指派、认领和提交命令。每片至少有一条项目命令检查和一条独立 agent 审查。只有两类检查均通过才进入收口。需要编写验收条目时，读 [条目写法](references/verification-entries.md)。
4. **修复**。每片最多初始构建加两次修复。按 ORX 返回区分产品失败和执行阻塞。产品失败按 Controller 流程修复；执行阻塞或预算用完时记录原因，保留可恢复状态。
5. **收口**。任务 `passed` 后，完成本地提交、更新适用的项目规划条目，并按 [轮报告](references/round-report.md) 记录证据。Controller 在这些动作完成后再派下一片。按 `orx status --json` 确认 Run 达到 `done`，并完成最后一轮收口。

迁移旧 PBV 入口时，读 [迁移说明](references/migration.md)。
