# G010 T006：跨进程端到端验收设计与边界（2026-10-16）

任务：以真实 CLI、临时项目、确定性宿主会话数据库及本地子进程构造零付费演练，
覆盖成功完成与明确取消两个分支；汇总验收证据、迁移与只读读取契约。本文记录
演练的构造决策、每阶段记录的口径与来源标注。测试本体：
`tests/test_host_progress_e2e.py`（G010 T006 节，三例）。

## 进程边界（不能仅重用内存对象）

场景的每一步状态迁移都是一次真实 `python -m orx ...` 子进程调用（cwd=临时
项目，ORX_PROJECT/隔离用户层/ORX_ZCODE_DB 显式注入）：子进程退出即屏障——
暂停在进程 A 提交，门禁拒绝在进程 B 观察，恢复在进程 C 发生，测试进程从不
持有任何打开的 Project 对象驱动场景。`_lifecycle_facts` 以 `mode=ro` 只读
读 SQLite，对每阶段记录 Goal/Run/revision、执行归属（attempt 的
id/role/nonce/session_ref/result）、事件、证据、用量与回执行数，并在阶段
边界逐一断言。测试进程的读取是验收取证，不是场景执行。

## 确定性宿主会话数据库（准真实替身）

`ORX_ZCODE_DB` 指向 `make_zcode_usage_db` 生成的替身库：park 落定后以只读
方式从 state.db 取出该 attempt 的真实 nonce，再写一个首 text part 携带
`ORX_ASSIGNMENT=<nonce>` 的会话（紧凑 JSON，与真实 zcode part 形状一致），
model_usage 携固定 token 数（第一轮 100/10/90，第二轮 1000/50/980）。
claim `--discover-session` 走的是**真实发现函数**，绑定的是真 nonce；
autobill 读的是真实子树聚合入口，入账的是替身库里的数字。目录谓词按
T001 修复后的语义不参与过滤（nonce 权威）。

## 两个分支

- 完成分支（`test_g010_e2e_completion_branch_full_lifecycle_cross_process`）：
  启动 → 部分任务（第一轮 claim+heartbeat+显式 fail，用量入账）→ retry 后
  旧 attempt 迟到被拒 → 暂停（run/claim 全门禁，无新派发）→ 会话关闭 →
  新会话恢复（同一 attempt/nonce，无第二执行者）→ 门禁红（阻塞）→ 修复 →
  同 attempt 接续交付 → 独立裁决 → done。重传在全新进程重放原回执且数据库
  零变化；T002（会话不在替身库）与 T003（无会话）分别得到
  `zcode_unreadable` 与 `host_session_unbound` 的具名缺失原因。
- 取消分支（`test_g010_e2e_cancel_branch_live_executor_safe_wrapup`）：
  已认领执行者在取消时仍存活——列为 unconfirmed（带 observe/recover 后续），
  停泊依赖任务被显式取消，执行者 heartbeat 仍被接受，操作者以
  `recover --termination-fact` 确认终止后收敛 cancelled；迟到写入拒绝、
  重复取消幂等、status/timeline/usage/goal show 历史完整可查。

## 抑制修复接续（权限抑制后的修复接续）

`test_g010_e2e_suppression_fix_continuation_through_the_cli`：cursor 档位
配本地 `agent` 替身（--version/--help 探测 + 从 `$E2E_AGENT_ENVELOPE` 读
结果信封），隔离 HOME 携真实形状的 cli-config.json。三轮同条件 blocked
交付后抑制生效（suppression_baseline 落库）；真实权限配置修复
（allowlist→unrestricted）后仅再跑 `orx run` 仍被抑制（指纹无消费者）；
`orx task reverify` 判定 lifted（suppression_reopen 落库）；换 passed
信封后下一次 run 正常重启并交付 passed——四轮 native_cli 用量行俱在。

## 来源标注

- 真实来源：全部 ORX 状态迁移（真 CLI 子进程 × 真 SQLite 状态机）、nonce
  发现与子树聚合、门禁重跑、取消收尾/收敛、抑制指纹比较（读真实配置文件
  与真实二进制探测）、T003 的可管理 CLI 停止（真 /bin/sh 子进程、真
  SIGTERM、真退出码等待）。
- 准真实替身：宿主会话数据库（结构真实、数字固定）、cursor `agent` 信封
  替身、m12 dogfood 快照读取。tests/fixtures/g010（T001）保留逐字节归档
  提示词与真实 token 数，供单测层引用。
- 未验证边界：本轮零付费模型调用；未发起真实付费子代理贯穿暂停/取消（平台
  禁止孙代；host 工作在演练中停泊由测试进程执行）；独立 orx-analytics 仅
  只读检视其支持声明（SUPPORTED_ORX_SCHEMA 8–12），未对 v15 库实跑该应用；
  README 的观测白名单句仍钉在打包期表述（tests/test_delivery_gate_e2e.py
  逐字钉住，不在本任务范围）。

## 文档与收口

操作手册（暂停/继续/取消/确认终止/复验/重传六例，docs/goal-lifecycle.md
"Operation cookbook"）由 tests/test_goal.py 钉住；只读契约新增
goal_lifecycle / goal_lifecycle_events 两查询与第十一修正案（含 Analytics
兼容状态与不授权声明）。五阶段报告：docs/goal-lifecycle-report.md。
G010 实施计划段落已从 IMPLEMENTATION_PLAN.md 移除（整文件删除被范围外的
tests/test_model_docs.py 历史钉住阻断——该文件同时承载 G004/G006/G007/G008
的归档记录，如实保留）。
