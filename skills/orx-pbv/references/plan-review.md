# PBV 主计划审核清单（Controller 用）

对 `orx plan` 产出的 Plan IR 逐项核对。任何一条不满足：把审核意见作为反馈
重跑规划（最多 2 次）；任务图结构性错误走 `orx replan`；仍不行 Controller
自己改写 Plan IR 并在轮报告注明"Controller 改写"。

## 切片结构

- [ ] 每个 task 独立可构建、可验证、可提交；id 为 T+数字，依赖只引用已存在 id。
- [ ] 线性依赖链：T002 `dependencies` = ["T001"]，T003 = ["T002"]……保证
      构建/验证串行，Close 前下一片不可运行（内核依赖机制同时兜底）。
- [ ] 切片宁小勿大：单片改动 ≤8 个文件、≤3 个模块、构建代理 ≤~60 次工具
      调用；"建块+训练+比较+文档"式的多合一切片必须拆（实测单代理可烧
      20M+ tokens）。

## 每 task 字段

- [ ] objective 引用项目任务规划文档条目（如 docs/DEVELOPMENT_PLAN.md 条目
      号），不另起炉灶。
- [ ] preread 已设置：项目相对路径、含测试文件、一次列全；**首项 = 轮计划
      文件路径** `reports/pbv/round-N-plan.md`（N = 该 task 的轮次；文件由
      每轮 Plan 步产生，Build 前落盘）。
- [ ] scope.allowed 只列项目相对路径（绝不绝对路径、不含 `..`），覆盖本片
      全部改动面。
- [ ] acceptance 含 Goal 验收原文的逐字拷贝（planner 契约要求；释义即拒）。
- [ ] routing（complexity / required_capabilities）与该切片难度相称，不强求
      全片一致。

## 每 task 验证（双门禁；空验证列表不满足 PBV 完成条件）

- [ ] ≥1 条项目最强命令门禁（开工前查 Makefile/scripts/CI 确认，StockMate
      为 `make verify`；不要凭习惯写）。
- [ ] ≥1 条 `agent:` 独立验收审查。
- [ ] 条目具体写法按 references/verification-entries.md。

## 仓库约束

- [ ] 计划内容不违反 Round 0 提取的仓库硬约束（约束已注入 Goal constraints，
      此处复查切片本身与约束无冲突，如数据目录只读、schema 迁移规程、凭据
      纪律等）。
