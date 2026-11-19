# 指标口径（冻结）

状态：Phase 1 冻结。修改需在最终报告中记录原因。本文件定义 orx-analytics
对外发布的每一个数字怎么算、分母是什么、缺失如何呈现。实现以
`src/orxanalytics/metrics.py` 为准，二者不一致时是缺陷。

## 总原则

1. **未知保持未知**：任何字段缺失（NULL）不补零、不插值、不升级。部分覆盖的
   和标注为 partial，并同时发布缺失计数。
2. **覆盖率与准确度分离**：`observed/attempts` 是覆盖率；`exact/estimated/
   unknown` 是观测行准确度。不混合成一个标签。
3. **口径可复算**：每个数字都能从已导入的行用一条确定性查询重算；报告附
   使用的 provenance（来源、reader 版本、快照时间）。
4. **不计费**：无计费依据，不推算费用或订阅额度。
5. **token 字段保持原语义**：input/output/cached/cache_write 等按来源原样
   存储与求和，不做跨工具换算，不做 `input - cached` 之类算术。
6. **展示统一千位分隔**（G012，macOS 展示层）：token 与计数在 App 内显示
   时按固定三位逗号分组（`1,234,567`），不随系统 locale 变化；仅影响显示，
   不改任何存储值、标识或求和口径。实现为 `MetricsFormat`（单一入口），
   未知 "—"/显式 0/两口径并列规则不变。

## 六读数（ORX 项目）

### 1 交付质量 delivery_quality
按 `(run_id, revision, task_id)`（短编号全链带 run/revision，绝不跨项目或跨
revision 并账）列出终态；`passed/terminal` 为该 revision 内比值，分母是
`passed|failed|cancelled` 的任务数。完成后缺陷来自 findings 记录，无则 NULL。

### 2 交付消耗 consumption
- 归属：`attempts.run_id = :run`（**不是** `revision_id IN (...)`——后者漏掉
  revision 为 NULL 的 planner attempt）。
- 每 attempt 取规范观测：`(attempt_id, source)` 中按 source 优先级
  `native_cli > zcode_db > host_report > output_estimate`（v12 契约第六修正
  引入 `zcode_db`，序次在 native_cli 之后、host_report 之前），同级取最小
  `usage_observations.id`。
- 和为 partial sum：任一规范行字段 NULL 时同时发布 `*_missing` 计数。
- 会话侧对照（zcode 关联可用时）：树汇总 = 关联会话及其全部后代的
  `turn_usage.input+output`（与 `part.step-finish` 逐步累计互证；二者不一致
  是缺陷）。会话树与 ORX 侧观测是**两个口径**，分开报告，不互相覆盖。

### 3 完成时间 completion_time
- `span_sum`：完整执行跨度（`ended_at-started_at`）之和，重叠不合并；不完整
  跨度计数并列。
- `waiting`：`runs.completed_at - runs.started_at`（生命期时钟）。
- 二者分开；都不从 `created_at/updated_at` 推导。
- 会话时长维度（非 ORX 项目也有）：会话首尾消息时间、模型请求时间合计、
  工具时间合计、最长无进展间隔（相邻活动之间最大空档）。

### 4 首次交付通过率 first_pass_rate
- 分母：**至少有一条 verification 行**的任务（`(revision_id, task_id)`）。
  从未到达验证的任务在率之外（另计入 `not_reached`），不算失败也不算通过。
- 分子：最早 verification 行（`created_at`, `id` 排序）`passed=1` 且该任务
  无 `to_status='failed'` 事件。
- 跨 revision：每个 revision 单独统计；跨 revision 汇总只做任务行级罗列，
  不把同编号任务合并（见 correlation.md）。

### 5 返工 rework
每次 `to_status='failed'` 事件一行，附 failure_kind 前缀归类
（`delivery blocked` / `delivery failed` / `verification`）。区分：
- 同 attempt 内修复：同 attempt 的多条 command verification 行（check→fix→check）；
- 跨 attempt 重试：同 `(revision, task)` 的多个 worker attempt；
- 跨 revision 重做：replan 对应表声明的 `redo` 分类（v9），没有声明就是未知；
- 重复实现：同源任务多后继/多交付，需 replan 边或 evidence 支撑，否则存疑。

### 6 数据覆盖 coverage
- ORX 侧：按 role/profile/harness 的 attempts 覆盖（observed /
  missing_recorded / unassessed），字段级覆盖，会话关联覆盖
  （有权威 session_ref 的 worker attempt / 总 worker attempt）。
- 会话侧：已导入会话中 token 观测可用比例；来源探针失败按缺口列出。

## 会话侧基础指标（非 ORX 项目同样可用）

- `session_totals`：单会话 input/output/cache_read/cache_creation/reasoning
  （turn_usage 汇总）。
- `tree_totals`：会话树（自身+全部后代，按 id 去重）汇总；父子不重复计数
  （一次导入中同会话只计一次；重复导入靠天然主键幂等）。
- `logical requests vs retries`：`model_usage` 按 `logical_request_id` 去重为
  逻辑请求数；`attempt_index>0` 与状态为重试/错误的行为重试计数。token 求和
  只用 turn/step 观测，不用请求行数乘任何系数。
- `model_usage` 请求口径镜像（G012，store v4）：`zc_requests` 增列镜像
  `model_usage` 的 `input/output/reasoning/cache_read/cache_creation/
  provider_total/computed_total` token 字段。一个请求的身份是
  `(session_id, logical_request_id, attempt_index)`（镜像天然主键，重复采集
  幂等去重）；`cache_read` 是 input 的**子集**，派生处理量 = input＋output，
  绝不把缓存叠加到 input 之上；`turn_usage` 是另一粒度口径，永不与请求行
  求和相加；状态为 `error` 且 token 全为零的行是**零占位**（计入请求数并
  以 `error_rows_zero_tokens` 标记，绝不视为免费或隐藏）；状态不在终态
  词汇表（completed/error/cancelled）的行计入 `settling_request_rows`，其
  累计数是暂定值并如实标注；来源缺列（旧库）镜像为 NULL 并在导入计数中
  列名，缺字段行进部分和与逐字段缺失计数，未知永不补零。
- `tool_activity`：按会话的 tool_calls、错误数、读写分布。
- Codex：`token_usage_record.usage` 逐响应累计（input/cached/cache_write/
  output/reasoning 原样）。
- Cursor：composerData.usageData 原样记录；字段缺失标记 unknown。

## 多 attempt 会话的用量拆分

一个会话承载多个 attempt 时：
- **会话总量**永远可报（turn_usage 汇总）。
- **attempt 可归属量**仅当关联依据能定位 attempt 窗口（起止 turn 或时间窗）
  时计算，并把窗口外用量列为 `unattributed_within_session`。依据不足时只报
  会话总量并注明不可拆分，不按时间邻近猜测。

## 任务/Run 用量归属读取模型（G002 T002 新增，2026-10-16）

本节为**新增**口径，不改动上文任何冻结读数。实现以
`src/orxanalytics/usage_readmodel.py` 为准（只读，仅 SELECT），回归测试
`tests/test_usage_readmodel.py`。面向菜单栏应用的"这个任务/这次执行花了
多少"，与六读数并列。总原则（未知保持未知、覆盖率与准确度分离、口径可
复算、不计费、字段原语义）同样约束本节。

### 归属账本

- **Task 消耗**＝`(run_id, revision_id, task_id)` 全部可归属 attempt 之和：
  worker 含失败与重试轮；可明确关联的 verifier 计入任务并单列
  （`books.verifier`）；其他角色挂任务时按角色名列出但**不计入**任务总额，
  直到契约命名它。
- **planner** 归 Run 规划开销：仅凭 `attempts.run_id` 归属（`revision_id`
  为 NULL 仍正确入账），`revision_ids` 如实携带 NULL。
- **controller**：ORX 已记录（attempt 行 `role='controller'`）才进 ORX
  口径的 `books.controller`；会话侧推导的 controller 自身消耗只存在于
  原生预览口径（见下），绝不混入 ORX 总额。
- **未归属**：`run_id` 已知但无法归属到任务或已命名角色的进
  `unattributed_within_run`；`run_id` 未知或 observation 无 attempt 关联的
  进 `project_unattributed`。一律**绝不**按任务数、attempt 数或时间摊分；
  短编号（T001）不是身份，不得把未归属行挂到任何任务。
- **Run 汇总覆盖全部 revision**（按 `attempts.run_id`），重规划不丢旧版本
  消耗；`per_revision` 逐版本给出 token 与覆盖，无观测版本保持 None。

### 规范观测与汇总附注

- 每 attempt 取一条规范观测（`native_cli > zcode_db > host_report >
  output_estimate`，同级最小 id），与第 2 读数同一规则、同一实现；不同
  来源是同一工作的重复视角，**不相加**。
- 每个汇总（task/run/各账本/未归属）携带：`coverage`
  （`observed/attempts`＋`missing_recorded/unassessed`）、`missing_fields`
  （逐字段 NULL 计数）、`accuracy`（canonical 行 `exact/estimated/unknown`
  分布；accuracy 为 NULL 或无法识别计 `unknown`）、`as_of`
  （`orx_snapshots.taken_at`）。
- 完全未知为 `None` 不补零：某字段在汇总范围内无任何非 NULL 观测即为
  `None`；仅当来源明确记录为零（存储 0）才出现 `0`。部分已知是带缺失
  计数的部分和（`partial` 语义由缺失计数表达）。

### 两口径结构分离

- `orx_recorded`：上述全部，仅来自 `orx_usage_obs` 的规范观测。
- `native_session_preview`：原生会话预览——关联会话/线程的当前累计量，
  仅来自会话镜像（`zc_*`/`cx_*`）。树内按 id 去重、父子不重复计数；
  union 对全部关联树每会话只计一次；**不同树相交**（一 attempt 的祖先是
  另一 attempt 的会话）列为 `subtree_conflicts`；同一棵树承载多 attempt
  是已知多 attempt 会话情形，列为 `shared_session_trees`，绝不逐 attempt
  相加树总量。controller 会话（见下）报**自身**用量，子代理后代属于各自
  attempt 的账；仅当全部子会话的 attempt 同属一个 run 时
  `run_attribution.attributed=true`。
- 两口径各自携带 `caliber` 标记，预览结果**顶层不出现任何 token 数**
  （只在具名会话/条目内），任何接口都不返回两口径的合并数。

### controller 自身用量（G012 用户反馈 5，2026-10-17）

用户反馈"controller 的 token 没有检测到"：ORX usage 只记 worker/verifier
attempt，驱动循环的 controller 宿主会话（zcode 主会话）token 只在 zcode
`model_usage` 里。补齐方式仍是只读镜像，不写 ORX、不新增 ORX 口径数字：

- **发现**：controller 会话 = 本项目已关联子会话（stored `session_ref` 或
  ORX v11 `ORX_ASSIGNMENT` nonce 锚）沿 `parent_id` 链的**根祖先**（任意
  层级深度），且自身未关联任何 attempt；自身已关联的根会话是 attempt
  账本，不是 controller。目录只作记录事实（符号链接两侧解析后比对），
  绝不作归属依据——子代理继承 controller 窗口目录，可以是别的项目
  （G010 语义）。
- **用量**：`controller_sessions[].own_usage` 为 `model_usage_requests`
  口径（`usage_readmodel.request_totals`），仅统计该会话自身的请求行
  （worker/verifier 后代已在各自 attempt 的账里，绝不叠加），按
  `(session_id, logical_request_id, attempt_index)` 去重，缓存为 input
  子集，处理量 = input＋output，turn_usage 永不叠加，错误零占位显式标记。
- **归属**：仅当其全部子会话 attempt 同属一个 run 且 run 均已知时
  `attributed=true`；同一 controller 会话跨多个 run、或子 attempt run
  未知时保持未归属并给出原因，绝不按 run 数或时间摊分。
- **每 run 判定**：`native_preview.controller_for_run` 给出该 run 的结论
  ——`attributed`（列出会话 id）/ `unattributed`（跨 run 共享等，附原因）/
  `host_session_unbound`（该 run 的 zcode attempt 均无会话绑定，沿用 ORX
  缺量原因词汇）/ `session_not_imported`（session_ref 存在但 zcode 镜像未
  收录该会话）/ `no_controller_session` / `no_zcode_attempts`。判定层不
  携带任何 token 数；展示层（Swift 详情）按统一千位分隔显示 token、来源、
  更新时间与关联依据，未知显示"—"，绝不补零，两口径并列不相加。跨 run
  共享（`unattributed`）时仍显示该会话**自身累计**请求量——那是会话的
  事实而非本 run 的归属额——并强制标注"跨多个 run 共享、未归属单一 run、
  绝不按 run 摊分"；`host_session_unbound` / `session_not_imported` 等无
  会话可列的判定只显示原因，不显示任何数字。

### 标准化处理量 normalized_processing（版本 1）

独立、有版本（`NORMALIZED_PROCESSING_VERSION`）的派生指标，仅用于比较
工作量，**不是计费**；原始 token 求和保持来源语义、保持权威。按 attempt
的 harness 缓存语义（ORX observability contract）逐 canonical 行派生：

| 语义 | harness | 派生 |
|---|---|---|
| cache subset | zcode、codex | `input + output`（再加 cached 会重复计数） |
| cache separate | cursor | `input + cached + output`（cached NULL 时为明确部分和并置 partial） |
| 语义未知 | 其他/缺失 | 该行不派生，计入 `rows_missing` |

操作数缺失的行为 NULL 不补零；`value` 为部分和（`partial=true` 或
`rows_missing>0` 时必须伴随缺失计数发布）；汇总同时给出
`cache_semantics` 三类行数分布。升级语义需递增版本号并补回归测试。
