# IMPLEMENTATION_PLAN — orx-pbv（替换 StockMate pbv-loop）

创建：2026-10-03。任务 Goal 原文由用户会话给出；替换契约见 `docs/pbv-mapping.md`（Stage 1 冻结基线）。
Stage 5 验收 case（用户已确认）：StockMate 新样本外窗口评估（2026-09-30 后自动积累窗口）。

状态图例：Not Started / In Progress / Done（以证据为准，不以文件存在为准）。

## Stage 1：冻结替换契约 — Done
- [x] `docs/pbv-mapping.md`：功能与验收映射、词汇/状态/场景映射、内核 API 契约、已知限制。
- 证据：mapping 文档逐条覆盖旧 skill/模板/脚本的正常、失败、复验、阻塞、收尾场景（§3 表）。

## Stage 2：补齐指派与验证契约（内核切片，行为测试先行）
- [ ] A1 preread 字段 + worker prompt（约束/preread/prior_failure）+ planner prompt 说明 + plan 校验与 schema — In Progress（GLM 主线）
- [ ] A2 verifier prompt 丰富（约束/acceptance/命令门禁结果/证据/prior issues）— In Progress（GLM 主线）
- [ ] A3 host/external 任务指派与 host verifier 指派落盘 + payload（prompt/prompt_file/preread/isolation）— In Progress（GLM 主线）
- [ ] A4 attempt.isolation wiring（dispatch 各 attempt_create 传 launch.sandbox / prompt_only；依赖 B 的 Launch.sandbox）— In Progress（GLM 主线）
- [ ] B codex 按角色沙箱（planner/verifier read-only）+ Launch.sandbox + attempts.isolation 迁移 v5 — In Progress（子代理）
- [ ] C skill 可选安装（`orx skill install [NAMES…]`、动态名称校验、update 只刷新已装）— In Progress（子代理）
- 验收：全量 `uv run pytest` 绿；先写行为测试再实现。

## Stage 3：实现 skill 与可选安装
- [ ] D `skills/orx-pbv/`（SKILL.md + references/：主计划审核清单、轮计划提示词模板、验证条目模板、轮报告模板、恢复与迁移）— In Progress（子代理草拟，GLM 终审定稿）
- [ ] 安装更新行为：无参默认不变、显式装 orx-pbv、update 只刷新已装（Stage 2-C 承载）。
- [ ] 构建产物包含 orx-pbv 资源（打包 force-include 覆盖 references/）。
- 验收：临时用户目录安装/重复安装/更新/未装更新/非法名称/无参行为测试绿。

## Stage 4：验证完整开发循环（本地替身，不调付费模型）
- [ ] tests/test_pbv_loop.py：正常两片循环、命令失败、审查失败、修复成功、修复耗尽（内核侧）、收尾前不解锁、中途重读状态。
- [ ] 双门禁：空验证列表不满足 PBV 完成（skill 层清单断言以文档契约为准；内核循环测试断言双条目路径）。
- 验收：`uv run pytest` 全绿。

## Stage 5：StockMate 验收与迁移（样本外窗口评估 case）
- [ ] 5.1 StockMate `orx init` + profiles（codex 规划只读、GLM 构建、Flash 审查）+ Goal 建立（约束注入）。
- [ ] 5.2 主计划（planner，小切片）→ Controller 审核 → plan submit。
- [ ] 5.3 每轮：轮计划 → 构建 → 双门禁验证 → Close（提交 + DEVELOPMENT_PLAN 状态 + 轮报告）；每轮 Build 前用户否决点（或已授权自动推进并记录）。
- [ ] 5.4 `make verify` 全绿；实际模型/用量核对（unknown 合法）；真实数据与生产流水线未动。
- [ ] 5.5 停用旧入口（pbv-loop 退出技能扫描，保留历史报告与 Git 历史）；新入口与恢复方法文档。
- 验收：ORX 状态与轮报告一致；以实际证据判断完成。

## 执行约束（摘自 Goal）
- 单片参考 ≤8 文件、≤3 模块；增量实现、先写行为测试；每次提交过适用检查；最终全量测试 + 构建检查。
- 本地提交不 push；不覆盖用户已有修改与其他任务计划文件。
- 构建与验证串行；每片初始 + ≤2 修复；阻塞停止保留可恢复状态。

## 轮次记录
- 2026-10-03 R1：Stage 1 冻结（mapping 文档）；Stage 2 并发开工（GLM：dispatch/plan/verify 切片 A；子代理 B：codex 沙箱+迁移；子代理 C：skill 可选安装；子代理 D：orx-pbv skill 草稿）。
