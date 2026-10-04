# PBV 轮报告模板

每轮 Close 时由 Controller 写 `reports/pbv/round-N.md`。字段如实填写，没有的
写"无"。轮报告是中断恢复的依据之一（见 references/migration.md），Close 完成
前不开工下一片。

```markdown
# PBV Round <N> — <task id 与标题>

- 时间：<本地日期时间>
- Goal：<一句话>
- 切片：<task id>，attempt 数：<初始 + 修复次数，如 1 或 3>

## Plan
- 计划文件：reports/pbv/round-N-plan.md（来源：<planner | Controller 抽取 | Controller 改写>）
- Controller 审核结论：<通过 / 修改了什么>

## Build
- profile：<该 attempt 实际使用的 profile；记录缺失写 unknown>
- 改动摘要：<文件清单 + 每个一句话>
- 聚焦测试：<跑了什么、结果>
- 偏离说明：<worker 对计划的偏离与实际做的最小正确改动；无则"无">

## Validate
- 命令门禁：<命令> 退出码 <0 / 非 0（附要点）>
- agent 审查：<pass / fail + 结论摘要>
- 复验范围：<首次全量 / 复验增量（上轮问题 + 修复触及文件）；增量仅在
  审查基线在案且 evidence 列出改动文件清单时使用，否则写"复验全量"；
  首次写"首次全量">

## Close
- 提交：<hash> <subject>（本地，未 push）
- 文档状态更新：<项目任务规划文档改了什么>

## 成本
- orx usage 摘要：<本轮 planner/worker/verifier 各段 token 与失败次数；
  缺失记 unknown，不当零；单段异常偏大附原因分析>

## 遗留与下一轮
- <未尽事项、风险、下一片是什么；循环结束时写总结与建议下一步>
```

补充纪律：

- attempt 数按 ORX 执行记录取（一个 task = 一轮，初始构建 = attempt 1，
  每次 `orx task retry` 后重建 +1），以 `orx status --json` 为准，与运行
  事实对不上时修正报告、不改记录。
- 范围外问题不在本轮修复（走 `orx replan` 增补任务或交用户决定，见
  SKILL.md §6）；在此如实记录发现与去向（归属、决定、去向）。
- Veto 情况注明：本轮是"用户逐轮确认"还是"已授权自动推进"（授权来源）。
