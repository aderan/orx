# ORX 长期执行 Goal 与交接计划

日期：2026-10-07。状态：**计划已准备，实施未开始**。本文件不创建 live Goal、不启动真实模型、不修改生产权限。代码调查基准为 `ab6a240cb856b0856b538d8890b5d5c1fda1c55e`；接手后重新核验差异。

## 执行目标

系统修复 ORX 在 CLI 交付、权限诊断、退避切换、宿主故障恢复和用量观测上的缺陷，建立大型任务前可执行的小任务验证链，并以实测约束上下文与重复尝试成本。

这是功能修复与机制验证的持续 Goal，不以“补一份报告”结束，也不要求常驻后台服务。总耗时和总 token 暂不作无依据承诺；先获得可复用基线，再设预算和恢复点。

## 权威文件与接手顺序

- `docs/diagnostics/recovery-hardening-2026-10-07/goal.json`：Goal草案，8条验收逐字保留；其字段供现有 `orx goal new` 使用，并非新增JSON导入接口。
- `docs/diagnostics/recovery-hardening-2026-10-07/plan.template.json`：符合现有 PlanIR 的权威模板；额外 `handoff_details` 是交接说明，ORX不会持久化它作为核心契约，因此正文也需保留。
- `docs/cursor-system-analysis-2026-10-07.md`：纠正后的事故与用量审计。
- `docs/recovery-validation-plan-2026-10-07.md`：配置/故障注入/真实微任务验证矩阵。
- `docs/diagnostics/cursor-2026-10-07/`：派生证据、只读重算脚本、两个红回放。

1. 读取 AGENTS.md/用户指令、以上文件及 orx-controller 技能；先看 git status 和当前 ORX状态。接手环境没有原始DB时，以已打包派生证据与可运行回放起步，不伪称重新查过原始会话。
2. 确认没有冲突的活跃 Goal；执行时才通过 ORX API/CLI创建该 Goal，不改 `.orx/state.db`。若已有不相关活跃 Goal，不擅自替换。既有G007/G008归档历史不重写。
3. 使用真实 Goal ID替换模板的 `__GOAL_ID__`，以有效profile capabilities重新执行 `parse_ir`/`validate_ir`，再走现有 `plan submit`。不要直接提交占位ID，也不要把“已计划”说成“已调度”。
4. 沿下面串行任务实施；文件scope不能容纳必要变动时先按正式replan扩范围。目标验收不悄悄降级。操作在工作区内可逆推进、本地提交；每个阶段给下一阶段留下证据索引和明确状态。

## 事实基线与优先级

**P0**：CLI exit 0 + worker blocked 被记completed；控制器门禁绿时甚至passed，已复现。**P0**：CLI资源失败切host无条件走CLI adapter，报 `no adapter for harness zcode`，已复现。

**P1**：Cursor内部命令被拒绝，重试却反复提示代码失败；当前profile未开force，当前全局审批allowlist只允许ls，与记录相符。`--trust`、sandbox disabled和`--force/--yolo`并不等价；缺历史配置快照，不能单凭当前配置断定当时每项设置。

**P1**：a130并非出生即死；已claim、运行约一小时并修改代码，遇GLM1308后直到控制器恢复才关闭。误写无工作区改动、reset自行加Z、失联时恢复缺执行者均需治理。

**P2**：会话覆盖2 Goals/10 tasks/26 attempts，归一化累计处理量约155,836,133，缓存口径因provider不同，不能冒充账单。ORX仅5/26 attempts有usage observation；controller输入与host worker首包偏大是优化方向，不等于已证明继承父历史。没有同类预算，不能给“超预期几倍”的结论。

已有100/88例相关测试绿与两个真实回放红并存。正式全量基线在接手后建立。本次两个回放不是付费调用，当前exit 1是预期缺陷信号。

## 执行边界与默认策略

- 范围：仓库代码、测试、技能/agent定义、说明文档，以及隔离临时项目中的微任务。保留单一writer、nonce、独立验收和provider身份；禁止静默换模型掩盖不可解析配置。
- 不直接改真实DB，不修改用户全局Cursor权限，不读取输出凭据，不替用户修订外部记忆；需要的事故勘误先保存在仓库。
- 不发布版本、不push、不建付费账号、不真实耗尽配额、不新增常驻daemon。真实微任务采用已有认证和路由档位；每profile首轮一次，发现权限阻塞就收口。
- 同会话修复只在harness实际支持时采用；新attempt必须有差量交接、明确原因和总预算。三次相同失败后停止该路径并记录替代方案，独立工作可继续；不能用换agent绕过同一阻塞。
- 正式完成需要真实CLI与真实host各一条闭环。接手平台缺ZCode入口时可完成工程修复与注入回归，但真实host验收保持未验证，整体Goal不得宣称done。
- 本计划的数值时间/TTL建议用于校准，不作为已经被用户确认的硬预算；需新增大规模付费对照、全局权限变更或后台监控时先形成具体结果再确认范围。

## Goal验收

1. CLI 的进程成功、结构化交付和验收门禁相互区分；blocked、failed、缺失或非法交付均不能因 exit 0 或控制器绿测试被判 passed。
2. CLI、host、external 的路由切换遵循各自执行契约；资源失败后的 CLI→host 切换产生有效宿主指派，不再报 no adapter for harness zcode。
3. 权限、认证、额度、限流、代码检查失败分别诊断；相同条件下的同一 blocker 不反复启动，退避和修复预算有界，人工 override 不被自动清除。
4. 宿主恢复区分真实故障、观察时间和关闭时间；保留 claim、改动及原始时区信息，不由心跳静默认定死亡，不允许两个 writer 或旧 nonce 交付覆盖新 attempt。
5. 配置预检、确定性故障注入和真实微任务形成可复用验证入口；至少一条真实 CLI worker 和一条真实 host worker 完成读写、内部检查、正式交付及独立门禁闭环，其余启用 profile 均有明确验证状态和证据。
6. 用量区分 provider 原始字段、归一化处理量、缓存、未缓存输入、输出和缺失数据；同一 token 不重复累计，controller、worker、verifier、probe 分账，不能把处理量冒充账单。
7. 用可重复的代表性任务对照证明上下文成本治理效果；保留正确率、验收和恢复能力，公布原始指标及限制，并设置可配置的软提示与有界预算行为。
8. 新增回归、既有全量测试和受影响的打包或迁移检查通过；交付完整实现、可重取证据、修正后的事故结论、独立验收与逐阶段本地提交。

## Stage 1: 先修复交付与driver分发

**Goal**: 完成 T001–T003，下阶段消费经过独立检查的结果。

**Success Criteria**: 以下任务逐项验收通过，遗留阻塞有明确状态；阶段本地提交包含原因与验证。

**Tests**: 以下任务 verification；阶段结束复核受影响既有测试，不重复无关重测。

**Status**: Not Started

### T001 · 冻结事故基线，将两个已知红回放及交付边界转成常规行为回归。

至少读 delivery_gate、host_progress、quota/routing 三类相似实现。固定既有状态机与正式 delivery 契约，不先发明另一套状态。诊断脚本当前 exit 1 是预期缺陷，不是实施失败。

**依赖**：接手核验。 **执行能力**：coding。

**先读**：`docs/cursor-system-analysis-2026-10-07.md`, `docs/recovery-validation-plan-2026-10-07.md`, `docs/diagnostics/cursor-2026-10-07/replay.py`, `docs/diagnostics/cursor-2026-10-07/replay_cli_to_host.py`, `tests/conftest.py`。

**允许改动**：`tests`, `docs/diagnostics`, `docs/recovery-hardening-report.md`。

**验收**：

- 两个真实调度回放都有可定位的 red 基线；缺失、畸形、互相矛盾的交付有行为测试。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_delivery_gate_e2e.py tests/test_adapters.py tests/test_health.py tests/test_routing.py`
- `agent: 确认新增回归修复前因目标缺陷失败，而非 fixture 或环境失败；原始证据没有被改写。`

### T002 · 接通 CLI 结构化 delivery 与正式门禁，修正健康学习语义。

优先扩展既有 Capture/交付入口，运输状态与任务状态分开；不要仅靠英文关键词或要求模型控制 CLI 退出码。a132 仅自然语言 blocked 要被诊断为非正式交付/阻塞证据，不能伪造结构化成功。通过明确机器契约逐步消除歧义；按真实输出形状补 Cursor/Codex/shell fixtures。健康仅更新得到证明的能力维度，代码红不等于账号不可用。

**依赖**：T001。 **执行能力**：coding。

**先读**：`src/orx/adapters/base.py`, `src/orx/adapters/cursor.py`, `src/orx/adapters/codex.py`, `src/orx/dispatch.py`, `src/orx/health.py`, `docs/host-progress-contract.md`。

**允许改动**：`src/orx/adapters`, `src/orx/dispatch.py`, `src/orx/runtime.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/health.py`, `tests`, `docs`, `skills/orx-agent`, `agents`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- CLI 的进程成功、结构化交付和验收门禁相互区分；blocked、failed、缺失或非法交付均不能因 exit 0 或控制器绿测试被判 passed。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_delivery_gate_e2e.py tests/test_verify_check.py tests/test_adapters.py tests/test_health.py`
- `agent: 用 exit 0+blocked 在红/绿门禁两种情况下核验均不 passed；审查未知交付、override 和旧 CLI 兼容边界。`

### T003 · 让资源失败后的下一档按 driver 分发，补齐有界切换和身份绑定。

复用已有 CLI 执行、host parking、external 路径；最小公共分发入口优先，避免重写 dispatch。覆盖 CLI→CLI、CLI→host、host→CLI、host→host 及 external 分支；host 先取得正式结束事实再切换，不由 CLI 日志替代宿主协议。

**依赖**：T002。 **执行能力**：coding。

**先读**：`src/orx/dispatch.py`, `src/orx/routing.py`, `tests/test_host_progress_e2e.py`, `docs/responsibilities.md`。

**允许改动**：`src/orx/dispatch.py`, `src/orx/routing.py`, `src/orx/records.py`, `tests`, `docs`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- CLI、host、external 的路由切换遵循各自执行契约；资源失败后的 CLI→host 切换产生有效宿主指派，不再报 no adapter for harness zcode。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_routing.py tests/test_quota.py tests/test_host_progress_e2e.py tests/test_delivery_gate_e2e.py`
- `agent: 重放 CLI→host，检查 host_required、profile、nonce；external 只生成正式外部契约，不冒充 adapter。`

## Stage 2: 有界退避、修复及宿主恢复

**Goal**: 完成 T004–T006，下阶段消费经过独立检查的结果。

**Success Criteria**: 以下任务逐项验收通过，遗留阻塞有明确状态；阶段本地提交包含原因与验证。

**Tests**: 以下任务 verification；阶段结束复核受影响既有测试，不重复无关重测。

**Status**: Not Started

### T004 · 建立失败类型、同条件 blocker 抑制和有界 check-fix 机制。

先核验当前 CLI help/官方文档是否支持同会话延续，再实现；不能把新进程称为同会话。支持时保留 session/attempt 的修复轮证据；不支持时生成精简修复包并在总预算内换 attempt。权限阻塞立即收口，不能进入代码修复循环；不要盲目默认 --force。

**依赖**：T003。 **执行能力**：coding。

**先读**：`src/orx/health.py`, `src/orx/dispatch.py`, `src/orx/runtime.py`, `src/orx/config.py`, `docs/routing-strategy.md`。

**允许改动**：`src/orx/config.py`, `src/orx/dispatch.py`, `src/orx/health.py`, `src/orx/routing.py`, `src/orx/runtime.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/adapters`, `tests`, `docs`, `skills`, `agents`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 重复 blocker 和 check-fix 行为按任务、profile、环境指纹及实际能力约束，有有限启动次数与可解释停止原因。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_health.py tests/test_routing.py tests/test_runtime.py tests/test_delivery_gate_e2e.py`
- `agent: 同一权限阻塞不重复启动；代码红仍有真实可用的修复机会；不支持 resume 的 harness 有明确有界退路。`

### T005 · 校准额度故障域、退避时间与未知状态的路由规则。

保留提供商原始 reset 字符串与时区来源；未知时区不得自行补 Z。共享账号额度的模型不能被当成独立容量。探针成功与额度足够区分，override 优先。虚拟时钟覆盖增长、封顶、过期及未知 reset，无真实耗尽实验。

**依赖**：T004。 **执行能力**：coding。

**先读**：`src/orx/quota.py`, `src/orx/health.py`, `src/orx/routing.py`, `docs/quota-preflight-research.md`。

**允许改动**：`src/orx/quota.py`, `src/orx/health.py`, `src/orx/routing.py`, `src/orx/config.py`, `src/orx/dispatch.py`, `src/orx/records.py`, `src/orx/state.py`, `tests`, `docs`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 权限、认证、额度、限流、代码检查失败分别诊断；相同条件下的同一 blocker 不反复启动，退避和修复预算有界，人工 override 不被自动清除。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_quota.py tests/test_health.py tests/test_routing.py`
- `agent: 注入中文 GLM1308、无时区 reset、共享账号多模型、全部不可用及 reset 前后，检查无无限循环和虚假 available。`

### T006 · 补齐宿主故障事实、可信观察与安全恢复入口。

观察/判定/恢复分开。只读终止事实绑定 session/attempt/nonce、来源和时间，不把 silence 当 death。控制器与 worker 同配额故障时，提供独立可调用的状态诊断及显式恢复流程；没有实际恢复执行者就保持可解释停留，不承诺自动继续。历史误报以补充纠正证据保留原记录，不直接 SQL 改库。无需常驻 daemon。

**依赖**：T005。 **执行能力**：coding。

**先读**：`docs/host-progress-contract.md`, `src/orx/zcode_sessions.py`, `src/orx/dispatch.py`, `src/orx/records.py`。

**允许改动**：`src/orx/zcode_sessions.py`, `src/orx/dispatch.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/cli.py`, `tests`, `docs`, `skills/orx-controller`, `agents`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 宿主恢复区分真实故障、观察时间和关闭时间；保留 claim、改动及原始时区信息，不由心跳静默认定死亡，不允许两个 writer 或旧 nonce 交付覆盖新 attempt。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_host_progress_e2e.py tests/test_health.py tests/test_quota.py`
- `agent: 回放已 claim/已修改后 quota 失败、控制器同故障、迟到回传和失联仍在写；核验没有两个 writer。`

## Stage 3: 建立提前验证能力

**Goal**: 完成 T007–T009，下阶段消费经过独立检查的结果。

**Success Criteria**: 以下任务逐项验收通过，遗留阻塞有明确状态；阶段本地提交包含原因与验证。

**Tests**: 以下任务 verification；阶段结束复核受影响既有测试，不重复无关重测。

**Status**: Not Started

### T007 · 增加执行能力预检与验证结果的配置指纹。

读取当前真实配置再解释。输出避免秘密、完整全局配置与账号标识泄漏；只存必要指纹和来源。若需更改 force，先在隔离项目显式选择并留证据，不修改用户全局权限。

**依赖**：T006。 **执行能力**：coding。

**先读**：`src/orx/probes.py`, `src/orx/doctor.py`, `src/orx/adapters/cursor.py`, `src/orx/config.py`。

**允许改动**：`src/orx/probes.py`, `src/orx/doctor.py`, `src/orx/cli.py`, `src/orx/config.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/adapters`, `tests`, `docs`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 预检明确说明可证明的范围；结果绑定 CLI/profile/model/effort/权限/workspace/工具链/时间，配置变化失效，不能自动清除额度或人工 override。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_probes.py tests/test_adapters.py`
- `agent: 检查 trust、force/yolo、审批模式、sandbox 与项目 hooks 的区别；帮助输出和配置检查不能冒充内部 shell 成功。`

### T008 · 建立可重复的故障注入与组合验收矩阵。

正反例同时覆盖，禁止靠无异常返回判成功；过期额度只允许重新考虑候选，不宣称长任务容量充足。

**依赖**：T007。 **执行能力**：coding。

**先读**：`docs/recovery-validation-plan-2026-10-07.md`, `tests/conftest.py`, `tests/test_quota.py`, `tests/test_host_progress_e2e.py`。

**允许改动**：`tests`, `docs/diagnostics`, `docs/recovery-validation-plan-2026-10-07.md`, `docs/recovery-hardening-report.md`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 全部确定性矩阵有可定位用例和有限动作断言；组合路径覆盖 driver、权限、配额、override、陈旧身份与控制器同故障。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_health.py tests/test_quota.py tests/test_routing.py tests/test_probes.py tests/test_host_progress_e2e.py tests/test_delivery_gate_e2e.py`
- `agent: 对照验证计划的矩阵逐行找测试证据，确认只 mock 外部进程/提供商与时钟，没有 mock 被测 dispatch。`

### T009 · 运行隔离真实微任务，验证投产 profile 与切换接手者。

微任务使用隔离临时仓库、预置 red 测试和一行修复。优先现有 worker 两档；其余角色按职责验证，不能由 worker 成功给 verifier/planner 背书。每个 profile 首轮一次启动，60–90秒为校准目标；阻塞立即停止该 profile 的本轮，不派代理绕过审批。只使用已有认证，模型按现有路由。故障由注入触发，备用执行者是真实模型。接手平台没有 ZCode 原生入口或缺认证时记未验证，工程工作继续，但该验收不得冒充完成。此项需要原生宿主启动能力，由具备host_context的执行者组织，不能交给CLI假装完成host认证。

**依赖**：T008。 **执行能力**：host_context。

**先读**：`docs/recovery-validation-plan-2026-10-07.md`, `docs/host-progress-contract.md`, `.orx/config.toml`, `.orx/profiles.toml`。

**允许改动**：`tests`, `docs/diagnostics`, `docs/recovery-hardening-report.md`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 配置预检、确定性故障注入和真实微任务形成可复用验证入口；至少一条真实 CLI worker 和一条真实 host worker 完成读写、内部检查、正式交付及独立门禁闭环，其余启用 profile 均有明确验证状态和证据。

**验证**：

- `agent: 至少核对一条真实 CLI 和一条真实 host 的 nonce、范围内改动、agent 内部真实测试结果、正式 delivery、控制器独立门禁及用量；其余启用 profiles 有证据状态。`

## Stage 4: 观测、上下文与预算治理

**Goal**: 完成 T010–T011，下阶段消费经过独立检查的结果。

**Success Criteria**: 以下任务逐项验收通过，遗留阻塞有明确状态；阶段本地提交包含原因与验证。

**Tests**: 以下任务 verification；阶段结束复核受影响既有测试，不重复无关重测。

**Status**: Not Started

### T010 · 统一 provider 用量口径并补齐 host/controller/probe 观测。

核验同一会话、消息、turn 和请求的重复来源并去重；明确 provider input 是否已含缓存。关注当前21/26 host attempts 缺 ORX usage。持久字段变动须加增量迁移、旧记录默认 unknown、备份/复制库兼容测试。根会话/会话外 planner/CLI 不能漏算或重复归属。不得把内建观测扩展成整个外部 analytics 产品。

**依赖**：T009。 **执行能力**：coding。

**先读**：`src/orx/zcode_sessions.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/adapters/base.py`, `docs/diagnostics/cursor-2026-10-07/evidence.json`。

**允许改动**：`src/orx/adapters`, `src/orx/zcode_sessions.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/dispatch.py`, `src/orx/cli.py`, `tests`, `docs`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 用量区分 provider 原始字段、归一化处理量、缓存、未缓存输入、输出和缺失数据；同一 token 不重复累计，controller、worker、verifier、probe 分账，不能把处理量冒充账单。

**验证**：

- `.venv/bin/python -m pytest -q tests/test_adapters.py tests/test_host_progress_e2e.py`
- `agent: 在复制/合成 fixtures 重算会话树与 ORX attempts；缓存不双算、controller/probe 单列、缺失不等于零。`

### T011 · 用对照实测压缩 controller 与 worker 上下文，形成预算策略。

先对提示构建与工具 schema 做静态/捕获测量，再做有限真实对照。控制器只保留调度事实与证据索引；worker 输入以任务契约、preread、失败差量为主。不假设父会话历史一定被继承，不盲删验证工具。软预算告警、硬预算停止/恢复点均可配置，未知用量不能当0；阈值来自样本而非虚构价格或预期。解析器 O(n²) 已修复，补性能回归及诊断长挂预算，不重复当待修根因。

**依赖**：T010。 **执行能力**：coding。

**先读**：`docs/cursor-system-analysis-2026-10-07.md`, `src/orx/dispatch.py`, `skills/orx-controller/SKILL.md`, `agents/orx-worker.md`。

**允许改动**：`src/orx/config.py`, `src/orx/dispatch.py`, `src/orx/runtime.py`, `src/orx/records.py`, `src/orx/state.py`, `src/orx/cli.py`, `tests`, `docs`, `skills`, `agents`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 用可重复的代表性任务对照证明上下文成本治理效果；保留正确率、验收和恢复能力，公布原始指标及限制，并设置可配置的软提示与有界预算行为。

**验证**：

- `agent: 对相同小任务与一个代表性开发任务记录 baseline/改后上下文、请求、缓存/未缓存输入、输出、检查轮、启动数与正确率，复核原始证据。`

## Stage 5: 兼容终验与交付

**Goal**: 完成 T012–T013，下阶段消费经过独立检查的结果。

**Success Criteria**: 以下任务逐项验收通过，遗留阻塞有明确状态；阶段本地提交包含原因与验证。

**Tests**: 以下任务 verification；阶段结束复核受影响既有测试，不重复无关重测。

**Status**: Not Started

### T012 · 执行兼容、打包、迁移及全量验证，整理使用文档。

完整检查通过后才提交对应阶段；无 formatter/linter 配置时如实说明，不引入工具凑检查。打包检查已有全量用例覆盖时不无理由重复，但新增持久化/技能资源变动必须覆盖。更新事故勘误只在仓库，外部 ZCode memory 另需明确范围。注意既有全量测试依赖本地dogfood DB，并会重写docs/m1.2-report.md；保留基线，复核并还原纯测试生成的无关报告差异。跨工作树缺失数据库时补足只读测试输入或记录环境缺项，不能冒充代码回归。新活跃阶段放在历史之前，以符合当前G008文档检查边界。

**依赖**：T011。 **执行能力**：coding。

**先读**：`docs/package-acceptance.md`, `scripts/check_release.py`, `tests/test_package_acceptance.py`, `pyproject.toml`, `tests/test_m12_acceptance.py`, `tests/test_model_docs.py`。

**允许改动**：`src/orx`, `tests`, `scripts`, `docs`, `README.md`, `skills`, `agents`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 新增回归、既有全量测试和受影响的打包或迁移检查通过；交付完整实现、可重取证据、修正后的事故结论、独立验收与逐阶段本地提交。

**验证**：

- `.venv/bin/python -m pytest -q`
- `git diff --check`
- `agent: 对受影响的 wheel/资源/旧库迁移运行现有检查；确认没有版本发布、测试禁用或未经记录的跳过。`

### T013 · 依据完整证据独立终审 Goal，交付实现报告和剩余阻塞。

此任务需要全局验收与跨任务上下文，必须 host_context。交付本地 commits、改动摘要、故障恢复操作、计量口径、配置迁移说明及未验证项目；不能由CLI重复读取全库写总结。

**依赖**：T012。 **执行能力**：host_context。

**先读**：`docs/cursor-system-analysis-2026-10-07.md`, `docs/recovery-validation-plan-2026-10-07.md`, `docs/recovery-execution-goal-2026-10-07.md`, `docs/diagnostics/recovery-hardening-2026-10-07/goal.json`。

**允许改动**：`docs/recovery-hardening-report.md`, `IMPLEMENTATION_PLAN.md`。

**验收**：

- 最终报告每条验收都有状态、证据路径、命令/观测时间和限制；实际状态与报告一致，无未处理的必需工作。

**验证**：

- `agent: 逐项核对 goal.json 的8条验收、ORX真实状态、独立verdict、真实微任务与对照测量；任何缺项不得宣称 Goal done。`

## 证据、暂停点与最终交付

实施报告使用 `docs/recovery-hardening-report.md`，每个任务记录：输入commit、红信号、实现选择、绿验证、真实/模拟来源、attempt/session/nonce、配置指纹、用量来源、缺失字段、耗时区间与提交。模型过程原始内容不要反复装进controller上下文；索引与差量足够。

收到同一blocker三次、微任务权限拒绝、全部profile不可用、身份冲突、预算触发或宿主终止不明确时，停止对应派发，保留恢复点和明确需要改变的条件。真正恢复以新事实为依据，不以等了一段时间、probe一句ok或另一模型名称为依据。无恢复执行者时不能承诺自动恢复。

最终交付包括本地提交序列、8条验收证据表、配置与兼容说明、微任务结果表、事故勘误、处理量口径和对照测量、正常/blocked/恢复操作示例。全部必需项真实完成才写done；将可做但不在范围内的常驻观察服务、外部记忆维护和大规模模型能力评测列为后续建议，不能借此扩大当前Goal。

`IMPLEMENTATION_PLAN.md` 已有历史记录，接手应只更新本Goal章节。AGENTS中的“完成后移除计划”对本次临时活跃阶段适用，不能删除既有G007/G008历史或权威Goal/IR及证据交接资料。
