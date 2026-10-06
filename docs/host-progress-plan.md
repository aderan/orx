# 下一阶段：host worker 进展报告与安全恢复观测

日期：2026-10-06。ORX Goal `G006` / Run `R006`。
结构化计划：[host-progress-plan.json](host-progress-plan.json)；
接纳记录：[host-progress-intake.json](host-progress-intake.json)。

本次提交五项待执行任务，参考工作量 **3–5 小时**。这不是时长承诺，
也不是必须耗尽的预算；完成以各项验收和 ORX 状态为准。

## 为什么现在做

当前基线 `852cd0a` 已包含 G003 交付协议、G004 重规划预检及成果引用、
配额预检/退避和 host worker 会话身份绑定。下一缺口是：Controller 虽然
能找回正在执行的 attempt，却看不到 worker 最后一次报告的进展和时间。

已有机制作为本轮基础：

- claim/complete 的稳定 attempt 身份与迟到提交拒绝；
- `orx run recovery` 的原执行者优先、禁止第二写入者契约；
- additive 数据库迁移、历史保留和只读观测查询。

本阶段通过显式报告补齐这个缺口。**最近收到报告不等于证明进程仍然存活；
长时间无报告不等于证明进程死亡。** 超时只提示核查原会话。

## 提交时的基线

- 初始工作树干净，最近 ORX 运行 `R005` 为 `done`。
- `uv run python -m pytest -q`：**627 passed in 53.32s**。
- 标准 `uv run pytest -q`：收集失败，定位为
  `tests/test_session_identity.py` 的 `from tests.conftest` 导入；
  仓库其他测试使用 `from conftest`。修复列入 T001，不绕过或停用测试。

## 五项任务

| 任务 | 交付 | 验收重点 | 依赖 |
| --- | --- | --- | --- |
| T001 | 标准测试入口修复、进展报告契约 | 全量标准入口绿；冻结字段、阈值、所有权和错误规则 | 无 |
| T002 | attempt 绑定的追加进展存储、只读查询 | 历史不覆盖；重开仍可查；迁移失败原库可用；旧数据 unknown | T001 |
| T003 | `task heartbeat` 命令与写入门禁 | 拒绝过期/已关闭/错误身份；不改验收、状态、会话或用量 | T002 |
| T004 | 当前进展、超时提示、恢复与历史呈现 | status/list/recovery 不混入旧报告；timeline 可追溯；不自动重启 | T003 |
| T005 | 恢复闭环替身测试、协议文档同步 | 全量测试与构建绿；独立审查；有效迟到交付可接纳，旧 attempt 提交被拒绝 | T004 |

每项按结构化计划的范围和检查执行。先行为测试、再最小实现、再验收提交；
共享工作区串行写入。完整验收原文、必读文件和检查命令在 JSON 中。

## 边界与后续

本阶段不引入后台常驻 Controller、自动 fail/retry/replan、租约抢占、
并行 worktree、状态 UI 或跨供应商成本换算。没有报告就保留未知，
不从 claim 时间、供应商会话或 token 消耗推测存活。

worker 技能将说明关键阶段前后显式报告；Controller 技能将说明有界等待
与先核查原会话。显式报告接口不能宣传成自动定时心跳或自动通知。

G004 遗留的 `orx status` 重规划对应关系呈现仍是独立开放项，不混入本阶段。

执行状态以 `orx status --json` 为准；提交计划不代表任务已实施。

## 提交结果

计划预检零错误，已通过规划指派 P007 提交为 R006 revision 1。
ORX 状态：T001 `runnable`；T002–T005 `pending`，按依赖串行。
Run 的聚合状态为 `running`，表示计划具备可执行任务；本次未 claim 或启动 worker。
