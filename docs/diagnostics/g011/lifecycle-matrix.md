# G011 生命周期分层矩阵（T002 契约）

状态：T002 交付的权威矩阵。ORX 侧语义依据 `docs/goal-lifecycle.md`
（G010，schema v13/v14/v15）；Analytics 侧实现位于隔离克隆
`.release-workspaces/orx-analytics`（`display.py` 的 `lifecycle_view` 是
唯一分类点，`query_model.overview` 与 `engine._count_active_runs` 都调用
它，Swift 只渲染引擎分类，不复算业务规则）。

## 1. 两个显式层，互不改写

| 层 | 数据源 | 取值（原文只读） | 展示词汇 |
|---|---|---|---|
| Goal 控制状态 | `goals.status` | active / paused / cancelling / cancelled / done（+未知值） | 进行中 / 已暂停 / 取消中 / 已取消 / 已完成（未知→未知状态） |
| Run 执行事实 | `runs.status`（辅以 task/attempt 状态与时间线） | planning / running / blocked / done（+未知值） | 规划中 / 执行中 / 受阻 / 完成（未知→未知状态） |

- `paused`/`cancelling` 是 **Goal 控制态且未结束**：占用唯一未完成
  Goal 槽；`cancelling` 在收敛前不是终态。
- **Run blocked = 受阻**（执行层：任务受阻），**Goal paused = 已暂停**
  （控制层：新派发被闸门拒绝）。两个词不同维度，永不混用。
- Analytics 全程只读：不改写 `runs.status`、`goals.status`、task/attempt
  状态；不新增 ORX 控制命令、恢复动作或 `receipt_json` 重放能力。

## 2. 分组矩阵（overview 三桶）

桶：**A** = 进行中（active 列表）；**R** = 最近完成（recent_completed）；
**H** = 仅历史（不出现在面板任何当前列表，`runs.list`/`runs.get`/
`tasks.get` 与用量照常可查）。

| 控制 \ 执行 | planning | running | blocked | done | 未知(执行) |
|---|---|---|---|---|---|
| active | A | A | A ＋ 关注(blocked_run) | R | A ＋ 警告 |
| paused | A〔已暂停〕 | A〔已暂停〕 | A〔已暂停〕＋ 关注 | **R〔已暂停〕** | A〔已暂停〕 |
| cancelling | A〔取消中〕 | **A〔取消中〕（未关闭执行保留）** | A〔取消中〕＋ 关注 | **H**（竞争完成不聚合） | A〔取消中〕 |
| cancelled | **H** | **H** | **H** | **H** | **H** |
| done | A | A | A ＋ 关注 | R | A ＋ 警告 |
| 未知(控制) | A | A | A ＋ 关注 | R | A ＋ 双警告 |

〔〕内为控制层徽标：分组由执行事实决定，控制状态作为并列徽标呈现，
两个维度同时在卡片/行/详情上可见。

关键格语义（对应 Goal 验收原文）：

- **paused/running**：暂停不饿死在途执行——Run 仍在 active，徽标
  已暂停；不是异常，不进关注、不通知。
- **paused/done**：暂停期间 Run done 是执行事实，可进最近完成，但
  徽标保持已暂停——**不冒充 Goal done**（Goal 仍占未完成槽）。
- **cancelling/未关闭执行**：收敛前 Run 仍 running/blocked——留在
  active，徽标取消中，开放执行的事实完整保留。
- **cancelling/done**：与取消竞争的完成保留其事实（可查）但永不聚合成
  “完成”——Goal 只能收敛为 cancelled，故进 H。
- **cancelled/\***（含 blocked、done、running 等所有执行态）：不进当前
  活跃、不进最近完成、不进失败关注、不触发系统通知；历史失败与用量
  通过查询接口照常可查。

## 3. 关注与通知口径

- 关注（attention）按当前事实：`blocked_run`（Run 受阻）与
  `failed_task`（当前计划未恢复的失败任务）。**已知 cancelled 的
  Goal 不产生任何关注项**；paused/cancelling/done/未知控制态不改变
  关注口径——执行层失败/受阻仍是当前事实。
- 通知只发自事实型关注项（`failed_task`/`blocked_run`）。本任务不新增
  任何暂停/取消类关注或通知类型：控制态是徽标，不是异常。
- 历史失败（被接替的失败、cancelled Goal 下的失败）留在查询与用量
  账本中，不进当前关注，不重复报警。

## 4. 计数口径（不重复计数）

- 一个 Run（project＋run_id 复合身份）在面板三区中至多出现一次；
  `summary.active_runs == len(active)`、`attention == len(attention)`、
  `recent_completed == len(recent)`。
- 菜单栏数字 = 进行中 Run 数（A 桶），cancelled 已排除。
- 引擎调度档位的活动执行计数（`engine._count_active_runs`）使用同一
  `lifecycle_view` 规则：cancelled Goal 的 Run 不再维持 active 档刷新
  频率；cancelling/paused 下未结束的 Run 维持。

## 5. 未知值

- 未知的 Goal 控制值：`unknown-goal-status` 警告，控制层显示未知，
  分组退回执行事实决定；绝不猜成终态。
- 未知的 Run 执行值：沿用 `unknown-run-status` 警告，永不视为终态。
- 无 Goal 行（旧数据/run 无 goal_id）：控制层缺省（徽标不显示），
  分组按执行事实。

## 6. Python/Swift 契约同步

- 线上形状（wire）保持协议 v1 不变：`_run_item` 的 `goal.status`
  `{raw, known, display}` 块已承载控制层，分组由引擎决定，Swift
  `GoalBlock`/`DisplayStatus` 解码同步。
- 旧缓存/旧字段兼容：goal 块缺省、关注项无 evidence 等旧形状照常
  解码（可选字段），缓存文件无需迁移。
