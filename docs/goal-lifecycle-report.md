# Goal 生命周期管理（G010）五阶段报告

日期：2026-10-16。范围：Goal 暂停与继续、取消与安全收尾两个主功能，配套补齐
真实执行身份与用量关联、恢复与重复提交可靠性；Analytics 保持只读展示，不承担
调度权。本报告为轮次收口：六条 Goal 验收证据、迁移与升级链、只读读取契约与
Analytics 兼容状态、全量验证与打包结果、来源标注。跨进程演练的设计与边界见
`docs/diagnostics/g010/t006-e2e-acceptance.md`；各任务诊断见
`docs/diagnostics/g010/`（diagnosis.md、t002–t006）。

## Stage 1 — 真实执行身份与用量关联（T001）

诊断 orx-analytics G002/R002 断点：真原生子代理继承控制器窗口目录，
`discover_attempt_session` 的目录谓词把 18/18 个 nonce 精确命中全部滤除
（session_ref NULL → host_session_unbound → 0 观测行；1,222 请求实存于
zcode 库）。修复：nonce 权威发现（目录事实记入候选、根会话结构性排除、
歧义不绑定）、`settling_request_rows`（未稳定不入账）、重复补录与子树
冲突分开陈述、五个闭合点携带 `usage_backfill`。脱敏真实结构复现先红 7 例
后绿；零付费准真实闭环以 R002 归档原始提示词（逐字节 fixture）串联
run/revision/task/attempt/nonce/session/usage。证据：diagnosis.md、
tests/fixtures/g010/、tests/test_session_identity.py、test_usage_autobill.py。

## Stage 2 — Goal 暂停与继续（T002）

schema v13（`goals.status` 增 `paused`，父表 copy-then-replace，FK
deferred，零回填）。幂等 pause/resume；refresh 收敛改为行决 CAS
（active→done），暂停不被隐式解除；统一启动门禁（planner/replan 路由、计划
激活、worker 派发、停泊 claim、verifier 派发、降级重路由）与执行者占位写
同一 `BEGIN IMMEDIATE` 事务；运行中执行者身份不动、事实提交全合法；resume
不启动任何东西，返回原 attempt/nonce 与恢复指引。竞态以独立连接+确定性屏障
证明（暂停 vs 停泊两序、并发 claim 单赢、并发 resume 翻转一次、跨进程身份
保持）。证据：t002-pause-resume.md、tests/test_lifecycle.py（G010 T002 节）、
tests/test_goal.py。

## Stage 3 — 取消与安全收尾 + 权限修复后的显式复验（T003/T004）

schema v14（`cancelling`）：请求→清扫→收敛三段，未启动工作经合法转换显式
取消（goal_cancel 事件），运行中执行者绝不静默关闭——真实事实（迟到交付、
裁决、`recover --termination-fact`）逐个收敛；完成与取消并发恰一个合法结果
（cancelling 行令 done CAS 失效，真实交付保留）；可管理 CLI 停止：SIGTERM→
SIGKILL 仅针对本次启动进程组、等待实际退出、停止请求/信号/退出码全记录
（真实子进程 e2e）。取消终态后一切读面可查、迟到写入拒绝、新 Goal 不扰旧史。
T004：抑制生效时记录 `suppression_baseline`（有效配置指纹）；显式
`orx task reverify` 比较当前指纹，变化才 lift（`suppression_reopen`），
重启由下次 `orx run` 正常派发；相同指纹/mtime-only/重复复验有界拒绝；复验
本身零启动零写入。证据：t003-cancel.md、t004-suppression-reverify.md、
tests/test_lifecycle.py（取消节）、tests/test_routing.py、
tests/test_host_progress_e2e.py（T004 节）。

## Stage 4 — 有身份约束的幂等 complete 与持久化原回执（T005）

schema v15（`completion_receipts`，attempt 唯一 write-once，追加式零回填）。
交付身份 = 规范化内容摘要；归属判断先于回执（retry/recover 替换、revision
 supersede、取消关闭皆拒绝迟到交付——迟到结果不覆盖新尝试）；closed+回执+
任务未取消才纯读重放原回执（不重跑门禁、零新增执行者/证据/事件/用量行、不
重写会话归属）；同身份冲突内容/会话/模型按名拒绝；受理原子化（单事务，提交
前故障全量回滚、提交后故障=响应丢失由重传恢复）；并发重复在事务内复查恰受理
一次。证据：t005-idempotent-complete.md、tests/test_lifecycle.py（T005 节）、
tests/test_state.py（v15 迁移）。

## Stage 5 — 跨进程端到端验收、兼容验证与操作文档（T006，本任务）

- 端到端演练（真 CLI 子进程 × 临时项目 × 确定性宿主会话库 × 本地替身，
  零付费）：完成分支与取消分支各一例全程走通，端到端矩阵含权限抑制后的修复
  接续、旧 attempt 迟到、已受理交付重传、取消时仍存活的执行者、无法关联
  用量的具名原因（tests/test_host_progress_e2e.py G010 T006 节三例；设计
  见 t006-e2e-acceptance.md）。
- 只读读取契约：第十一修正案新增 `goal_lifecycle` / `goal_lifecycle_events`
  查询（新状态/事件/时间），读取测试拒绝写入（tests/test_observability_
  contract.py 12 passed）；独立 Analytics 兼容状态如实记录（见下）。
- 操作文档：docs/goal-lifecycle.md "Operation cookbook"（暂停、继续、取消、
  确认终止、复验、重传六例，tests/test_goal.py 钉住）；README 生命周期不变
  量补取消句与 v13–v15 观测说明；orx-controller skill 增生命周期控制节；
  orx-worker 定义与 orx-agent skill 增重传安全性说明。
- 实施计划收口：G010 全部计划段落已从 IMPLEMENTATION_PLAN.md 移除（整文件
  删除被范围外 tests/test_model_docs.py 的 G004/G006/G007/G008 历史钉住
  阻断，如实保留其余 Goal 归档）。

## 六条 Goal 验收证据（原文与出处）

| # | 验收原文 | 证据 |
|---|---|---|
| 1 | 完整场景走通：启动 Goal → 执行部分任务 → 暂停 → 关闭宿主会话 → 新会话继续 → 遇到阻塞后修复并接续 → 完成或明确取消。 | `test_g010_e2e_completion_branch_full_lifecycle_cross_process`（完成分支全程）与 `test_g010_e2e_cancel_branch_live_executor_safe_wrapup`（明确取消分支）；单元层 `test_pause_gates_every_dispatch_entry_point_and_running_facts_still_land`、`test_cancel_unstarted_work_is_explicit_and_running_waits_for_real_facts` |
| 2 | 暂停后没有新派发。 | e2e 暂停后 `orx run` exit 1（error 含 paused）、attempt 数不变（facts 快照）；单元层启动门禁矩阵（plan/plan submit/run/claim/verify dispatch/降级重路由全部 GoalPausedError） |
| 3 | 继续时没有第二个执行者。 | e2e 恢复后 claim 返回同一 attempt id，attempts 总数保持 2；`test_parked_work_survives_pause_and_resume_reuses_the_same_attempt`、`test_new_process_resume_preserves_all_identity_without_restarting` |
| 4 | 取消后历史记录完整。 | e2e 取消终态后 status/timeline --run/usage/goal show 全可查（goal_cancel 事件、attempt 结果、heartbeat 历史俱在）；`test_cancelled_goal_history_stays_queryable_and_survives_a_new_goal` |
| 5 | 迟到结果不会覆盖新尝试。 | e2e 旧 attempt 迟到 complete exit 1（stale）且数据库零变化；`test_receipt_never_bypasses_current_ownership_checks`（retry/recover/supersede 三态）、`test_stale_cli_attempt_cannot_deliver_after_resource_reroute` |
| 6 | 用量可以追溯，无法关联时明确说明原因。 | e2e 两轮 zcode_db 观测行精确（100/10、1000/50），无会话/会话不在库分别得到 `host_session_unbound` 与 `zcode_unreadable` 具名原因；T001 修复 + 第七修正案词表、`test_usage_attribution_separates_controller_worker_verifier_probe` |

## 迁移与升级链

- v13/v14：`goals.status` CHECK 两次加宽，父表 backup-replace（FK deferred），
  零回填、失败留原库；v15：`completion_receipts` 追加式（attempt 唯一
  write-once），升级库无回执且绝不从旧受理推导。
- 升级链（真实 v0.3.0 代码生成的 schema v8 fixture → 当前版本）：
  `uv run python scripts/check_release.py` 逐行核对既有表列、追加表为空、
  旧 v0.3.0 读入口拒绝升级库且字节不变、备份可恢复可回读（§6 全链）；
  `tests/test_state.py` / `tests/test_m12_acceptance.py` 各迁移用例；
  本仓 dogfood 库 `.orx/state.db`（git 忽略）已随新代码完成 v14→v15 追加式
  迁移（T005 披露）。

## 只读读取契约与独立 Analytics 兼容状态

- 契约（docs/observability-contract.md，v8–v15 白名单）：第十一修正案新增
  `goal_lifecycle`（逐 Goal 状态/时间/取消事件数/未确认执行数，状态读
  `goals.status` 原文，绝不从 runs.status 推导）与 `goal_lifecycle_events`
  （goal_cancel*/suppression_* 全族事件带时间与 reason 原文）。全部契约查询
  在 `mode=ro + query_only` 连接上运行且写入被拒绝
  （`test_documented_queries_do_not_modify_the_database`、
  `test_goal_lifecycle_reads_reject_writes_like_every_contract_query`）。
- 独立 orx-analytics 兼容状态（2026-10-16 只读检视，commit 3f1b66c）：
  `SUPPORTED_ORX_SCHEMA = {"8"…"12"}`（src/orxanalytics/probe.py）——
  v13/v14/v15 库尚不被接受，其快照以警告隔离不崩溃。必要适配：白名单补
  13/14/15；paused/cancelling 按 UNFINISHED 处理（占用唯一未完成 Goal 槽）；
  Goal 状态读 goals.status；回执仅作重放面（attempt_id 唯一计数）。契约
  明确不授予其调度权（不能暂停/继续/取消/派发/复验）与迁移权（不得
  Store.open、不得写、不得 checkpoint）。

## 全量验证与打包（零付费声明）

- 规定检查集（tests/test_goal.py、test_lifecycle.py、test_host_progress_
  e2e.py、test_host_recovery_facts.py、test_observability_contract.py）：
  107 passed。
- 全量 `.venv/bin/python -m pytest -q`：1141 passed（T005 基线 1134 + 本任务
  新增 7：跨进程 e2e 三例、只读契约三例、操作手册钉一例），零失败，本轮全部
  测试零付费模型调用（CLI 替身、宿主停泊与确定性伪库），无新增 skip。
- 干净安装包验证与升级链 `uv run python scripts/check_release.py`：exit 0、
  problems=[]（wheel 构建/干净 venv 安装/资源与 preset/技能装更/独立 CLI
  闭环/v0.3.0 fixture 升级链与再生成比对 matches_regeneration=true，全部对
  构建产物而非源码树）。
- 用户文件保护：`docs/m1.2-report.md` 内容与 T001 基线摘要一致
  （user-file-baseline.json 顶层 sha256 = T001 postscript 记录的处置摘要
  5289e6e7…，逐次检查通过），本轮一切提交不含该文件（未暂存、未提交）。

## 命令记录

```text
$ .venv/bin/python -m pytest -q tests/test_goal.py tests/test_lifecycle.py \
    tests/test_host_progress_e2e.py tests/test_host_recovery_facts.py \
    tests/test_observability_contract.py
107 passed in 28.11s

$ .venv/bin/python -m pytest -q
1141 passed in 161.94s (0:02:41)

$ uv run python scripts/check_release.py
exit 0; report "problems": []; fixture_provenance "matches_regeneration": true

logs: .orx/runs/R010/check/T006/{01-0006,02-0007,03-0008}-command.log
```

## 来源标注（真实来源 / 准真实替身 / 未验证边界）

- **真实来源**：全部状态迁移经真实 CLI 子进程与真实 SQLite 状态机；nonce
  发现与用量子树聚合为真实实现（数字来自替身库）；交付门禁每次验收真实重跑
  命令；取消收尾/收敛/CAS 真实；抑制指纹比较读真实配置文件与真实二进制探测；
  T003 的可管理 CLI 停止为真实 /bin/sh 子进程 + 真实 SIGTERM + 真实退出码。
- **准真实替身**：宿主会话数据库（ORX_ZCODE_DB，结构真实、token 数固定）；
  cursor `agent` 结果信封替身；m12 dogfood 快照读取；tests/fixtures/g010
  的逐字节归档提示词与真实 token 数（单测层）。
- **未验证边界**：本轮零付费模型调用，未发起真实付费子代理贯穿暂停/取消
  （平台禁止孙代；演练中 host 工作停泊后由测试进程执行）；独立
  orx-analytics 应用未对 v15 库实跑（仅只读检视其支持声明）；README 的
  观测白名单句仍为打包期表述并被 tests/test_delivery_gate_e2e.py 逐字钉住
  （不在本轮范围）；时区/时钟仅经 ORX 自有时钟接缝冻结验证。
