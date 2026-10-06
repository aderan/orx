# IMPLEMENTATION_PLAN — orx-pbv（替换 StockMate pbv-loop）

创建：2026-10-03。任务 Goal 原文由用户会话给出；替换契约见 `docs/pbv-mapping.md`（Stage 1 冻结基线）。
Stage 5 验收 case（用户已确认）：StockMate 新样本外窗口评估（2026-09-30 后自动积累窗口）。

状态图例：Not Started / In Progress / Done（以证据为准，不以文件存在为准）。

## Stage 1：冻结替换契约 — Done
- [x] `docs/pbv-mapping.md`：功能与验收映射、词汇/状态/场景映射、内核 API 契约、已知限制。
- 证据：mapping 文档逐条覆盖旧 skill/模板/脚本的正常、失败、复验、阻塞、收尾场景（§3 表）。

## Stage 2：补齐指派与验证契约（内核切片，行为测试先行）— Done
- [x] A1 preread 字段 + worker prompt（约束/preread/prior_failure）+ planner prompt 说明 + plan 校验与 schema（迁移 v6：tasks.preread_json）
- [x] A2 verifier prompt 丰富（约束/acceptance/命令门禁结果/证据/prior issues）
- [x] A3 host/external 任务指派与 host verifier 指派落盘 + payload（prompt/prompt_file/preread/isolation）
- [x] A4 attempt.isolation wiring（CLI=launch.sandbox；host=prompt_only；external=NULL；迁移 v5）
- [x] B codex 按角色沙箱（planner/verifier read-only）+ Launch.sandbox + attempts.isolation（子代理完成）
- [x] C skill 可选安装（`orx skill install [NAMES…]`、动态名称校验、update 只刷新已装；子代理完成）
- [x] A5 `orx verify submit --reason`：fail 问题进入 failure 状态并进入下次 worker/verifier prompt
- 证据：全量 `uv run pytest` 357 绿（321 存量 + 36 新增）；提交 8a712f7。

## Stage 3：实现 skill 与可选安装 — Done
- [x] D `skills/orx-pbv/`（SKILL.md 120 行 + 5 个 references；子代理草拟、GLM 终审并解决 5 处契约歧义）
- [x] 安装更新行为：无参默认不变、显式装 orx-pbv、update 只刷新已装（Stage 2-C）。
- [x] 构建产物包含 orx-pbv 资源（wheel 解包验证 6 个文件齐全）；已实装 ~/.agents/skills + ~/.zcode/skills 链接。
- 证据：tests/test_skills_update.py 23 绿；提交 e65dd3e。

## Stage 4：验证完整开发循环（本地替身，不调付费模型）— Done
- [x] tests/test_pbv_loop.py（6 场景）：正常两片循环、命令门禁失败（无审查直接 fail）、审查失败→修复→增量复验、修复耗尽（跨进程恢复）、Close 失败状态保持、attempt/isolation 台账。
- [x] 双门禁：内核循环测试断言命令+agent 双条目路径；空验证列表由 skill 审核清单禁止（pbv-mapping §4.8）。
- 证据：357 全绿；提交 e65dd3e。

## Stage 5：StockMate 验收与迁移（样本外窗口评估 case）— Done
- [x] 5.1 `orx init` + 路由（codex-frontier 规划只读 / orx-host 构建审查）+ Goal G001（8 硬约束 + 4 验收原文注入）。
- [x] 5.2 主计划：codex 只读真实调用产出 4 片线性链（自主发现国庆空窗并设计只读+副本隔离）；Controller 改写补 preread（修订 2，host-manual 如实记录）→ submit。
- [x] 5.3 四轮循环（round-9..12，编号续接旧序列）：每轮 轮计划→Build（GLM 子代理）→双门禁→Close（本地提交+DEVELOPMENT_PLAN 状态+轮报告）。Round-10 修复回环 1/2 闭环（独立审查发现 HIGH 级真实缺陷：晚可见基准跳日 → --reason 回传 → 一次修复 → 增量复验 pass）。Veto：用户在 Round-1 Build 前明确授权自动推进并记录于轮报告。
- [x] 5.4 门禁全绿（T003/T004 含全量 make verify；终局 344 passed）；用量 exact/unknown 如实（codex 505,602 in / 8,574 out exact；host unknown）；源库 sha256 前后一致、模型文件未动、零 Provider 调用。
- [x] 5.5 旧入口停用：`.zcode/skills/pbv-loop` → `.zcode/retired-skills/pbv-loop`（git mv，历史保留；旧 round-1..8 报告原样只读）；新入口与恢复方法文档 `docs/pbv-migration.md`。
- 证据：`orx status --json` run done / goal done（4/4 passed）；StockMate 提交 a0e6a87 / da6fce4 / 7df601c / 98d7004 / 7044c92（本地未 push）；轮报告 reports/pbv/round-9..12.md；审查证据 reports/g001/verify-t00*.md；首次真实执行产物 reports/g001/first/20261003T100000Z-dbc1abe9b80699/。

## 已知偏差与事故（如实）
- 轮编号事故：Controller 计划改写误用 round-1..4 新编号，覆盖旧 round-1-plan.md（不可恢复；权威旧报告完好）；已修复 skill 规则（编号 Round 0 对齐续接，提交 aae7e3c）并在 round-9.md 如实记录；T002-T004 内核 preread 仍指旧编号，构建契约行纠偏（pbv-mapping 已知限制同步）。
- 首验审查发现的 HIGH 缺陷正是双门禁设计的价值实证；每片修复预算 ≤2 未触上限（仅 Round-10 用 1 次）。

## 执行约束（摘自 Goal）
- 单片参考 ≤8 文件、≤3 模块；增量实现、先写行为测试；每次提交过适用检查；最终全量测试 + 构建检查。
- 本地提交不 push；不覆盖用户已有修改与其他任务计划文件。
- 构建与验证串行；每片初始 + ≤2 修复；阻塞停止保留可恢复状态。

## 轮次记录
- 2026-10-03 R1：Stage 1 冻结（mapping 文档）；Stage 2 并发开工（GLM：dispatch/plan/verify 切片 A；子代理 B：codex 沙箱+迁移；子代理 C：skill 可选安装；子代理 D：orx-pbv skill 草稿）。
- 2026-10-03 R2：Stage 2+3+4 完成并提交（8a712f7、e65dd3e）；357 tests 绿；orx-pbv 已装入用户级目录；Stage 5 待用户确认否决点策略后启动。
- 2026-10-03/04 R3：Stage 5 完成轮编号事故修复（aae7e3c）后全程执行：G001 四轮双门禁循环至 run done（round-9..12；修复回环 1 次闭环）；StockMate 5 个本地提交；旧入口停用 + 迁移文档；ORX 全量 357 tests 终验绿。**Goal 完成。**
- 2026-10-04 修订：skills 职责重划——orx-controller 成为通用执行入口（直接目标 / 外部咨询移交两种接纳，咨询建议与待验证假设不自动成约束）；orx-pbv 改为仅显式调用，删除范围外直接修复例外（范围变化走 replan 或用户决定），修复预算按 ORX 执行记录恢复，轮计划定位为不改生效任务的辅助产物。映射基线相应增补，见 docs/pbv-mapping.md 2026-10-04 修订段。

## 恢复与再次使用（Controller 换会话续跑指南）
- 新开发 Goal：宿主加载 `orx-pbv` skill，按其 SKILL.md 走 Round 0（`orx init`/doctor/约束提取/编号对齐 reports/pbv 旧序列）→ `orx goal new` → `orx plan`；每轮 Build 前按授权策略处理否决点。
- 中断恢复：`orx status --json` + 最近 round-N.md + `git log` 三步定位（详见 skills/orx-pbv/references/migration.md 与 StockMate docs/pbv-migration.md）。
- 定期评估重跑：`uv run stockmate periodic-evaluate --output-dir reports/periodic`（窗口自 2026-10-08 积累；同输入幂等、新截止独立运行）。

---

# G004：重规划差异预检与成果引用（2026-10-05 起；2026-10-05 归档）

Goal：新计划修订生效前，产出新旧任务对应关系与工作分类（已完成只需确认仍有效、
新增工作、确实需要重做＋重做原因），成果按可追溯的对应关系引用而非按任务编号。

**归档状态（T005 终审时记录）**：五阶段全部交付（阶段 3 的 `orx status` 呈现为
显式遗留开放项，未做、未声称）。契约权威文档 `docs/replan-contract.md`；端到端
回归 `tests/test_replan_e2e.py`（本地替身，无付费模型）；README 与两个 skill
已同步真实流程并由测试钉住。G004 的临时计划部分（五阶段待办清单）已并入下方
归档记录；本文件更早的 orx-pbv 历史原样保留。

硬约束（约束原文为据）：
- 不做旧 passed 状态自动继承：R003 复盘中"12M token 浪费在重做"的证据不成立
  （不同计划修订中的同号任务被误判为同一工作），不据此开发自动继承功能。
- M0/M1/M1.2 不变量保持：exit codes 0/1/2、`--json` envelope、additive
  integer-versioned migrations with backup-replace-restore。
- 契约：`docs/replan-contract.md`（权威字段与结构/语义边界）。

## 五阶段实施记录（归档）

状态图例沿用：Not Started / In Progress / Done（以证据为准）。

### 阶段 1：正式契约与纯校验 — Done（T001，R004）
- [x] 行为测试先行（tests/test_plan_ir.py 新增 37 例：分类/来源/去向/重做原因/
      当前验证/双向一致性/同号不自动对应/拆分合并部分对应/round-trip/提示词）。
- [x] `records.py`：`ReplanClassification`（new|confirm|redo|continue）、
      `SupersededDisposition`（confirmed|continued|redone|split|merged|dropped）。
- [x] `plan.py`：`ReplanMapping`/`ReplanTaskMapping`/`ReplanSource`/`ReplanSuperseded`
      模型；`validate_ir` 结构内检 + `validate_replan(ir, prior_tasks)` 纯外检；
      `PLAN_IR_SCHEMA` 与 planner 提示词同步（首计划 `"replan": null`）。
- [x] `docs/replan-contract.md`：字段、四种对应形态、旧任务去向、结构检查 vs
      语义审查边界、R003 说法不作事实依据的声明。
- [x] `to_dict()` exclude_none：首计划输出形状不变；历史 IR 照常可读。
- 证据：`uv run pytest -q tests/test_plan_ir.py` 74 绿；全量 561 绿（524 存量 + 37 新增）。

### 阶段 2：dispatch 接线 — Done（T003，R004）
- [x] 共用差异预检 `dispatch._replan_precheck`：`validate_ir` 结构内检 +
      `validate_replan`（以 `replan_snapshot` 任务行）外检 + prior_revision 必须
      等于当前 active 修订 + 成果绑定检查（命名了本 Run 已记录 evidence 的
      artifact 必须属于已声明来源；错绑定位到任务拒绝，未记录路径留给语义审查）。
- [x] 新命令 `orx plan check --file`（只读）：不创建修订、不取消旧任务、不改
      Goal/Run/规划指派状态；仅落一条 `replan_reports` 审计行（revision NULL）。
      报告双形态（--json envelope + 可读文本）：新旧对应（含重编号）、四类分类、
      重做原因、契约差异（将取消/终态保留/验收覆盖）、旧任务去向、来源状态核对、
      引用问题；失败含分类（category/locus/message）与具体错误，exit 0/1/2 保持。
- [x] 手工 submit 与 CLI planner 自动提交同一门禁：`submit_plan` 单入口生效前
      重新预检（来源状态变化重新判定）；失败抛 `plan.ReplanCheckFailed`
      （PlanValidationError 子类，携带分类报告），原计划继续有效、规划指派与
      planner attempt 原样保留、失败报告落库审计。
- [x] 原子生效：作废旧修订+取消未终态旧任务+新修订任务+`replan_mapping_save`
      +报告绑定+关闭规划指派在单事务内，任一故障整体回滚（无部分生效修订）。
- [x] 修复提前重开：路由重规划不再立即置 planning/active；重开只在新修订落地时
      发生，失败重规划不丢 completed_at。
- [x] 既有重规划夹具显式声明关系（conftest `with_replan` 等字面量助手）；范围外
      夹具最小机械修正并披露（test_replacement_boundary / test_verify /
      test_verify_check / test_subagent_contract / test_observability /
      test_m12_acceptance）。
- 证据：`uv run pytest -q tests/test_revisions.py tests/test_replan_context.py
  tests/test_cli.py tests/test_handoff.py` 100 绿；全量 583 绿（10 失败修复后）。

### 阶段 3：持久化与呈现 — 落库部分 Done（T002，R004）；呈现为遗留开放项
- [x] 存储接口 + 映射/预检报告/可追溯成果来源落库（schema v9 additive 整数版本迁移：
      `replan_mappings`/`replan_task_mappings`/`replan_sources`/`replan_superseded`/
      `replan_reports`/`replan_artifact_sources` 六表；backup-replace-restore 保持；
      v8→v9 失败原库可用、WAL 已提交记录不丢；旧库升级后新表为空，历史缺失保持
      unknown，无回填、无 passed 继承）。
- [x] 多轮追溯与身份规则：来源以 (run, 修订, 任务) 解析并锚定任务行；成果出处保留
      Run/修订/来源任务/attempt/evidence/成果身份，同号任务不同修订或不同 attempt
      不混淆（`replan_trace_chain` 跨轮、拆分、合并、重编号、重新打开可查询）。
- [x] 观测读取契约同步 v9：gate 只接受明确支持的版本（8/9 白名单），既有查询不变，
      新增 replan_correspondence/replan_superseded/replan_artifact_provenance 三查询
      + 旧库构造夹具（seed.sql v9 子集）。
- [ ] `orx status` / 快照呈现新旧对应关系与分类。

### 阶段 4：成果按对应关系引用 — Done（T004，R004）
- [x] worker/verifier 提示词经 `artifacts` 边引用旧成果，不按任务编号
      （`dispatch._replan_reference_context`：分类含义、来源全身份+记录状态、
      来源 evidence 行号/attempt 号、每个成果的解析身份与存在性/变化状态；
      confirm 仅确认适用性+必要回归、redo 展示原因与范围、verifier 端显式标注
      recorded HISTORY 不得冒充当前验证）。
- [x] 交付时保存绑定 attempt 的成果来源与交付快照：`_record_replan_delivery`
      仅在门禁接受的交付上运行——每个解析成果经 `replan_artifact_source_add`
      记录来源任务自身 attempt/evidence 身份；交付快照
      （`deliveries/Txxx-rRR-aAAA.json`，修订+attempt 寻址）记录所有声明成果的
      存在性与 sha256，作为 evidence 行挂在完成 attempt 上；缺失/变化不默认有效
      （UNCHANGED/CHANGED/MISSING 如实展示，历史出处保留）。
- [x] 修复同号任务跨修订的 check 日志覆盖：`orx task check` 日志序列按
      (run, 任务号) 跨修订续数（`Store.verifications_for_task_in_run`），修订内
      布局不变、既有路径保持可读；交付快照按修订+attempt 寻址互不覆盖。
- [x] planner 端贯通：事实快照 evidence 携带行号/attempt 身份并渲染进提示词，
      规划规则要求经对应关系在 artifacts 里引用 evidence 路径。
- [x] 不变量保持：引用与旧通过记录不写 verification 结果（新任务 pending/
      runnable 起步、当前窗口为空、只经自己的门禁+独立 verifier 通过）；G003
      门禁/同 attempt 检查/交付前重跑/独立验证不变；无 schema 版本变化
      （state 仅增读接口）。
- 证据：`uv run pytest -q tests/test_assignments.py tests/test_replan_context.py
  tests/test_verify_check.py tests/test_verify.py tests/test_delivery_gate_e2e.py`
  70 绿（61 存量 + 9 新增）；全量 592 绿。

### 阶段 5：端到端回归与终审 — Done（T005，R004）
- [x] `tests/test_replan_e2e.py`（本地替身，无付费模型）：一次修订切换同时断言
      差异报告、持久化引用链、原子修订切换与新任务独立验证；场景覆盖重编号确认、
      同号不同工作（编号不构成对应，trace 为空）、新增与未完成延续、拆分（part
      边）、合并（多来源）、明确理由的重做（报告与提示词逐字）、多轮成果追溯
      （三轮 + 跨 prior_revision 回溯来源、每边独立成果出处与交付快照互不覆盖）、
      失败预检后旧计划继续执行至 done、确认任务当前回归红则不能通过（旧 pass
      不背书新任务；修复后经自己的检查通过）。
- [x] README 与 orx-agent / orx-controller skill 同步真实流程：生效前预检
      （`orx plan check --file`，submit 重跑同一门禁）、成果按对应关系出处引用
      （绝不按任务编号）、具体重做理由；显式声明历史未知与语义审查边界；
      不宣传 passed 自动继承、不宣传未经证实的 token 节省（R003 数字已撤回）。
      文档由 `test_docs_teach_the_real_replan_flow_and_its_boundaries` 钉住。
- [x] 全量回归绿（见下方轮次记录证据行）；G004 记录归档，临时计划部分并入本节，
      更早历史文档保留。
- 证据：`uv run pytest -q tests/test_replan_e2e.py` 5 绿；全量 597 绿。

## G004 轮次记录
- 2026-10-05 R4 T001：阶段 1 完成并自验（见上证据行）。
- 2026-10-05 R4 T002：阶段 3 落库部分完成——schema v9 additive 迁移 + `Store.replan_*`
  存储接口（对应关系/预检报告/成果出处）+ 多轮追溯 + 观测契约 v9 同步；
  `uv run pytest -q tests/test_state.py tests/test_subagent_contract.py
  tests/test_m12_acceptance.py tests/test_observability_contract.py` 56 绿。
- 2026-10-05 R4 T003：阶段 2 完成——共用差异预检（`_replan_precheck`）+ 原子生效
  单事务 + `orx plan check --file` 只读命令 + 手工 submit/CLI planner 同一门禁 +
  已完成 Run 失败重规划不再提前重开 + 夹具显式声明关系（见阶段 2 证据行）。
- 2026-10-05 R4 T004：阶段 4 完成——引用链解析进 worker/verifier 提示词（存在性/
  变化以交付快照为基线，缺失不默认有效、历史出处保留）+ 交付时绑定 attempt 的
  成果出处与交付快照落库（同号跨修订互不覆盖）+ 同号跨修订 check 日志续序修复 +
  planner 事实快照携带 evidence 身份（见阶段 4 证据行）。
- 2026-10-05 R4 T005：阶段 5 完成——`tests/test_replan_e2e.py` 五场景端到端
  （四维同时断言：差异报告/持久化引用链/原子切换/新任务独立验证，全部本地替身）；
  README 与两个 skill 同步真实重规划流程并声明历史未知与语义审查边界（测试钉住）；
  `docs/replan-contract.md` 阶段 5 状态更新；G004 记录归档（临时计划并入，
  `orx status` 呈现为显式遗留开放项）。全量 597 绿（592 存量 + 5 新增）。

---

# G006：host worker 进展报告与安全恢复观测（2026-10-06 提交）

目标及计划：[docs/host-progress-plan.md](docs/host-progress-plan.md)。
权威任务图：[docs/host-progress-plan.json](docs/host-progress-plan.json)。
Run：R006；五项任务串行，参考工作量 3–5 小时，按验收完成。
实施已完成，本节为归档记录（证据为准，见下）；实测报告：
[docs/host-progress-report.md](docs/host-progress-report.md)。

## 归档状态（T005 交付时记录）

五阶段全部交付，实测报告见 [docs/host-progress-report.md](docs/host-progress-report.md)
（只记录实际运行的命令与结果）。权威契约 `docs/host-progress-contract.md`（T001 冻结）；
端到端替身回归 `tests/test_host_progress_e2e.py`（冻结时钟，无付费模型、无真实 sleep）。
T005 的独立 agent 审查与 Goal 验收以 ORX verification 记录为准，此处不预支结论。

### 五阶段实施记录（归档）

- [x] Stage 1（T001，3a03e15）：`tests/test_session_identity.py` 导入恢复仓库惯例；
      契约冻结（命令/门禁/响应/存储/观察/阈值/无副作用/恢复边界/禁止宣称）。
      证据：全量 627 绿（`evidence-T001.json`）。
- [x] Stage 2（T002，4bd0c4b）：schema v10 additive `attempt_progress` +
      Store 三接口（add/latest/all）+ 只读观测契约 v10 同步；重开一致、迁移失败
      原库可用、WAL 保留、旧库不回填、按 attempt 隔离。证据：指定检查 exit 0
      （`evidence-T002.json`；提交记录 64 项指定测试绿）。
- [x] Stage 3（T003，979b837）：`orx task heartbeat` CLI + `dispatch.task_heartbeat`
      写入接口；输入校验 exit 2、七条所有权门禁单事务重核、全部拒绝无副作用。
      证据：指定检查 exit 0（`evidence-T003.json`；提交记录 107 项指定测试绿、
      全量 646+1）。
- [x] Stage 4（T004，18fef91）：`worker.progress_timeout_min` 分层配置（默认 60）+
      `progress_observation`（当前窗口/闭边界/时钟异常不误报）接入 status/task
      list/recovery 与 timeline `attempt.report` 历史。证据：指定检查 exit 0
      （`evidence-T004.json`；提交记录 164 项指定测试绿、全量 662）。
- [x] Stage 5（T005，本次）：端到端替身复演 §10 两场景（超时提示→恢复重浮现→
      原 attempt 合法迟到交付受理；确认死亡 fail/retry→新窗口→旧 attempt 迟到
      报告 `attempt_closed` 与迟到交付 `stale` 双拒）+ 文档钉 4 测试；
      README/skills 补显式报告时机、有界等待与先查原会话规则（不越 §11 红线）；
      技能安装同步仅经 `orx skill update` 现有入口。证据：全量 668 绿（662+6）、
      `uv build` exit 0（`.orx/runs/R006/check/T005/`）；实测报告
      `docs/host-progress-report.md`。

## G006 轮次记录
- 2026-10-06 R6 T001→T005 串行五任务全部交付；详细实测与命令日志见
  `docs/host-progress-report.md` 与 `.orx/runs/R006/check/T00*/`。
- 未宣称（契约 §11）：定时自动心跳、自动恢复、存活证明、租约、真实时长或
  token 节省数字一律不写；超时提示只建议核查原会话，死亡确认是人的决定。

---

# G007：ORX 0.3.1 发布收尾（2026-10-06 起）

目标：补齐三个必须缺口（wheel 缺 agents force-include、角色定义示范旧
evidence、controller 技能硬编码本机 analytics 路径），补升级与回退说明与
安装包级验收，更新版本号与发布材料后正式发布 v0.3.1，并更新本地安装与
技能。Run：R007；五任务串行（T001→T005），发布渠道沿用 GitHub
aderan/orx push + tag v0.3.1；本地 editable 安装更新走 `orx skill update`。

范围外（明确延后，不纳入本 Goal）：状态页对应关系呈现、后台自动恢复、
并行 worktree 均留后续版本。

## 五阶段实施记录

状态图例沿用：Not Started / In Progress / Done（以证据为准，不以文件
存在为准）。

### 阶段 1：打包补齐 + 角色定义统一 + 包级验收地基 — Done（T001，R7）
- [x] `pyproject.toml`：`[tool.hatch.build.targets.wheel.force-include]`
      增加 `agents = "orx/agents"`（与 skills 同法）；干净安装不再报
      `install manually`。
- [x] `agents/orx-worker.md` TASK 段统一到现行单 assignment 协议：新版
      evidence 示例（status/summary/checks[{command,exit_code,log}]/
      artifacts）、START GATE、BLOCKED EXIT、同会话 CHECK-FIX LOOP、
      DELIVERY GATE、claim/attempt 身份（`--attempt`）、heartbeat 报告
      规则、ORX_ASSIGNMENT 身份锚；旧 `{summary,commands,artifacts}`
      示例删除并声明其被拒。
- [x] `agents/orx-verifier.md` / `orx-verifier-strong.md` 核对只读独立
      判断与两行 verdict 契约（与 orx-agent 技能、dispatch verifier
      prompt 一致），补身份锚段落。
- [x] 示范 JSON 真实校验：`tests/test_delivery_gate_e2e.py` 从
      worker 定义中提取示例 JSON，经 `verify.load_delivery_evidence`
      零错误接受，并在临时 stand-in task 中填充真实 check 行后通过
      交付门禁；角色/技能示例同 schema 钉住。
- [x] clean-wheel 回归：`tests/test_phase_c.py` 钉 hatch force-include
      配置；`tests/test_package_acceptance.py` 构建真实 wheel、临时
      venv 安装、仓库外运行 probe，证明 orx 导入位置在临时
      site-packages、三个角色随包分发且 preset 安装内容与包内逐字节
      一致、无 `install manually` 提示、preset 保留已有文件回归通过。
- [x] `scripts/check_release.py`：构建、临时安装、包内资源与
      preset/skills 检查四段（可独立运行，`main()` 输出 JSON 报告、
      发现问题退出非零），由包验收测试调用。
- [x] `docs/upgrade-0.3.1.md` 起草：旧角色备份（cp 带 .bak-0.3.0）、
      diff 比较、显式替换（mv + `orx preset install zcode` 或手工合并
      后放回）与 preset 保留语义、新版 evidence 示例；数据库迁移与
      回退章节留给阶段 3。
- 证据：`uv run pytest -q tests/test_phase_c.py tests/test_skills_update.py
  tests/test_delivery_gate_e2e.py tests/test_package_acceptance.py` 绿；
  全量 `uv run pytest -q` 绿；`uv build` exit 0；`git diff --check` 干净。

### 阶段 2：controller 看护接入显式配置化 — Done（T002，R7）
- [x] analytics 看护改为明确配置、可选执行的外部调用：可执行文件来源、
      start/status/stop 条件、未安装与启动失败处理、仅清理由当前会话
      启动的看护；移除把本机 analytics 目录表述为固定位置的文字（历史
      测量中的真实路径保留为历史证据）；技能约束回归。
      — skills/orx-controller/SKILL.md 循环步骤 1 重写：可执行文件仅经
      `ORX_ANALYTICS_BIN` 显式解析（无默认位置、不探测），OPTIONAL 且
      不阻断循环（规划/执行/验证/收尾 never blocked），启动失败记一次
      继续，`watch stop` 仅限本会话启动的看护；分析实现维持独立仓库、
      ORX 不内置分析层、看护只观察不自动恢复。docs/observability-contract.md
      去除固定本机路径表述（保留独立仓库声明与 orx-analytics 名称，
      历史路径声明为 history, not defaults）。
      tests/test_delivery_gate_e2e.py 新增 2 项技能/契约约束回归
      （空白归一后钉住措辞，防止换行规避）。
- 证据：`uv run pytest -q tests/test_delivery_gate_e2e.py
  tests/test_observability_contract.py` 24 绿（22 存量 + 2 新增）；
  全量 `uv run pytest -q` 688 绿；`git diff --check` 干净。

### 阶段 3：升级与回退文档完成 — Done（T003，R7；角色部分 T001 起草）
- [x] `docs/upgrade-0.3.1.md` 角色文件备份/比较/显式替换与 preset 保留
      语义、新版 evidence 示例（T001 起草，待 T003 复核）。
- [x] 从 README 链接；补齐暂停写作者、SQLite 一致性备份、schema v8→v11
      自动迁移、旧版本拒读新版库、回退必须恢复升级前备份；分渠道更新
      步骤；`replan --context-file` 要求；README 观测版本改 v8/v9/v10/v11
      （保留历史契约版本语境）。
      — `docs/upgrade-0.3.1.md` §2 分渠道步骤（editable：git checkout
      v0.3.1、明确不做 `uv tool upgrade` 并引用 update.py 的拒绝对原文；
      wheel：`orx update`/`uv tool upgrade orx-agent`）；§6 备份与回退
      （新版首次打开旧库前暂停写作者并备份；`VACUUM INTO`/`.backup`
      一致性快照含 WAL 已提交帧，明示 cp 主文件会漏；连同
      config/profiles 与 `.orx/runs/` 运行材料；v8→v9→v10→v11 纯增量
      自动迁移、复制-替换失败保原库；旧版本拒读原文
      "newer than supported version 8"；回退=恢复备份，never hand-edit
      schema_version，恢复时移除残留 -wal/-shm）；§7 `replan
      --context-file`（可读非空、64 KiB/65536 字节、reason/intent/
      supporting material、分发前校验、代码层可选 vs controller 中途
      重规划先写 context 文件的流程要求，分开表述）。README：Usage 观测
      表述改 v8/v9/v10/v11（gate 恰收 8/9/10/11，保留 v8 立契约、
      v9/v10/v11 增量修订的历史语境）、Skills/update 段升级提示、Docs
      列表链接。tests/test_delivery_gate_e2e.py +5、
      tests/test_replan_context.py +2 文档钉住回归（空白归一，备份先于
      首次打开的顺序、WAL 一致备份、恢复式回退、editable 不走
      uv tool upgrade、skill 与角色文件两入口分开、文档 §3.4 evidence
      示例经真实校验器接受、context-file 限额与报错原文对齐代码常量）。

### 阶段 4：安装包级验收固化（完整链路） — Not Started（T004）
- [ ] 扩展 `scripts/check_release.py` 与包验收测试为完整可重复链路：
      构建最终源码 wheel、临时环境安装、agents/skills/preset 资源检查、
      preset 与全部打包技能安装、已有角色保留与技能刷新验证、仓库外
      stand-in Goal 全流程（规划/执行/新版 evidence 交付/独立 verdict）；
      真实 v0.3.0 schema v8 样例库副本升级到 v11 的逐行核对与旧版本
      拒读验证；不允许 skip 或只验证源码回退。

### 阶段 5：版本、发布与本地更新 — Not Started（T005）
- [ ] 版本单一来源升 0.3.1；四成果发布说明与三个延后事项；README 观测
      表述更新；docs/m1.2-report.md 测量截止时间戳刷新随发布提交；
      全量测试 + 构建 + 包级验收 + 独立审查后 push aderan/orx 并建
      tag v0.3.1（不强推不覆盖）；本地 editable 确认 + `orx skill
      update` 实跑 + 按升级说明备份/比较/更新本地旧角色定义；归档本
      阶段记录。

## G007 轮次记录
- 2026-10-06 R7 T001：阶段 1 交付（attempt 115）——打包、角色统一、
      示范 JSON 真实校验、clean-wheel 回归、check_release 四段、
      升级文档角色章节起草；证据见 `.orx/runs/R007/check/T001/`。
- 2026-10-06 R7 T002：阶段 2 交付（attempt 117）——controller 技能
      看护接入显式配置化（ORX_ANALYTICS_BIN、OPTIONAL、跳过/启动失败/
      清理边界）+ 观测契约去除固定本机路径表述 + 技能约束回归 2 项；
      证据见 `.orx/runs/R007/check/T002/`。
- 2026-10-06 R7 T003：阶段 3 交付（attempt 119）——升级/回退文档完成
      并 README 链接（分渠道步骤、备份先于首次打开、WAL 一致备份、
      v8→v11 自动迁移、旧版拒读、恢复式回退、replan context-file 两层
      要求；README 观测版本改 v8/v9/v10/v11）；文档钉住回归 7 项；
      证据见 `.orx/runs/R007/check/T003/`。

