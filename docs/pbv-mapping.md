# PBV 替换契约（旧 pbv-loop → orx-pbv）

状态：冻结，2026-10-03。本文是 Stage 1 的替换映射基线，也是最终交付的"替换能力映射及已知限制"的底稿。改动本文需同步更新 `IMPLEMENTATION_PLAN.md` 的状态。

替换对象：`/Users/flb/Sources/Products/StockMate/.zcode/skills/pbv-loop`（项目级 skill，含 references/ 4 个模板与 `scripts/codex_plan.sh`）。
替换产物：ORX 仓库维护的 `skills/orx-pbv`（可选安装）+ ORX 内核补齐的通用指派/验证契约。

## 1. 分层归属

| 层 | 承担 | 不承担 |
| --- | --- | --- |
| ORX 内核 | 执行状态权威（Goal/Plan/Task/Attempt/Evidence/Verification）；三类指派的 prompt 组成（目标、约束、scope、验收、先读清单、前次问题、证据、检查结果）；按角色沙箱与隔离记录；host/CLI 一致的任务契约；验证双门禁执行；用量与实际模型记录；skill 安装机制 | 开发流程编排、切片护栏、修复上限、轮报告、业务约束内容 |
| 通用 skill（orx-controller / orx-agent） | 宿主循环（status→dispatch→claim→complete→verify→done）、子代理行为纪律、verdict 输出契约 | 多轮开发循环协议本身 |
| orx-pbv skill | 多轮 Plan-Build-Validate 循环协议：Round 0 检查、主计划审核清单（小切片护栏、preread 要求、双门禁要求、线性依赖链）、每轮自包含轮计划的生成与使用、约束提取注入 Goal、修复上限与停止规则、Close 步骤与轮报告、旧入口停用与恢复 | 执行状态存储（不另建状态）；通用宿主循环（引用 orx-controller） |
| 项目规则（StockMate 文档） | 业务任务定义（docs/DEVELOPMENT_PLAN.md 条目）、验收要求原文、硬约束原文（AGENTS.md / architecture.md）、确定性门禁命令（make verify） | 执行状态（由 Controller 同步进 ORX 数据库） |

## 2. 词汇映射

| 旧 PBV | ORX 承载 |
| --- | --- |
| goal（自然语言目标） | ORX Goal：`orx goal new --objective --acceptance… --constraint…`；仓库硬约束作为 Goal constraints 注入 |
| 大任务主计划（codex master plan） | ORX Plan IR（生效计划修订）：一个切片 = 一个 Task；DEVELOPMENT_PLAN 条目引用写入 objective/exploration |
| 切片 S1/S2/… | Task T001/T002/…，依赖显式且**线性链**（保证构建/验证串行，Close 前不解锁下一片） |
| 每轮 round N | 一个 Task 的生命周期（runnable→running→verifying→passed）；N 只是报告文件编号 |
| round-N-plan.md（自包含轮计划） | 项目规划文档（reports/pbv/round-N-plan.md），由 planner 只读生成或 Controller 机械抽取；作为该 Task `preread[0]` 传入构建；**主计划全文永不进入构建指派** |
| codex 规划（codex_plan.sh，read-only 沙箱） | planner 角色：主计划走 `orx plan`/`orx replan`（Plan IR，codex 适配器 `-s read-only`）；每轮细化走 planner 指派，产物是项目文件，不改计划图 |
| 构建子代理（Agent 工具 + build 模板 + CONSTRAINTS） | worker 角色：host 指派（`orx run` 停泊 → `orx task claim` → 子代理执行 kernel worker prompt）或 CLI 指派；约束经 Goal constraints 进入 worker prompt |
| 验证工作流（CreateWorkflow：make verify + Flash 验收审查） | ORX verification：plan 每片 verification = [项目最强命令门禁, `agent: 独立验收审查`]；`orx verify` 执行命令门禁并派发 agent 审查；host 驱动时 Controller 用 `orx verify submit` 交裁决 |
| 修复回环（≤2 次/轮） | `orx verify submit --result fail` → task FAILED → `orx task retry` → 重新构建；前次问题经 prior_failure/issues 进入下一次 worker 与 verifier prompt；**上限 2 次由 Controller（skill 层）执行，内核不设尝试上限** |
| 范围限定复验（__RECHECK__） | 验证条目文本自带复验语义（"复验只核上轮问题与修复触及文件"）+ kernel verifier prompt 注入 prior issues；不再需要工作流脚本与槽位替换 |
| Close（终审、提交、轮报告、文档状态） | skill 层 Controller 动作：task PASSED 后本地提交（不 push）、更新 DEVELOPMENT_PLAN 状态、写 reports/pbv/round-N.md；这些完成前不开工下一片 |
| Done | ORX run done（生效计划全部 task passed，`orx status --json` 为准）+ skill 收尾（HANDOFF/总结/停用旧入口） |
| 阻塞停止 | task FAILED + Controller 停止并写轮报告；恢复入口 = 新会话读 `orx status --json` + 轮报告 |
| CONSTRAINTS 提取（≤10 条） | Goal 开始时一次性提取进 Goal constraints（`goal new` 时注入）；进入 planner/worker/verifier 三类指派 prompt |
| Token 经济（构建代理上下文最小化） | preread 点名文件、轮计划自包含且短小、prompt 不含主计划；用量由 ORX 记录（缺失=unknown，不当零）；轮报告引用 `orx usage` |
| 模型分工（codex 规划 / GLM 构建 / Flash 验证） | profiles.toml 按角色分层 + 路由；实际模型记录在 attempt，与配置不一致时报告 |
| 每轮 CreateWorkflow 用户否决点 | 默认保留：每轮 Build 开工前 Controller 停一次向用户确认；用户在 Goal 开始时明确授权全程自动推进则记录该选择（轮报告注明），后续轮不再逐轮确认 |

## 3. 状态与场景映射

attempt/round/retry/Close/Done 的关系：

- 一个 Task = 一轮。初始构建 = attempt 1；每次修复回环 = `task retry` 后的新 attempt。
- 片内上限 = 初始执行 + 2 次修复（3 次 attempt）；耗尽即该轮判停（task FAILED，Controller 写报告停循环，不 replan 不换图）。
- 双门禁（命令门禁 exit 0 且 agent 审查 pass）全过 → task PASSED → 才允许 Close；Close 完成才开工下一片（线性依赖链同时由内核保证下一片此前不可运行）。
- run done ≠ 收尾完成；收尾（最终提交、HANDOFF、旧入口停用）由 skill 层在 done 之后执行。

场景核对清单（Stage 4 测试逐条映射）：

| 旧场景 | 新承载 | Stage 4 断言 |
| --- | --- | --- |
| 正常两片循环 | 两 Task 线性依赖，各含双门禁，host 构建 + host 审查 | 两片先后 passed，run done；第二片在第一片 Close 前不可运行 |
| 命令门禁失败 | verification command 非 0 → task FAILED（failure_reason=命令失败） | task failed、依赖未解锁、retry 后 prompt 含前次问题 |
| 审查失败 | verify submit fail → task FAILED | 同上，且 verifier 下次 prompt 含 prior issues |
| 修复成功 | retry → 重建 → 双门禁过 → passed | 第二次 attempt 的 worker/verifier prompt 含前次失败；attempt 隔离记录正确 |
| 修复耗尽 | 2 次修复后仍 fail → Controller 停止 | 内核无上限（第 3 次 retry 合法），停止是 skill 行为；状态可恢复 |
| 收尾失败（如提交冲突） | task 已 passed、Close 未完成 | 下一片依赖已解锁（内核在 pass 时解锁，不感知 Close）；skill 层要求 Close 完成前不开工下一片，Controller 报告后可重试 Close |
| 中途重读状态 | `orx status --json` 是唯一事实源 | 新会话从 status + 轮报告恢复，不依赖聊天记忆 |
| 范围外失败（既有违规） | skill 层：git blame 归属、最小修复单独提交、Controller 直跑门禁复核并如实记录 | 不重跑全量 agent 审查（skill 纪律） |
| 权限/配额/环境阻塞 | attempt failed + 错误分类（auth_required/quota_exhausted…）+ resource 状态 | Controller 停止，保留可恢复状态 |

## 4. 内核契约补齐（Stage 2 冻结的 API）

1. **PlanTask.preread**：`list[str]`（default `[]`），项目相对路径，逐条 `is_safe_scope_path` 校验；进 `PLAN_IR_SCHEMA` 与 strict schema；planner prompt 要求逐任务给出（含测试文件）。
2. **worker prompt**（host 与 CLI 同文）：Goal objective + **Goal constraints** + task objective + scope.allowed + acceptance + **preread 清单（"先读且只先读这些"）** + verification 列表 + prior_failure + 既有规则。host 停泊指派与 CLI 指派由同一函数产生。
3. **verifier prompt**：Goal objective + **constraints** + task objective + **acceptance 全列** + 该条检查指令 + **该任务已记录的命令门禁结果** + **执行证据（log/evidence 路径）** + **prior issues（task_events 中最近失败原因）** + verdict 输出契约。
4. **host/external 任务指派落盘**：`.orx/runs/<run>/assignments/<task>.md`（worker prompt 全文）；`orx run` 返回 payload 增加 prompt/prompt_file/preread/isolation。host verifier 指派同样落盘并在 `orx verify` 输出 prompt 与 prompt_file。
5. **按角色沙箱**：codex 适配器 planner/verifier launch 用 `-s read-only`，worker 保持 `-s workspace-write`；`Launch.sandbox`（"read_only"|"workspace_write"|None）声明实际隔离。
6. **隔离记录**：attempts 表新增 `isolation TEXT`（migration v5）：CLI = launch.sandbox（"read_only"/"workspace_write"）；host = `"prompt_only"`（明确不是强制沙箱，仅为提示词纪律）；external = NULL（操作者自管，ORX 不作声明）；`attempt_create(..., isolation=None)` 向后兼容。补充（Stage 2 落地时修正）：`orx verify submit` 新增 `--reason`——fail 时审查问题进入 failure 状态，并自动出现在下一次 worker 与 verifier prompt（修复回环的问题回传通道）。
7. **skill 安装**：`orx skill install [NAMES…]`——无参 = 安装默认集（orx-controller、orx-agent，行为不变）；显式名称 = 只装该 skill（名称按打包目录动态校验，未知名称报错并列出可用项）；`orx skill update` 只刷新 canonical 目录中已安装且仍打包的 skill。
8. **空验证列表**：内核契约不变（完成即过）；PBV 计划由 skill 审核清单强制要求每片 ≥1 条命令门禁 + ≥1 条 agent 审查，空列表不满足 PBV 完成条件。

## 5. 已知限制（替换时如实声明）

- 内核有效并行度 1，无 worktree/合并机制：PBV 切片靠线性依赖链串行，正好匹配；不做并行构建。
- host 驱动无强制沙箱：planner/verifier 的只读在 host 驱动下是提示词纪律（isolation=prompt_only 如实记录）；只有 codex CLI 驱动提供 `-s read-only` 强制隔离。
- 复验的"增量"语义来自验证条目文本与 prior issues 注入，内核不感知"哪些文件是本次修复触及的"（由条目文本要求修复代理报告改动文件）。
- 每项目一个 active Goal、每 Run 一版生效计划：PBV 多轮共用一版计划修订；轮内发现计划错误走 `orx replan`（与旧"Controller 改写"对应）。
- `scope.allowed` 是路径约束与提示词纪律，不是文件级强制隔离（responsibilities.md 既有限制，不变）。
- preread 路径在计划提交时冻结：Goal 间轮编号续接（旧 pbv 报告序列）必须在 Round 0 对齐进计划，否则内核 preread 会指向错误文件（G001 实测：T002-T004 的 preread 冻结为 round-2/3/4-plan.md，Controller 在构建契约行中显式纠偏到续接编号 round-10/11/12-plan.md；skill 已加编号规则防再犯）。

## 6. 验收要求（Stage 5，样本外窗口评估 case）

- StockMate 真实规划（`orx plan`，codex read-only）、构建（host 子代理）、独立验证（命令门禁 `make verify` + agent 审查）、提交与交接证据齐备；ORX 状态与轮报告一致。
- 实际模型与用量从 attempt/usage 核对；缺失记 unknown。
- 真实数据库、生产流水线、历史研究产物不被修改；本地提交不 push。
- 通过后：旧 `.zcode/skills/pbv-loop` 退出技能扫描范围（保留 Git 历史与历史报告），新入口与恢复方法写入文档。
