# G010 T001 诊断报告：真实执行身份与用量关联断点

日期：2026-10-16。任务：诊断并修复真实执行身份与用量关联断点（Analytics
G002/R002）。所有外部数据只读取证（sqlite `mode=ro` + `query_only`）；两个外部
数据库（orx-analytics `.orx/state.db`、`~/.zcode/cli/db/db.sqlite`）未被写入、
迁移或 checkpoint，任何外部历史未篡改。本报告不根据时间相近猜测任何会话归属。

## 定位路径

用户级项目索引 `~/.local/share/orx/projects-index.json` 只读定位：
`/Users/flb/Sources/Tools/orx-analytics`（name `orx-analytics`，last_run_id
`R002`，has_state_db true）。Goal G002（菜单栏观察工具首版交付），Run R002
（2026-10-16T07:24:04Z → 15:35:54Z，done，revision 4 active，13 任务全 passed）。

## 逐段证据链（每段：来源 + 事实）

| 段 | 事实 | 来源（只读） |
|---|---|---|
| 归档 assignment | 18 份归档提示词（13 worker + 5 verifier）全部以 `ORX_ASSIGNMENT=<nonce>` 开头；nonce 与 attempts 行逐一吻合（抽查 T002.md ↔ attempt 15 nonce 520449c9…、verify-T009-01.md ↔ attempt 22 nonce 08a42551…，全文核对为逐字一致） | `.orx/runs/R002/assignments/` |
| nonce 转交 | 真实子代理会话的首个 text part 逐字携带 nonce（如 `sess_subagent_agent_1f18b027…` 首部即 ASSIGNMENT IDENTITY 两行 + 归档提示词全文）。转交未丢失、未被改写 | zcode part（首个 text part 查询，与 discovery 同谓词形状） |
| 会话结构 | 控制器根会话 `sess_97742628…`（parent NULL，title "ORX Analytics 菜单栏工具设计方案"）恰有 16 个子会话，与 16 个 subagent 尝试（attempts 15–30）一一对应；每个 nonce 在子会话首部恰命中一次。另有一会话（`sess_subagent_agent_97d1cd2b…`，实为 T010 worker）仅在**后续** part 引用 T009 verifier 的 nonce——首部携其自己的 nonce bfe57be3…：证明"只读首 part"是必要的消歧规则 | zcode session/part |
| 目录（断点所在） | 全部 16 个子会话与控制器根会话的 `directory` 均为 `/Users/flb/Sources/Products/ORX`（控制器窗口目录，子代理继承），而本 Goal 的 ORX 项目根是 `/Users/flb/Sources/Tools/orx-analytics`。`discover_attempt_session` 的目录谓词（`session.directory` 解析后等于项目根）将每一个 nonce 精确命中的会话全部滤除 → decision "none" → `session_ref` 保持 NULL | zcode session.directory 列 |
| 发现时机 | claim 事件存在（T002 于 07:45:26 host claim），对应会话创建于 07:44:35——会话先于 claim 存在，24h 发现窗口富余。事件表不记录 `--discover-session` 旗标（未知项，见下），但无论旗标如何，目录谓词在上游已滤空 | analytics task_events + zcode time_created |
| 子树读取 | `model_usage` 表存在且数据完备：16 个子会话共 1,222 行请求、input 167,384,405 / output 829,732 / cache_read 165,131,968。T002 worker 会话单独 47 行（6,854,455/59,456/6,668,096）；T009 verifier 会话 100 行（5,200,561/39,169/5,030,912）。若身份绑定成功，子树聚合本可直接入账 | zcode model_usage |
| 结果（ORX 侧） | R002 的 `usage_observations` 为 0 行；18 个尝试全部 `usage_missing_reason = host_session_unbound`。缺失有明确原因（诚实缺失非零），但关联本身从未发生 | analytics attempts / usage_observations |

## 已证实根因

1. **目录谓词误杀 nonce 精确命中**（主根因）：真原生子代理继承控制器窗口的
   `session.directory`；跨项目控制（在 A 项目窗口的会话里驱动 B 项目的 ORX
   run）使每个 nonce 精确命中的会话目录都不等于 B 的项目根。发现函数在目录
   谓词处滤空 → `session_ref` NULL → 闭合点 autobill 走 discovery 同样滤空 →
   `host_session_unbound`。修复（本任务）：nonce 是唯一权威——首 part nonce
   命中即绑定，目录事实记入候选（`directory_matched`）而非作为过滤条件；
   根会话（parent NULL）结构性排除；双命中仍为 ambiguous 不绑定；legacy
   marker 路径目录谓词保持必需（`directory_filter` 字段言明当时适用的规则）。
2. **次生缺口一：用量尚未稳定不可检测**：闭合点读取若撞上仍在途的
   model_usage 行（状态不在终态词表 completed/error/cancelled——该词表为真库
   全量观测），旧代码会把**部分和**记入每 attempt 每源的单例槽，之后的真实
   数字反而无法入账（单例冲突）。修复：聚合新增 `settling_request_rows`；
   存在即返回 `unsettled` 不写库，autobill 记 `zcode_usage_unsettled`；全部
   行终态后的重跑正常入账并清除缺失原因。
3. **次生缺口二：重复补录与子树冲突混标**：`_usage_autobill` 此前对一切
   ConflictError 统一标 `zcode_subtree_conflict`。当 attempt 已有 zcode_db
   观测而新读数值不同（单例守卫拒绝覆盖），这是**重复补录**而非子树重叠，
   且该 attempt 已被观测、不是缺失。修复：先查既有观测行区分两种冲突；
   重复补录返回 `already_recorded` + `zcode_duplicate_backfill`（两个数字并
   陈供人工对账），不写缺失原因、不覆盖存量行。
4. **闭合结果不可见**：五个闭合点算了 `_usage_autobill` 的返回值却全部丢弃。
   修复：`task complete / fail / recover` 与 `verify submit` 结果携带
   `usage_backfill`（recorded 或陈述的原因）。

## 排除项（有证据排除，非根因）

- **nonce 丢失/改写（R006 形态）**：归档提示词与子会话首 part 逐字一致，排除。
- **歧义**：16 nonce ↔ 16 会话一一对应；后续 part 引用他尝试 nonce 的会话不
  构成歧义（只读首 part），排除。
- **发现窗口**：会话创建先于 claim 数十秒，24h 窗口富余，排除。
- **zcode 库缺失/不可读**：只读查询全部成功，排除。
- **model_usage 数据缺失**：1,222 行俱在，排除。
- **ORX_SESSION_REF 误用**：host 路径设计上不读该环境值（控制器会话归属），
  未发生，排除。

## 未知项（如实陈述，不猜测）

- **claim 是否每次都带 `--discover-session`**：task_events 只记 `host claim`
  不记旗标。无论旗标如何，目录谓词在上游已把候选滤空，结论不变；此细节
  无法从既有数据复原，不作为根因主张。
- **Analytics 应用快照的确切时刻**：见下节——计数差异与全部已记录时间戳
  相容，但快照时间本身无记录。
- **R002 头两个尝试（13/14，profile `orx-host` 无 host_mode）**：self 执行，
  执行者即控制器根会话，结构性无子代理会话。绑定控制器根会话正是
  "误用 controller 根会话"反模式（G009 attempt 164 已知边界）；本任务不修，
  这两笔保持"有原因的缺失"。16 个 subagent 尝试的会话在修复后可绑定。

## R002 当前样本 vs 规划背景计数

- **规划背景**（R010 规划意图原文）：16 missing_recorded + 1 unassessed（=17）。
- **当前样本**（终态只读复读）：18 个尝试，18 个 `host_session_unbound`，
  0 unassessed，0 观测行。
- **差异解释**：全部时间戳相容的解释是规划数字来自运行中的活体快照——
  T013 worker（attempt 29）执行期间（15:19:45–15:31:41），当时 attempts
  13–28 共 16 个已闭合且各带 missing 原因，attempt 29 开放中（无观测、无
  缺失原因 = unassessed），attempt 30（verifier，15:31:52 才创建）尚不存在。
  终态时 29、30 双双闭合并各带 `host_session_unbound`：17 → 18。
- **残留未知**：快照的确切时刻无记录（不猜测）；差异的算术（+1 开放尝试
  转闭合 +1 新增 verifier 尝试）与每一条已记录时间戳一致。

## 脱敏真实结构复现（先红后绿）

fixture `tests/fixtures/g010/`（provenance.json 记录全部来源与真实数值）：

- `archived-worker-prompt.md`：R002 归档 T002 提示词逐字节副本（attempt 15，
  nonce 520449c9…，真实会话 47 请求 6,854,455/59,456/6,668,096）。
- `archived-verifier-prompt.md`：归档 verify-T009-01 逐字节副本（attempt 22，
  nonce 08a42551…，真实会话 100 请求 5,200,561/39,169/5,030,912）。
- 会话 id 与目录拼写合成（`/users/sample/controller-window`），结构忠实：
  根会话 parent NULL + 子会话继承窗口目录、nonce 在首个 text part、
  model_usage 携真实 token 数。

红（7 例失败，均为预期信号，`red-green/01-red-run.txt`）：目录谓词下
decision 'none'；跨目录双 nonce 命中被旧逻辑误判 'unique'；
`directory_filter` 键不存在；worker/verifier 准真实闭环绑定 None；
unsettled 被当作可入账（'recorded' ≠ 'missing'）；重复补录被误标子树冲突
（'missing' ≠ 'already_recorded'）。

绿（`red-green/02-green-run.txt`）：四个门禁文件 88 passed（最终轮，含
CLI unsettled 陈述域错误一例）；全量 1061 passed 0 fail（全量包含 m12
再生测试——其工作区影响见 user-file-baseline.json postscript）。

## 零付费准真实闭环（worker + verifier 各一）

worker（tests/test_session_identity.py::
test_quasi_real_worker_loop_with_archived_prompt）：park 在确定化 nonce
（归档 nonce 经 `dispatch.mint_nonce` 替身注入）→ `task claim
--discover-session` 用**实际发现函数**绑定伪库中携带**归档原始提示词**首
part 的会话 → `task_complete`（**实际闭合入口**，delivery gate 跑 `true`）→
autobill 读子树 → 唯一一行 `zcode_db` 观测 = 真实数值 → 任务 passed。
链：run/revision/task/attempt/nonce/session/usage 全串。

verifier（tests/test_subagent_contract.py::
test_quasi_real_verifier_loop_with_archived_prompt）：worker 完成 →
`verify_dispatch`（state 侧 mint 替身注入归档 verifier nonce）→
`verify_submit --discover-session`（实际发现 + 实际闭合入口）→ 会话绑定、
attempt 关闭 result=pass、autobill 入账真实数值。

覆盖矩阵（无匹配/歧义/库不可读或不存在/未稳定/子树冲突/重复补录）：
无匹配与歧义、根会话拒绑、legacy 目录必需 —— test_session_identity.py
（既有 none/ambiguous/unavailable 用例 + 新增跨目录族）；库不存在 —— 既有
`zcode_db_absent` 安静跳过用例；未稳定、重复补录 —— test_usage_autobill.py
新增两例；子树冲突 —— 既有 `zcode_subtree_conflict` 用例。缺失皆有原因
（MISS 词表 + 第七修正案），未知不补零（`_token_sum`/聚合 None 语义未动），
controller 根会话结构性拒绑（parent NULL 谓词 + 新增根会话首 part 携 nonce
亦不绑定的回归钉住）。

## 保护基线与用户文件

`user-file-baseline.json`：起始 HEAD `c96ed8b9…`、docs/m1.2-report.md 起始
sha256 `e1125201…`（未暂存、268/205 相对 HEAD）。任务过程中唯一一次全量验证
运行触发了仓库已知的 m12 再生测试，工作区字节变为 `5289e6e7…`——字节级还原
已尽四条路径（git 悬空对象、APFS 快照、zcode 会话 Read 记录、planner 日志）
均不可得，处置与全部细节见该文件 postscript：内容保持同一机器生成类别、
未暂存、未被本任务提交或还原，本任务不再运行全量（规定检查集不含 m12）。

## 外部历史

两个外部数据库零写入（所有查询 `mode=ro`；测试全部使用临时伪库）；
analytics 仓库仅读取（归档提示词以 cp 复制为 fixture，源文件未动）；
不根据时间相近猜测会话；不改写任何已记录事实。
