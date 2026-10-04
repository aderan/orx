# 从项目级 pbv-loop 迁移与恢复

完整替换映射（旧机制 → ORX 承载）见 ORX 仓库 `docs/pbv-mapping.md`；本文只写
操作。

## 安装与更新

- 安装：`orx skill install orx-pbv`（显式名称，只装本 skill）。
- 更新：`orx skill update`（只刷新已安装且仍打包的 skill）。

## 迁移步骤

1. 新 Goal 建立在 ORX 上：按 SKILL.md Round 0 → Goal 建立 → 主计划。旧 skill
   的 CONSTRAINTS 提取与验收原文迁移进 `orx goal new` 的 `--constraint` /
   `--acceptance`（内核负责注入三类 prompt，不再需要提示词槽位）。
2. 旧 `reports/pbv/` 历史报告与计划保留只读：绝不删除、不改写。新轮报告
   继续写同一目录，round-N 编号接续旧序列（开工时定一次并在首份轮报告注明
   编号起点）。
3. 验收 case 通过后，把旧 `.zcode/skills/pbv-loop` 移出技能扫描范围：重命名
   目录如 `pbv-loop.retired/`（保留 Git 历史），绝不删除历史报告；向用户说明
   新入口（orx-pbv）与恢复方法。

## 中断恢复（新会话）

通用接管与恢复协议（`orx run` 重新停泊、recovery 条目、no-second-writer、
late result 绑原 attempt、stale 拒绝）全部遵循 orx-controller skill；本节
只补 PBV 特有的三步定位：

1. `orx status --json`：run/goal/task 状态是唯一事实源。
2. 读最近一份 `reports/pbv/round-N.md`：上一轮走到哪步（Build / Validate /
   Close）。
3. `git log` 对照：确认 Close 是否完成（提交是否存在、规划文档是否更新）。

然后续跑：Close 未完 → 补 Close（提交、文档状态、轮报告）；Close 已完 →
下一轮 Select。修复预算按 ORX 执行记录恢复（该 task 的 attempt 计数，以
`orx status --json` / `orx timeline` 为准），不重新起算；轮报告只是辅助
留痕，与运行事实不一致时以运行事实为准。task 处于 failed 时先
`orx task retry` 再重建。

## 旧机制 → 新承载对照（摘要）

| 旧 pbv-loop | orx-pbv |
| --- | --- |
| goal + CONSTRAINTS 注入提示词槽位 | `orx goal new` --constraint/--acceptance，内核注入 planner/worker/verifier prompt |
| codex 主计划（codex_plan.sh 只读沙箱） | `orx plan` / `orx replan` 产 Plan IR（codex CLI 驱动只读沙箱） |
| round-N-plan.md 自包含轮计划 | 同名项目文件，作为该 task preread 首项传入构建 |
| 构建子代理 + build 模板 | `orx run` 停泊 → `orx task claim` → 子代理跑内核 worker prompt |
| CreateWorkflow（make verify + Flash 审查） | `orx verify`：命令门禁 + agent 审查；`orx verify submit` 交裁决 |
| __RECHECK__ 范围限定复验 | 验证条目文本自带复验语义 + 内核注入 prior issues |
| 修复回环 ≤2 次/轮 | `orx task retry`；内核无上限，上限由 Controller 执行 |
| Close（终审、提交、轮报告、文档状态） | skill 层 Controller 动作，不变 |
| 每轮 CreateWorkflow 用户确认 | 每轮 Build 前否决点，或 Goal 开始时授权自动推进并记录 |
| 轮报告成本段（.codex.log/agent usage） | `orx usage` / `orx timeline`，缺失记 unknown |
