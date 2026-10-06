# Host worker 进展报告与安全恢复观察契约（host-progress-contract）

状态：**已冻结**（R006 / T001，2026-10-06）。T002–T005 按本契约实现；
本文的命令命名、字段、默认阈值、错误规则是权威定义，后续任务不得临时更改——
发现契约缺口时回到 Controller 显式修订（走 replan），不允许实现时静默改契约。

配套文档：[host-progress-plan.md](host-progress-plan.md)（任务图）、
[host-progress-intake.json](host-progress-intake.json)（验收原文）、
[observability-contract.md](observability-contract.md)（只读观测与 schema 白名单）。

---

## 0. 目标与非目标

Controller（或恢复后的新会话）目前只能重浮现正在执行的 attempt 身份，
看不到该 worker 最后一次报告的进展和时间。本契约定义一条**显式、无副作用、
按 attempt 追加保留**的进展报告通道，以及一套只读的观察规则：
当前 attempt 最近一次报告是什么、何时收到、距今多久；
长时间无报告时给出**核查提示**，仅此而已。

非目标（本阶段明确不做，任何文档不得宣称）：

- 不做后台常驻 Controller、定时器、自动心跳或自动推送；
- 不做自动 fail / retry / replan / 取消任务 / 启动第二个 worker；
- 不做租约（lease）、keepalive 或抢占；
- 不做进程存活探测或死亡判定；
- 不扩展到 CLI/external/planner/verifier 的进展报告（host worker 专用）；
- 不修改模型路由、供应商数据库或执行配置。

## 1. 三个不可混淆的概念（本契约的核心语义）

| 概念 | 含义 | 来源 | 能证明什么 |
| --- | --- | --- | --- |
| **报告收到时间** `received_at` | ORX **接收并落库**该报告的 UTC 时刻 | ORX 自己的时钟，写入时生成 | 仅此事实：该时刻有一个持有该 attempt 身份的调用者提交了这段文本 |
| **原 worker 生存状态** | 原 worker 会话现在是否还活着 | **无来源** | **永远 unknown**。不从报告、claim 时间、供应商会话或 token 消耗推测 |
| **任务验收** | 任务是否通过 | 既有 verification / delivery gate | 报告与验收完全解耦：报告不写 verification 行、不影响 verdict、不作为证据 |

由此得出的纪律：

- "最近收到过报告" ≠ "进程仍存活"；"长时间无报告" ≠ "进程已死亡"。
- 报告是**观察事实**，不是 lease、不是存活证明、不是验收输入、不是费用或资源健康信号。
- 缺失报告一律如实显示 `unknown`，不用任何替代推断（包括 claim/attempt 开始时间）填空。

## 2. 命令接口（冻结）

### CLI

```
orx task heartbeat <TASK_ID> --attempt <INT> --phase <TEXT> [--message <TEXT>] [--json]
```

- `TASK_ID`：位置参数，必填。
- `--attempt`：必填，整数。**调用者无法省略或让 ORX 猜测身份**：这是唯一的身份入口。
- `--phase`：必填，有界自由文本（§3）。
- `--message`：可选，有界自由文本（§3）。
- `--json`：输出 JSON envelope（成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": {...}}`）。
- **没有 `--session` 参数，也没有任何客户端时间戳参数**：heartbeat 永不绑定/改写
  `session_ref`，`received_at` 永不由客户端提供。环境变量 `ORX_SESSION_REF`
  属于 Controller 会话，heartbeat 与 identity 门禁都不读它当 worker 身份。

### dispatch 层

```python
dispatch.task_heartbeat(project, task_id: str, attempt_id: int,
                        phase: str, message: str | None = None) -> dict
```

拒绝时抛既有异常族（`NotFoundError` / `ConflictError` / 输入校验错误），
不新增异常类型；成功返回 §5 的结构。

## 3. 输入校验与长度限制（冻结）

`phase` 与 `message` 是**不透明文本**：ORX 不解释、不限制词表、不归一化大小写。
校验只做边界（长度按 Unicode 字符数计，先做首尾空白 strip）：

| 字段 | 规则 | 违反时 |
| --- | --- | --- |
| `phase` | strip 后长度 1–64 字符 | 输入校验错误，exit **2**，reason `phase_invalid` |
| `message` | 可省略；strip 后长度 0–512 字符；strip 后为空视同省略（存 `NULL`） | 超长：exit **2**，reason `message_invalid` |

非规范示例（仅供参考，不构成枚举约束）：`exploring` / `implementing` /
`checking` / `delivering` / `blocked`。

输入校验错误（exit 2，"调用方式错了"）与身份/状态拒绝（exit 1，"调用方式对但
ORX 拒绝"）**必须可区分**。

## 4. 写入身份与所有权门禁（冻结）

一次 heartbeat 写入前，以下条件**全部**满足才落库；全部检查与追加写入在
**同一个事务**内完成（写入瞬间重新核对，不信任读到的旧快照）：

1. `<TASK_ID>` 属于当前 active Goal 的 active Run 的 **active plan revision**；
2. 任务状态为 `running`（`waiting_host`/`waiting_external`/`verifying`/
   `passed`/`failed` 等一律拒绝）；
3. `attempt_id` 存在，且该 attempt：role 为 `worker`、driver 为 `host`
   （host-worker attempt；planner/verifier/CLI/external 一律拒绝）；
4. attempt 的 `revision_id` 与 `task_id` 与当前 active revision 和该任务一致
   （跨修订同号任务不互通）；
5. attempt 是该 (revision, task) 的**最新** attempt（已有更新 attempt 即过期）；
6. attempt 未关闭（`ended_at IS NULL`；已完成/已失败/已 blocked 的 attempt
   **永不因报告复活**）；
7. heartbeat 不写 `session_ref`、不改任何 attempt 字段。

调用者信任模型与 claim/complete 一致：ORX 不做调用者认证，显式 `--attempt`
即身份权威；门禁保证的只是"该 attempt 仍是被认可的当前执行者"。

并发语义：与 `task complete` 竞争时只有两种安全顺序——完成先落，heartbeat
看到已关闭 attempt 而拒绝；heartbeat 先落，报告成为该 attempt 的历史后完成
照常关闭。任何顺序都不产生半写状态，也不阻塞完成。

## 5. 成功响应与错误语义（冻结）

成功（exit 0，envelope `ok: true`）返回字段：

```json
{
  "ok": true,
  "task": "T003",
  "attempt": 42,
  "sequence": 3,
  "phase": "checking",
  "message": "round 2/3 red, fixing",
  "received_at": "2026-10-06T02:00:00.123456+00:00"
}
```

- `sequence`：该 attempt 内的第几条报告，从 1 起单调递增（见 §6）；
- `message`：键始终存在；未提供或 strip 后为空时为 `null`；
- `received_at`：ORX UTC 时钟生成的 ISO 8601 时刻，与库内其他时间戳同源同格式。

拒绝（无任何数据或状态副作用）：

| 条件 | 退出码 | reason（`error.reason`，`--json` 下） |
| --- | --- | --- |
| `--attempt`/`--phase` 缺失、click 参数错 | 2 | usage（click 自身） |
| phase 越界/空白 | 2 | `phase_invalid` |
| message 超长 | 2 | `message_invalid` |
| 任务不在 active revision | 1 | `task_not_found` |
| 任务状态非 `running` | 1 | `task_not_running` |
| attempt 不存在 | 1 | `attempt_not_found` |
| role ≠ worker 或 driver ≠ host | 1 | `attempt_not_host_worker` |
| revision/task 不匹配 | 1 | `attempt_foreign` |
| attempt 已关闭（`ended_at` 非空） | 1 | `attempt_closed` |
| 已有更新的 attempt | 1 | `attempt_superseded` |

`error.message` 为人类可读说明（含定位信息，如涉及的任务/attempt 号）。
文本输出与 `--json` 表达同一事实。所有拒绝路径**不写任何行**：无报告行、
无 task event、无 verification 行、无 attempt 字段变化、无 check round 消耗。

## 6. 存储模型（T002 实现的权威定义）

- 新表 `attempt_progress`（additive 整数版本迁移，v9 → **v10**，沿用既有
  backup-replace-restore；迁移失败原库可用，WAL 已提交记录不丢）：

  | 列 | 类型 | 说明 |
  | --- | --- | --- |
  | `id` | INTEGER PK | 行 id |
  | `attempt_id` | INTEGER NOT NULL | 外键绑 `attempts.id`（真实 attempt 行，非任务号） |
  | `sequence` | INTEGER NOT NULL | attempt 内 1 起单调递增；`UNIQUE(attempt_id, sequence)` |
  | `phase` | TEXT NOT NULL | strip 后的值 |
  | `message` | TEXT NULL | 未提供/空为 NULL |
  | `received_at` | TEXT NOT NULL | ORX UTC 时钟 ISO 8601 |

- **追加即全部**：ORX 代码没有任何 UPDATE/DELETE 路径；历史永不覆盖、永不删除。
- `sequence` 由 Store 在写入事务内分配（该 attempt 当前 max+1），保证并发下
  严格递增；`received_at` 同刻并列时以 `sequence` 为权威顺序。
- 绑定 attempt 行意味着**天然隔离**：跨修订同号任务、重试后的新 attempt，
  都是不同 attempt 行，互不可见、互不冒充。

Store 只读/写入接口（命名冻结）：

```python
store.attempt_progress_add(attempt_id, phase, message, received_at) -> int   # 事务内分配 sequence，返回行 id
store.attempt_progress_latest(attempt_id) -> row | None                      # sequence 最大的一条
store.attempt_progress_all(attempt_id) -> list[row]                          # sequence 升序（追加序）
```

旧库规则：升级到 v10 后表为空——`latest()` 返回 `None`，观察层显示 `unknown`；
**不回填、不从 claim 时间/供应商会话/token 用量推测任何历史报告**。

## 7. 只读观察契约（T004 实现的权威定义）

**当前窗口**：任务 T（active revision）的当前 attempt =
`attempt_latest_for_task(active_revision, T)`。所有"当前进展"观察
（`orx status`、`orx task list`、`orx run` 的 recovery 面）**只读当前窗口**：

- 当前 attempt 无报告 → `unknown`，**绝不回落**展示旧 attempt 的报告；
- 展示字段（JSON 形态；CLI 文本表达同一事实）：

```json
"progress": {
  "attempt": 42,
  "state": "unknown | reported | overdue",
  "phase": null, "message": null, "received_at": null,
  "age_sec": null,
  "timeout_sec": 3600,
  "hint": null,
  "note": null
}
```

- `state`：`unknown`（无报告）｜`reported`（有报告且未过阈值）｜
  `overdue`（有报告且 `age_sec >= timeout_sec`，§8）。
- `age_sec`：读取时钟 − `received_at`（秒）。仅在能诚实计算时非空。
- `hint`：仅 `overdue` 时非空，文本建议**核查原会话**，例如
  "no progress report for 92m (>= 60m threshold); check the original worker
  session (handle: session_ref) before any fail/retry"。
- `note`：时钟异常说明（§8），正常为 `null`。

**timeline**：每条报告追加历史事件 `attempt.report`，actor 为该 attempt 的
profile，detail 携带**完整身份**（role、task、attempt 号、sequence、phase、
message），例如 `worker T003 a42 #3 checking — round 2/3 red, fixing`。
历史事件按既有 timeline 规则时间排序、可按 task/profile 过滤；它**只是历史**，
任何视图不得把历史 attempt 的报告当成本次任务的当前进展。

观察路径全部只读：不改任务状态、不创建 attempt、不触发模型调用、不写库。

## 8. 超时阈值与配置键（冻结）

- 配置键：`[worker] progress_timeout_min`（分层配置：环境覆盖 > 项目
  `.orx/config.toml` > 用户层 > 内置默认；与既有键同机制）。
- 类型：**正整数**（≥1，分钟）。`0`、负数、非整数一律按既有正整数校验拒绝。
- **内置默认 60**（分钟）。有效比较单位为秒：`timeout_sec = 60 × 配置值`。
- 比较与边界：`age = 读取时钟 − received_at`；
  `age < timeout_sec` → `reported`；`age >= timeout_sec` → `overdue`
  （**恰好等于阈值即 overdue**，边界闭区间）。
- 超时提示只建议核查原会话（§7 hint 文本），**不触发**任何状态迁移：
  不 fail、不 retry、不 replan、不取消、不另起 worker、不撤销 attempt、
  不拒绝该 attempt 之后的合法交付。

**时钟异常规则**：

- `received_at` 永远来自 ORX 的 UTC 时钟（与库内时间戳同一可注入时钟缝，
  测试以固定时钟注入，不真实 sleep）；客户端无法注入时间。
- 读取侧遇到无法诚实计算年龄的情况——`received_at` 缺失/不可解析、或
  `received_at` 晚于读取时钟（时钟回拨，`age < 0`）——一律：
  `state = "reported"`（确有报告）、`age_sec = null`、`note` 写明异常
  （如 "clock anomaly: received_at after read clock"）、**不触发 overdue 提示**。
  不显示 0 岁、不显示负数、不据异常时间判定过期。

## 9. 无副作用边界（冻结）

heartbeat 及所有观察路径**永不**改变：任务状态与 task events、verification
行与 verdict、attempt 任何字段（含 `session_ref`、`ended_at`、`result`）、
check rounds、token/用量记录、Goal/Run/revision/路由状态。heartbeat 不消耗
检查预算、不写 evidence、不算交付。

## 10. 恢复边界（冻结）

沿用 phase C 的 no-second-writer 契约，并叠加本契约：

- `orx run` recovery 对 RUNNING host 任务重浮现 attempt 身份 + 当前窗口的
  进展观察（§7 字段）+ 既有指引（先查原子会话；确认死亡才显式
  `task fail` + `task retry`）。
- 原 attempt 的**合法迟到交付**照常受理：过期/无报告不使交付失效，
  `task complete --attempt <id>` 仍走既有门禁（最新、未关闭、自身 gate 绿）。
- 显式 `fail` + `retry` 打开新 attempt 后：新窗口从 `unknown` 开始；
  **旧 attempt 的 heartbeat 被拒**（`attempt_closed` / `attempt_superseded`），
  其报告仅存于 timeline 历史。
- 死亡确认是**人的决定**：ORX 只提供观察与提示，不做判定。

## 11. 禁止宣称（文档与 skills 的红线）

不得把显式报告接口宣传为：自动心跳/定时报告、存活证明或死亡证明、
租约或保活、验收依据、自动恢复、token/时长节省（未实测的数字一律不写）。
worker 技能只说"关键阶段前后**显式**调用 heartbeat"；Controller 技能只说
"有界等待、先核查原会话再决定 fail/retry"。

## 12. 冻结的实现步骤与独立测试指引

| 任务 | 冻结交付 | 可独立编写的行为断言 |
| --- | --- | --- |
| T002 | §6 表与 Store 三接口；v10 additive 迁移；observability 白名单 + 只读查询 + seed fixture | add→重开库→latest/all 一致；迁移失败原库可用、WAL 保留；旧库升级后 latest 为 None（unknown）；sequence 并发单调；同号跨修订/重试后按 attempt 隔离 |
| T003 | §2 CLI 与 dispatch 接口；§4 门禁；§5 响应与错误表 | §5 表逐行一测试（退出码、reason、无副作用：状态/session_ref/verification/用量不变）；成功路径字段逐一断言；`ORX_SESSION_REF` 存在时 heartbeat 仍不写 session_ref |
| T004 | §7 观察字段；§8 配置键与边界；timeline `attempt.report` | 固定时钟覆盖 age <、==、> 阈值三边界；无报告 unknown；retry 后新窗口 unknown 且不显示旧报告；时间戳异常（未来/坏值）不触发 overdue；配置层优先级与正整数校验 |
| T005 | 端到端替身 + README/skills 同步 | claim→报告→超时提示→恢复重浮现→原 attempt 合法迟到交付受理；fail/retry 后旧 attempt 报告与交付均拒；文档不越 §11 红线 |

测试书写者只需本文即可断言以上行为，无需阅读实现。
