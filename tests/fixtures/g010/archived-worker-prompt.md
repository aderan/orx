ASSIGNMENT IDENTITY (machine anchor — do not remove or alter this line):
ORX_ASSIGNMENT=orx-assignment:520449c9-32b3-4f60-8cb7-9589bb29e539

You are an ORX worker for Goal G002: 按 2026-10-16 设计方案（docs/menubar-app-design.md）交付 ORX Analytics 首版：常驻 macOS 菜单栏的本地任务观察工具——帮助先发现正在执行和需要关注的任务，再查清每次执行的 token 消耗、重试和验证开销；交付可安装的 ORX Analytics.app（本仓 apps/macos 复用 Python 分析核心；ORX 主仓仅新增用户级项目索引及其契约测试）。

Your assignment is ONE task. Do only this task and stay inside its scope.

Task T002: 实现任务/Run 级用量归属与覆盖率的只读读取模型（设计 §5，冻结契约 docs/metrics.md 不被静默改写）：Task 消耗=所有可明确归属 worker attempts（含失败与重试）之和，verifier 消耗能明确关联的计入任务并单列；planner 归 Run 规划开销（revision 为 NULL 仍归属正确）；controller 单列；无法归属入“未归属”绝不按任务数或时间摊分；规范观测按 native_cli>zcode_db>host_report>output_estimate 每 attempt 取一，不同来源不相加；父/子会话不重复计数，重叠会话子树检测归属冲突；Run 汇总覆盖全部 revision（重规划不丢旧版本消耗）；每个汇总携带 observed/attempts 覆盖率、字段缺失计数、exact/estimated/unknown 分布、数据截至时间；完全未知为 None 不补零，仅来源明确记录为零才是 0；“标准化处理量”若实现须为单独有版本的派生指标（缓存包含于 input→input+output；缓存分离→input+cached+output；语义未知→None 或明确部分和），并同步补 docs/metrics.md 契约与回归测试；原生会话预览为独立口径，与 ORX 已记录用量结构上不可能相加。

Goal constraints (binding for this work):
  - 源库只读：遵守 ORX 只读契约（ORX 仓 docs/observability-contract.md：版本白名单、命名列查询、WAL 感知、只读连接），不调用可能触发迁移的 ORX Store 或命令；未知 schema 项目隔离标注不崩溃。
  - docs/metrics.md 指标冻结契约不得被界面需求静默改写；新增派生口径必须补契约文档与回归测试；ORX 已记录用量与原生会话预览两个口径独立展示、不相加、不制造混合总数；完全未知显示“—”，只有来源明确记录为零才显示 0。
  - 仓库分工沿用 ADR-0001：应用与引擎放本仓（apps/macos 等）复用 Python 分析核心，Python 运行时保持零第三方依赖（stdlib only）；ORX 主仓只新增用户级项目索引及其契约测试，索引更新失败不得阻止 ORX 执行。
  - Swift 层不直接写业务 SQL，不另实现一套 token 算法，一律经引擎查询接口取数。
  - 通知默认关闭，用户开启后再申请系统权限；相同异常去重、同一轮合并；检测旧版独立 watcher 提供明确迁移流程，不按 PID 盲目终止进程。
  - 退出或卸载 App 不删除 ORX 项目数据；清理 analytics 缓存必须是单独、明确的操作。
  - 遵循本仓 CONTRIBUTING：IMPLEMENTATION_PLAN.md 逐阶段更新、全部完成后移除；行为改动先补失败用例再最小实现；每阶段验证并提交；同一问题最多三次失败尝试后记录证据重新评估；测试用合成来源，不读真实 agent 会话、不调模型、不弹真实系统通知。

Goal context (background; binding only where it repeats a constraint):
gate 语义裁定（控制器 2026-10-16）：沿用设计稿版本白名单 + 数据健康维度呈现“版本不支持”，不改为能力探测。

Scope (write only inside these project-relative paths): src/orxanalytics/, tests/, docs/metrics.md
Read these files first — before any exploration, and only these plus the
files you yourself create or modify (project-relative):
  - docs/menubar-app-design.md
  - CONTRIBUTING.md
  - docs/metrics.md
Acceptance (your work is verified against these, verbatim):
  - '重试（同任务多 attempt）、多 verifier、跨 revision 重做归属正确；planner 无 revision 仍归属 Run'
  - '缺失不补零：完全未知为 None，仅来源明确记录为零才是 0；汇总附覆盖率与 exact/estimated/unknown 分布'
  - 'ORX 已记录用量与原生会话预览两口径独立、结构上不可能相加；缓存包含/缓存分离/字段缺失三情形派生指标正确或保持未知'

How your work will be checked (the prescribed checks, with ORX's
static pre-flight status — assembled at assignment time, nothing
was executed; the START GATE re-verifies it for real in your
environment):
  - [preflight:ok] command 'cd /Users/flb/Sources/Tools/orx-analytics && .venv/bin/python -m pytest -o addopts= -q tests/test_usage_readmodel.py' -> denylist allowed; first token 'cd' is a shell builtin
  - [preflight:ok] command 'cd /Users/flb/Sources/Tools/orx-analytics && .venv/bin/python -m pytest -o addopts= -q' -> denylist allowed; first token 'cd' is a shell builtin
Pre-flight summary: 2 command check(s): 2 ok, 0 blocked; 0 agent check(s) not preflighted.

Delivery contract (binding, in order):
Delivery contract (binding, in order): the four-clause delivery
contract in your agent definition (orx-worker: 1. START GATE — prove the
prescribed checks can run BEFORE implementing; 2. BLOCKED EXIT — when they
cannot, leave the workspace unchanged and exit non-zero with the blocker
in your output; 3. CHECK-FIX LOOP; 4. DELIVERY GATE) applies to this task
VERBATIM. Task-bound facts:
- check budget: 3 check round(s) on this attempt; `orx task check T002` reports rounds used vs budget
- stop and submit the structured failed/blocked delivery when the budget is exhausted and checks are still red
- completion: `orx task complete T002 --evidence <file>` only when every command check is green; a green self-check is necessary, not sufficient — the independent verifier still judges the work
Rules:
- do not modify the Goal text or other tasks' scope
- if you are blocked, leave the workspace unchanged and exit non-zero with the
  blocker in your output; do not widen scope
- exit 0 only when the task is done AND the delivery gate is green
