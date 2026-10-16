ASSIGNMENT IDENTITY (machine anchor — do not remove or alter this line):
ORX_ASSIGNMENT=orx-assignment:08a42551-b92a-4888-be05-f93c6b2ddfcb

You are an ORX verifier for Goal G002: 按 2026-10-16 设计方案（docs/menubar-app-design.md）交付 ORX Analytics 首版：常驻 macOS 菜单栏的本地任务观察工具——帮助先发现正在执行和需要关注的任务，再查清每次执行的 token 消耗、重试和验证开销；交付可安装的 ORX Analytics.app（本仓 apps/macos 复用 Python 分析核心；ORX 主仓仅新增用户级项目索引及其契约测试）。

Verify ONE acceptance check for task T009 (实现快速面板与执行/任务详情（设计 §3/§4/§5 展示层）：快速面板三区（需要关注/进行中/最近完成），同一 Run 只出现一次、异常优先排序，进度显示“已完成任务数/当前计划任务数”，当前计划进度与历史计划消耗分开表达，点击卡片打开执行详情；详情窗口四区——执行概况（含数据更新时间）、任务列表（状态/执行次数/已观测 token/异常提示）、消耗分析（worker/verifier/planner/controller/未归属分账、模型分布、缺失说明）、尝试与证据（每次 Attempt 结果/用量/会话关联/失败原因/来源）；两口径（ORX 已记录用量/原生会话预览）并列不相加；完全未知显示“—”仅明确记录为零显示 0；Revision 默认收起可展开；执行状态/关注事项/数据健康三维分立；关闭窗口驻留，仅“退出”结束。).

Check to verify (exact instruction): 启动 apps/macos 构建产物，截取菜单栏快速面板与执行详情窗口，核对设计 §3 结构（三区/卡片字段/最近完成入口）与 §4 状态三维分立、未知值“—”呈现，逐项列出符合与不符
Required capabilities: vision

Goal constraints (the work must respect these):
  - 源库只读：遵守 ORX 只读契约（ORX 仓 docs/observability-contract.md：版本白名单、命名列查询、WAL 感知、只读连接），不调用可能触发迁移的 ORX Store 或命令；未知 schema 项目隔离标注不崩溃。
  - docs/metrics.md 指标冻结契约不得被界面需求静默改写；新增派生口径必须补契约文档与回归测试；ORX 已记录用量与原生会话预览两个口径独立展示、不相加、不制造混合总数；完全未知显示“—”，只有来源明确记录为零才显示 0。
  - 仓库分工沿用 ADR-0001：应用与引擎放本仓（apps/macos 等）复用 Python 分析核心，Python 运行时保持零第三方依赖（stdlib only）；ORX 主仓只新增用户级项目索引及其契约测试，索引更新失败不得阻止 ORX 执行。
  - Swift 层不直接写业务 SQL，不另实现一套 token 算法，一律经引擎查询接口取数。
  - 通知默认关闭，用户开启后再申请系统权限；相同异常去重、同一轮合并；检测旧版独立 watcher 提供明确迁移流程，不按 PID 盲目终止进程。
  - 退出或卸载 App 不删除 ORX 项目数据；清理 analytics 缓存必须是单独、明确的操作。
  - 遵循本仓 CONTRIBUTING：IMPLEMENTATION_PLAN.md 逐阶段更新、全部完成后移除；行为改动先补失败用例再最小实现；每阶段验证并提交；同一问题最多三次失败尝试后记录证据重新评估；测试用合成来源，不读真实 agent 会话、不调模型、不弹真实系统通知。
Task acceptance criteria (judge against these, verbatim):
  - '从进行中执行点进任务，能看到消耗分解与缺失说明'
  - '刷新不阻塞界面；关闭窗口继续驻留'
  - '状态三维度分立呈现；未知值显示“—”而非 0；两口径不相加'
Deterministic checks already run for this task (do not re-run them):
  - cd /Users/flb/Sources/Tools/orx-analytics/apps/macos && swift build 2>&1 | tail -3 -> exit 0 (passed)
  - cd /Users/flb/Sources/Tools/orx-analytics/apps/macos && swift test 2>&1 | tail -3 -> exit 0 (passed)

Execution evidence for this task:
  - [completion] /Users/flb/Sources/Tools/orx-analytics/.orx/runs/R002/evidence-T009.json

Look at the project workspace at the current working directory. Then print
EXACTLY two final lines, nothing after them:

ORX_REASON=<one short line: what you checked, and for a fail what is missing>
ORX_VERDICT=pass
or
ORX_VERDICT=fail

A fail WITHOUT a reason line is an invalid verdict: the Controller cannot
act on an unexplained failure. Nothing else counts as a verdict. Exit 0
after printing both lines.
