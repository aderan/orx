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

# G004：重规划差异预检与成果引用（2026-10-05 起）

Goal：新计划修订生效前，产出新旧任务对应关系与工作分类（已完成只需确认仍有效、
新增工作、确实需要重做＋重做原因），成果按可追溯的对应关系引用而非按任务编号。

硬约束（约束原文为据）：
- 不做旧 passed 状态自动继承：R003 复盘中"12M token 浪费在重做"的证据不成立
  （不同计划修订中的同号任务被误判为同一工作），不据此开发自动继承功能。
- M0/M1/M1.2 不变量保持：exit codes 0/1/2、`--json` envelope、additive
  integer-versioned migrations with backup-replace-restore。
- 契约：`docs/replan-contract.md`（权威字段与结构/语义边界）。

## 五阶段实施计划

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

### 阶段 2：dispatch 接线 — Not Started
- [ ] 重规划提交（存在先前修订）强制携带 `replan` 映射：`validate_replan` 以
      `replan_snapshot` 的任务行调用；缺失/不一致按 PlanValidationError 拒绝。
- [ ] 首次计划仍不要求映射；exit codes 0/1/2 与 `--json` envelope 不变量测试。

### 阶段 3：持久化与呈现 — Not Started
- [ ] 映射随修订落库（additive 整数版本迁移，backup-replace-restore 流程）。
- [ ] `orx status` / 快照呈现新旧对应关系与分类。

### 阶段 4：成果按对应关系引用 — Not Started
- [ ] worker/verifier 提示词经 `artifacts` 边引用旧成果，不按任务编号。
- [ ] 证据文件引用与对应关系挂钩的行为测试。

### 阶段 5：端到端 dogfood 与终审 — Not Started
- [ ] 真实重规划场景走通：确认/新增/重做/延续四类齐全，成果引用可追溯。
- [ ] 全量回归 + 契约文档终审（结构/语义边界仍清晰）。

## G004 轮次记录
- 2026-10-05 R4 T001：阶段 1 完成并自验（见上证据行）。
