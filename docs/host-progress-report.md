# G006 实测报告：host worker 进展报告与安全恢复观测（Run R006）

日期：2026-10-06。本报告只记录实际运行的命令与真实结果（命令、退出码、
计数、时间戳均为实测；未实测的行为一律不写）。权威契约：
[host-progress-contract.md](host-progress-contract.md)（T001 冻结）。

## 方法声明（替身，非真实执行）

- 端到端验收位于 `tests/test_host_progress_e2e.py`：**本地替身**——dispatch
  调用同时扮演 worker 与 Controller；ORX 可注入时钟缝
  （`orx.state.now` / `orx.dispatch.db_now`）冻结在固定时刻，"超时"是时钟
  算术（如 age 90s ≥ threshold 60s），**没有真实 sleep、没有等待、没有
  付费模型调用**。临时项目在 tmp 目录创建，不触本仓库 `.orx/state.db`。
- 全部"时长"只有测试命令自身的真实运行秒数（下表 in 49.10s 等）；任何
  报告年龄、恢复等待时长都是冻结时钟的设定值，不是测量值。

## T005 实测命令与结果

| # | 命令 | 结果 | 时间（UTC） |
| --- | --- | --- | --- |
| 1 | `orx task check T005`（START GATE） | 2 项命令检查通过；check rounds 1/3 | ~03:02–03:04 |
| 2 | ↳ `uv run pytest -q`（基线） | exit 0；**662 passed** in 45.58s；日志 `.orx/runs/R006/check/T005/01-0000-command.log` | 同上 |
| 3 | ↳ `uv build` | exit 0；built `orx_agent-0.3.0.tar.gz` + `orx_agent-0.3.0-py3-none-any.whl`；日志 `02-0001-command.log` | 同上 |
| 4 | `uv run pytest -q tests/test_host_progress_e2e.py -k scenario` | **2 passed**, 4 deselected（契约 §10 两场景对既有实现首次运行即绿） | 开发中 |
| 5 | `uv run pytest -q tests/test_host_progress_e2e.py -k "not scenario"` | **3 failed**, 1 passed（文档钉测试在 README/skills 补写之前，预期的 TDD 红） | 开发中 |
| 6 | `uv run pytest -q tests/test_host_progress_e2e.py` | **6 passed**（2 场景 + 4 文档钉） | 开发中 |
| 7 | `uv run pytest -q tests/test_delivery_gate_e2e.py tests/test_replan_e2e.py tests/test_skills_update.py tests/test_cli.py tests/test_phase_c.py tests/test_lifecycle.py tests/test_timeline.py tests/test_assignments.py` | **180 passed**（文档改动未破坏既有钉测试） | 开发中 |
| 8 | `uv run pytest -q`（终验） | exit 0；**668 passed** in 49.10s（662 基线 + 6 新增）；日志 `.orx/runs/R006/check/T005/pytest-full-t005.log` | 03:11:26 起 |
| 9 | `uv build`（终验） | exit 0；同 #3 两个产物；日志 `uv-build-t005.log` | 03:12:21 |
| 9b | `orx task check T005`（交付前对最终工作树复跑） | 两项命令检查 ok：`uv run pytest -q` **668 passed** in 45.93s、`uv build` 成功；日志 `01-0002-command.log` / `02-0003-command.log`；check rounds 2/3 | 03:15 |
| 10 | `orx skill update`（唯一使用的安装同步入口） | exit 0；refreshed `orx-controller`、`orx-agent`、`orx-pbv`；symlinked into `~/.zcode`、`~/.cursor`、`~/.codex`；安装副本已含新 heartbeat 指引（grep 计数 1/1；刷新副本 mtime 03:09:49Z） | 03:09 |

## 场景复演内容（契约 §10，替身断言）

场景 A（`test_scenario_a_overdue_hint_then_original_attempt_late_delivery`）：
claim（session 绑定）→ 两条显式报告（response 逐字段：sequence 1/2、
ORX 时钟 `received_at`）→ 当前窗口 `reported`（age 0 / timeout_sec 60）→
时钟 +90s 越过 1 分钟阈值 → `run_slice` recovery 重浮现：`overdue` + hint
（`>= 1m threshold` / `check the original worker session` /
`handle: sess-worker-a` / `before any fail/retry`）→ 观察零副作用
（任务仍 running、attempt 数与 task events 数不变）→ status / task list /
timeline current 三面同窗 → **原 attempt 合法迟到交付被受理**（passed，
run done）→ 交付后该 attempt 再报告被拒（reason `task_not_running`），
两条报告仍按 attempt 可查。

场景 B（`test_scenario_b_confirmed_death_fail_retry_rejects_old_attempt`）：
报告后时钟 +4000s 越界（hint 出现）→ 人工确认死亡：显式 `task fail` +
`task retry` → `run_slice` 重新 parked（host_required，非 recovery）→ 新
claim 得到**新 attempt** → 新窗口 `unknown`（旧报告不回流；status/timeline
current 同验）→ **旧 attempt 迟到报告被拒**（reason `attempt_closed`）→
新 attempt 报告被收（sequence 重新从 1 起，逐字段窗口断言）→ **旧 attempt
迟到交付被拒**（ConflictError `stale`，任务保持 running）→ 旧报告仅存
timeline 历史（detail 全身份 `a<old> #1`）→ 新 attempt 正常交付（passed，
run done），两 attempt 报告历史均可查。

文档钉（4 测试）：README 与两个 skills 教显式报告时机、有界等待与
"先核查原会话"；逐行红线检查——含 heartbeat 的行不得出现 token/节省，
`automatic heartbeat`/`keepalive`/`lease` 只允许否定式出现。

## 前序任务实测（R006 已记录事实，此处引用不复测）

| 任务 | 记录的检查结果 | 证据 |
| --- | --- | --- |
| T001 | `uv run pytest -q` exit 0，627 passed（当时基线） | `.orx/runs/R006/evidence-T001.json`、提交 3a03e15 |
| T002 | 指定测试命令 exit 0 | `.orx/runs/R006/evidence-T002.json`、提交 4bd0c4b（提交记录：64 项指定测试绿） |
| T003 | 指定测试命令 exit 0 | `.orx/runs/R006/evidence-T003.json`、提交 979b837（提交记录：107 项指定测试绿，全量 646+1） |
| T004 | 指定测试命令 exit 0 | `.orx/runs/R006/evidence-T004.json`、提交 18fef91（提交记录：164 项指定测试绿，全量 662） |

## 副作用披露

- 全量测试会再生 `docs/m1.2-report.md`（截止时间戳/快照字段刷新）；本轮
  再生文件保留原样，未手工编辑。
- `orx skill update` 刷新了用户级 canonical 副本（`~/.agents/skills`）与
  三个 symlink 目标——这是现有更新入口的既定行为，未新增任何安装机制。

## 未宣称清单（红线，契约 §11）

以下一律**不宣称**：定时/自动心跳或自动推送；存活或死亡证明；租约/
keepalive/抢占；验收依据；自动恢复（自动 fail/retry/重开/第二写入者）；
任何未实测的时长或 token 节省数字。超时提示只建议核查原会话；死亡确认
是人的决定。
