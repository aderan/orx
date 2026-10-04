---
name: orx-pbv
description: 可选的多轮 Plan-Build-Validate 开发流程配方（切片、自包含轮计划、双门禁、有界修复、本地提交、轮报告）。仅在用户明确调用时使用——点名 PBV 循环、Plan-Build-Validate，或明确要求按轮切片推进并逐轮提交验收；普通开发 goal 一律直接走 orx-controller（通用执行入口），不因任务大、轮次多或"要多轮开发"自动启用；不用于单轮问答、单次小改动或无需切片验证的任务。
---

# orx-pbv：多轮 PBV 开发循环（显式调用的可选配方）

## 1. 定位

通用执行入口是 orx-controller skill：目标接纳（直接目标 / 外部咨询移交的
分类）、规划、指派交付、验证、重试、重规划、恢复与升级的协议全部集中在
那里，本 skill 引用、不另写一套。本 skill 只保留 PBV 特有的流程配方：

- 切片护栏（小切片、线性依赖链、逐片独立可提交）；
- 自包含轮计划（reports/pbv/round-N-plan.md，辅助实施文件）；
- 双门禁策略（每片 ≥1 条项目最强命令门禁 + ≥1 条 agent 独立审查）；
- 有界修复预算（初始构建 + ≤2 次修复）；
- Close（本地提交、规划文档状态、轮报告）与轮编号纪律。

ORX 拥有全部执行状态（Goal/Plan/Task/Attempt/Evidence/Verification），
不新增状态存储，绝不直接写 `.orx/state.db`。执行选择（profile、subagent、
模型、梯子）遵循配置与指派契约（见 orx-controller 与
docs/routing-strategy.md），本 skill 不规定供应商或模型。

本 skill 与具体仓库无关：文中路径（reports/pbv/、docs/DEVELOPMENT_PLAN.md）
与门禁命令（`make verify`）是示例（StockMate 为样例仓库），每个仓库按 Round 0
重新发现。安装：`orx skill install orx-pbv`；更新：`orx skill update`。

## 2. Round 0：开始前检查

按序确认，任何一步失败先向用户报告再继续：

1. 目标项目已 `orx init` 且 `orx doctor` 通过（缺可选 CLI 是 warning；认证
   失败让用户先处理，不拿正式调用试错）。
2. 规划、构建、审查三类角色在配置中有可用 profile（`[plan.*]` / `[worker]`
   / `[verify]`）；缺了先补配置，不绕过路由。
3. 确认项目最强验收门禁命令：查 Makefile/scripts/CI，不要凭习惯写。
4. 确认项目任务规划文档（如 docs/DEVELOPMENT_PLAN.md）：主计划必须引用其
   条目；没有就问用户以什么为准。
5. 建留痕目录：`mkdir -p reports/pbv`。
6. 从项目 AGENTS.md / architecture.md（或等价文档）提取 ≤10 条硬约束
   （祈使句），Goal 建立时注入。

## 3. Goal 建立

goal 来自用户消息，不明确就问，不替用户猜。用户移交外部咨询/评审总结时，
按 orx-controller 的接纳分类处理：只有用户确认的建议才进 `--constraint` /
`--acceptance`；待验证假设进计划任务或验证条目，不自动当约束。约束与验收在
`orx goal new` 一次性注入——内核会把它们组装进所有 planner/worker/verifier
prompt：

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
Plan IR 并经 `orx plan submit` 提交、在轮报告注明"Controller 改写"。
`orx replan` 只用于改图，绝不用于修复回环。单切片小 goal：主计划即轮计划
来源，跳过逐轮 planner 细化。

## 5. 每轮协议（对每个切片 task 循环）

一个 task = 一轮（N 只是报告文件编号）：

1. **Select**：`orx status --json` 确认下一个 runnable task；项目规划文档
   对应条目改 In Progress。
2. **Veto**：默认每轮 Build 开工前向用户确认一次；用户在 Goal 开始时明确
   授权自动推进则照办并记录（轮报告注明）。
3. **Plan**：按 references/round-plan.md 产出自包含轮计划
   `reports/pbv/round-N-plan.md`（planner 只读细化或 Controller 机械抽取）。
   轮计划是辅助实施文件：生效任务的范围、验收与验证以 Plan IR 为准，
   与 Plan IR 冲突时以 Plan IR 为准，不在轮计划里私自扩范围（认为需要
   变化走 `orx replan`）。轮计划路径 = 该 task preread 首项，Build 前
   必须落盘。
4. **Build**：按 orx-controller 的任务契约执行：`orx run` 停泊 host 任务
   → `orx task claim T00X`（记下返回的 attempt id）→ 子代理原样执行
   prompt → `orx task complete T00X --evidence evidence.json --attempt <id>`
   （evidence 格式与 stale 拒绝规则见 orx-controller）；失败则
   `orx task fail T00X --reason "<具体原因>"`（原因进入下一次 prompt）。
   prompt 由内核组装（Goal 目标+Goal 约束+task 目标+scope.allowed+验收+
   preread"先读且只先读这些"+验证清单+重试时前次失败），主计划全文永不
   进入构建上下文。
5. **Validate**：按 orx-controller 的验证契约执行：`orx verify` 运行命令
   门禁并派发 agent 审查；host 驱动时把 verifier prompt 原样交给子代理
   运行，裁决经 `orx verify submit T00X --result pass|fail --entry '<exact
   entry>' --attempt <派发返回的 id>` 交回；fail 时必须带
   `--reason "<问题清单>"`——问题经内核进入 failure 状态并自动出现在下一
   次 worker/verifier prompt。
6. **Gate**：双门禁全过（每条命令门禁 exit 0 且每条 agent 审查 pass）→
   task PASSED；任一失败进修复回环（见 6）。
7. **Close**：本地提交（不 push）→ 更新项目规划文档状态 → 按
   references/round-report.md 写 `reports/pbv/round-N.md`。Close 是宿主
   流程纪律：内核在 task passed 时即解锁依赖、不感知 Close，"Close 完成
   才开工下一片"由本 skill（Controller）保证。
8. **Next**：回到 1；仅当 `orx status --json` 显示
   `"run": {"status": "done"}` 才是 run done，之后收尾（最终提交、handoff/
   总结、按 references/migration.md 停用旧入口）。

## 6. 修复回环与停止

- 每片预算 = 初始构建 + ≤2 次修复。agent 审查 fail：
  `orx verify submit T00X --result fail --entry '<exact entry>'
  --reason "<问题清单>"` → `orx task retry T00X`（failed → runnable，同一
  计划）→ 重新 Build/Validate。命令门禁非 0 由内核直接判 task FAILED，
  无需 Controller 交裁决。下一次 worker/verifier prompt 自动携带前次
  问题（含 --reason 的问题清单），不要复述。
- 预算耗尽即停：task 保持 failed，写轮报告，向用户报告；不用 replan 换图
  变相续预算。
- 连续阻塞（auth/quota/环境）：停止循环，保留可恢复状态（恢复方法见
  references/migration.md）。
- 范围外问题（问题不在本片 diff，如既有提交遗留违规）：不在本轮顺手修，
  不绕过本片原定验收。属于同一 Goal 的必要工作 → `orx replan` 明确增补
  任务（带范围与验收）；超出 Goal 或授权 → 停下交用户决定。轮报告如实
  记录发现与去向。

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
