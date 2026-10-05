# 重规划对应关系与工作分类契约（G004）

创建：2026-10-05。状态：契约与纯校验已实现（阶段 1）；存储接口与 v9 持久化已实现（阶段 3 的落库部分，T002）；dispatch 接线与执行面引用见 §9 阶段划分。

本文件是 Plan IR 重规划映射（`replan` 字段）的正式契约：新计划修订生效前，必须产出
新旧任务的对应关系与工作分类；成果（artifact/evidence）按此对应关系引用，而不是按任务编号。

权威实现：

- 字段与解析：`src/orx/plan.py`（`ReplanMapping` / `ReplanTaskMapping` / `ReplanSource` / `ReplanSuperseded`）
- 枚举：`src/orx/records.py`（`ReplanClassification` / `SupersededDisposition`）
- 校验：`plan.validate_ir`（结构内检）与 `plan.validate_replan`（对照历史的外检，纯函数）
- 持久化：`src/orx/state.py` 的 `Store.replan_*` 接口（schema v9，additive 迁移；见 `docs/observability-contract.md` 的 v9 修订）
- 行为测试：`tests/test_plan_ir.py`（纯校验）、`tests/test_state.py`（存储与迁移）

---

## 1. 依据边界（先声明什么不是依据）

R003 复盘曾提出"12M token 浪费在重做"并据此建议自动继承旧 passed 状态。该说法
**不成立**：所谓"重做"来自把不同计划修订中的同号任务误判为同一工作。本契约：

- 不把"12M token"当作事实引用，也不作为任何功能依据；
- 不做旧 passed 状态自动继承——没有任何代码路径把旧任务的 passed 变成新任务的
  状态或免验资格；
- 新计划中的每个任务都从 pending/runnable 起步，Goal 验收条款仍须逐字落入任务
  acceptance（已有 M1 规则不变）。

## 2. 核心不变量：编号不是身份

任务编号（T001、T002…）只在单个修订内唯一。**不同修订中的同号任务是不同的工作**，
任何组件不得按编号自动建立对应。新旧任务的唯一对应来源是计划里显式声明的
`replan` 映射：来源以 `(revision, task_id)` 二元组完整标识，两端（新任务引来源、
旧任务声去向）必须互相吻合（§6 双向一致性）。

## 3. 正式 IR 字段

`replan` 是 Plan IR 的顶层字段。首次计划为 `"replan": null`（strict output schema
要求键存在）；历史 IR（无该键）照常可读（`extra="ignore"` 语义不变，未知字段仍被
忽略，正式字段经解析/严格 schema/序列化往返无损）。

```json
{
  "replan": {
    "prior_revision": 1,
    "tasks": [
      {
        "task": "T101",
        "classification": "new | confirm | redo | continue",
        "sources": [
          {"revision": 1, "task_id": "T001", "part": false}
        ],
        "redo_reason": "仅 classification=redo 时必填：为什么这项工作必须重做",
        "confirm_verification": [
          "仅 classification=confirm 时必填：当前验证要求，且必须逐字出现在该任务的 verification 列表中"
        ],
        "artifacts": [
          "经由本对应关系继承/引用的成果路径（项目相对路径或已记录的证据引用）"
        ]
      }
    ],
    "superseded": [
      {
        "revision": 1,
        "task_id": "T001",
        "disposition": "confirmed | continued | redone | split | merged | dropped",
        "successors": ["T101"],
        "note": "仅 disposition=dropped 时必填：为何放弃该工作"
      }
    ]
  }
}
```

## 4. 工作分类（classification，逐任务必填）

重规划修订中，**每个**新任务都必须声明且只声明一个分类；缺分类是定位到任务的错误。

| 分类 | 语义 | 结构要求 |
| --- | --- | --- |
| `new` | 新增工作，无先前对应 | `sources` 必须为空；声明了来源即错误 |
| `confirm` | 已完成工作只需确认仍有效：原样沿用，不重做 | `sources` 非空且每个来源状态必须记录为 `passed`；**必须列出当前验证要求**（`confirm_verification`），且每条逐字出现在该任务 `verification` 里——**旧 passed 不能替代当前验证** |
| `redo` | 确实需要重做（已完成工作再次进入实施） | `sources` 非空（来源状态不限）；**`redo_reason` 必填**，须说明为什么：什么变了、什么错了、新计划需要什么旧结果给不了 |
| `continue` | 未完成延续：旧修订未终态的工作继续做 | `sources` 非空且每个来源必须是非终态（pending/runnable/running/waiting_*/verifying）；终态来源用 redo 或 confirm |

## 5. 来源身份与对应形态（sources）

来源 = `{"revision": R, "task_id": T, "part": bool}`。`part=true` 表示本任务只覆盖
旧任务目标的一部分。四种形态都有明确表达：

- **重编号**：旧 `1:T001` → 新 `T101`。T101 的 sources 引 `1:T001`，`superseded`
  里 `1:T001` 声 `successors: ["T101"]`。编号变了对应不丢。
- **拆分（split）**：一个旧任务分布到 ≥2 个新任务。每个新任务引同一来源且必须
  `part=true`（多个新任务引用同一旧任务而任一声明全覆盖，是定位到该新任务的错误）；
  旧任务 disposition 为 `split`，successors 列全部新任务。各部分分类可不同
  （如一半 confirm、一半 redo）。
- **合并（merge）**：≥2 个旧任务合成一个新任务。新任务 sources 列全部旧任务；
  每个旧任务 disposition 为 `merged`，successors 都指向该新任务。
- **部分对应（partial）**：只对应旧任务的一部分时用 `part=true` 单独表达；其余
  部分由其他任务覆盖或由旧任务方向显式处置（split/dropped）。

来源可引用更早修订的记录（例如修订 3 的计划确认修订 1 完成的工作）；只有被取代
修订（`prior_revision`）的任务必须出现在 `superseded`。

## 6. 旧任务去向（superseded，逐旧任务必填）

`prior_revision` 修订中的**每个**任务都必须有一条 `superseded` 记录声明去向；
缺失是定位到旧任务（`R:Txxx`）的错误。

| 去向 | 语义 | 结构要求 |
| --- | --- | --- |
| `confirmed` | 工作被原样确认沿用 | 恰好一个引用它的任务，且分类为 `confirm` |
| `continued` | 未完成工作延续 | 恰好一个引用它的任务，且分类为 `continue` |
| `redone` | 工作被重做 | 恰好一个引用它的任务，且分类为 `redo` |
| `split` | 拆分到 ≥2 个新任务 | successors ≥2 且与实际引用一致 |
| `merged` | 与其他旧任务合并进一个新任务 | 该新任务的 prior 修订来源 ≥2 |
| `dropped` | 有意放弃 | successors 为空；`note` 必填说明为何放弃 |

**双向一致性**：每条 `superseded` 声明的 `successors` 集合，必须恰好等于以该旧任务
为来源的新任务集合（声明了没人引用、或引用了没声明，都是定位到旧任务的错误）。
这是"新旧即使重新编号也能追溯"的机械保证：沿任一方向都能走通。

## 7. 结构检查与语义审查的边界

**结构检查**（机械、确定性，由 `validate_ir` / `validate_replan` 执行，错误定位到任务）：

- 分类/去向取值合法；每个新任务恰有一条分类；每个 prior 修订任务恰有一条去向；
- 非空/互斥字段规则（redo↔redo_reason、confirm↔confirm_verification、new↔无来源）；
- 来源可解析（`(revision, task_id)` 在记录中存在）、分类与来源状态匹配
  （confirm←passed；continue←非终态）；
- 双向 successors 一致；split/merged/dropped 的形状要求；多引用必须 part=true；
- confirm_verification 是合法验证条目且逐字出现在任务 verification 中。

**语义审查**（结构检查证明不了，交给独立 verifier/Controller 判断，本契约不机械化）：

- redo_reason 是否**真的成立**（理由是否站得住，还是把"没读懂旧成果"当"需要重做"）；
- confirm_verification 是否**足够**（列出的当前验证能否真正证明旧工作仍有效）；
- dropped 的 note 是否诚实、拆分边界是否合理、artifacts 引用是否指向真实相关的成果。

校验函数文档字符串与实现均声明该边界；没有任何结构规则替语义审查做决定。

## 8. 成果引用（artifacts）

成果按可追溯的对应关系引用：`ReplanTaskMapping.artifacts` 挂在"新任务 ↔ 来源"的
边上，引用项目相对路径或已记录的证据（结构上仅要求非空字符串；指向是否恰当属
语义审查）。规划提示词明确要求"经由对应关系引用成果，绝不按任务编号引用"。

存储面（阶段 3 的落库部分，schema v9，已交付）：`state.Store.replan_mapping_save`
把声明的映射规范成可查询的行（映射头、任务分类、来源边、旧任务去向）；来源以
`(run, 修订, 任务)` 解析到具体任务行，同号任务在另一修订中不满足解析。
`Store.replan_artifact_source_add` 在**已声明的**（新任务 ↔ 来源）边上记录成果出处，
每行保留 Run、修订、来源任务全身份、来源任务自身的 attempt、evidence 行与成果引用；
attempt 与来源身份不符（同号不同修订）即拒绝。预检报告经 `replan_report_add` /
`replan_report_bind` 落库：修订落地前 `revision_id` 为 NULL（unknown，不猜测）。

执行面（阶段 2，已交付 T003）：`dispatch._replan_precheck` 是唯一的差异预检——
`orx plan check --file` 只读运行；`submit_plan`（手工 `orx plan submit` 与 CLI planner
自动提交都汇聚于此）在生效前**重新**运行同一预检（不缓存、不信任早先的报告：来源
状态变化在提交时重新判定）。预检内容 = `validate_ir` 结构内检 + `validate_replan`
以 `replan_snapshot` 的任务行外检 + prior_revision 必须等于当前 active 修订 +
成果绑定检查。成果绑定规则：映射 `artifacts` 中出现且恰好是本 Run 已记录 evidence
路径的引用，必须属于该任务**已声明的来源**（按全身份 `(修订, 任务)` 匹配，相对路径
按记录绝对路径的尾部匹配）；指向其它任务的 evidence 即"错绑"，定位到该新任务拒绝；
不是已记录 evidence 的路径（仓库路径等）结构上无法判定，按 §7 留给语义审查并在
报告中标注 `unresolved`。

预检失败（`plan.ReplanCheckFailed`，携带分类报告）时**原计划继续有效**：不创建修订、
不取消旧任务、Goal/Run 状态与规划指派不动（waiting 的规划指派与打开的 planner
attempt 原样保留），失败报告本身经 `replan_report_add` 落库审计（revision 保持
NULL）。生效是单事务：作废旧修订+取消未终态旧任务、插入新修订与任务、
`replan_mapping_save` 落映射、报告 `replan_report_bind` 绑定到落地修订、关闭规划
指派——任一步故障整体回滚，不存在部分生效的修订。已完成 Run 的重规划只在**新修订
真正落地时**才重开 Goal（run 状态由新任务重新计算）；路由重规划与预检失败都不再
提前改变完成状态（`completed_at` 不丢）。只读 `orx plan check --file` 除落一条
`replan_reports` 审计行外不写任何状态。报告（JSON envelope 与可读文本双形态）展示：
新旧对应（含重编号）、四类分类、重做原因、契约差异（将取消的未终态旧任务、保留的
终态事实、验收条款覆盖）、旧任务去向（含记录状态与去向）、来源状态核对、引用问题。

## 9. 实施阶段与当前状态

- **阶段 1（已交付）**：本契约 + IR 字段 + 纯校验 + 行为测试 + 规划提示词同步。
- **阶段 2（已交付，T003）**：dispatch 接线——共用差异预检
  （`_replan_precheck`：`validate_ir` + `validate_replan` 快照外检 + prior_revision
  一致性 + 成果绑定检查）与原子生效路径；`orx plan check --file` 只读命令；手工
  submit 与 CLI planner 同一门禁（无跳过预检的生效入口）；失败保留原计划与规划
  指派；已完成 Run 失败重规划不提前重开；既有重规划夹具显式声明关系（同号推断
  不再能绕过检查）。exit codes 0/1/2 与 `--json` envelope 不变量保持。
- **阶段 3**：
  - **落库（已交付，T002）**：存储接口 + additive 整数版本迁移 v9
    （`replan_mappings` / `replan_task_mappings` / `replan_sources` /
    `replan_superseded` / `replan_reports` / `replan_artifact_sources` 六表，
    backup-replace-restore 流程保持，v8 旧库升级后新表为空、历史缺失保持
    unknown）；多轮追溯（`replan_trace_chain`）、拆分/合并、数据库重新打开后
    均可查询；观测读取契约同步至 v9（gate 只接受明确支持的版本 8/9）。
  - `orx status` 呈现对应关系（待做）。
- **阶段 4**：worker/verifier 提示词与证据按对应关系引用旧成果（不再按编号）。
- **阶段 5**：端到端 dogfood + 全量回归 + 契约文档终审。

## 10. 兼容性

- 首次计划：`"replan": null`（或省略键）照常解析、校验；`to_dict()` 输出形状不变
  （exclude_none）。
- 历史 IR：无 `replan` 键的既有文档照常可读；`replan` 内外未知字段同样被忽略。
- 存储迁移（T002）：v9 为 additive 整数版本迁移，只新增表、不改既有表、不回填
  任何行；迁移走既有 backup-replace-restore（先迁移副本、成功后替换），失败时
  原库保持 v8 可继续使用。旧任务、attempt、evidence、verification 记录原样保留；
  没有 passed 状态继承，也没有按编号猜测的对应关系。
- 行为变更（T003）：路由重规划（`orx plan`/`orx replan`）不再立即把已完成 Run 置回
  planning/active——重开只在 新修订落地（`plan submit` 成功）时发生；失败的重规划
  不改变完成状态。首次计划（无先前修订）不要求映射，行为不变。
- M0/M1/M1.2 不变量不受影响：exit codes 与 `--json` envelope 约定原样；观测读取
  gate 从单值 `8` 变为显式白名单 `8`/`9`（既有六个读数查询不变）。
