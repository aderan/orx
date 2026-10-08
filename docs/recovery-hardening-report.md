# 恢复与成本治理实施报告（G009）

本报告是 docs/recovery-execution-goal-2026-10-07.md 定义的实施报告（"证据、暂停点与最终交付"）。
每个任务记录：输入commit、红信号、实现选择、绿验证、真实/模拟来源、
attempt/session/nonce、配置指纹、用量来源、缺失字段、耗时区间与提交。
逐任务追加；Goal 终验（T012）时按八条验收汇总证据表。

---

## T007 · 增加执行能力预检与验证结果的配置指纹

**输入commit**：`222872f`（T006 交付，2026-10-08）。
**attempt/session/nonce**：attempt 153（本条目写作时任务仍在 running；
session `sess_subagent_agent_7a8d97f2-bbc7-4094-9ab7-60fdaf7189bf`，
nonce 即 ORX_ASSIGNMENT `7cbc0637-9ecc-4436-8335-3f931f183845`）。

### 红信号（先写行为回归，新增用例全部失败后转绿）

新增 16 例测试先红：`probes.build_permission_preflight` /
adapter `permission_argv_axes` 镜像 / `orx agent preflight` CLI 均不存在。

### 实现选择

- **读真实配置，五轴分立**：cursor `~/.cursor/cli-config.json`
  （approvalMode、sandbox.mode、permissions.allow/deny）；codex
  `CODEX_HOME`/`~/.codex`（config.toml 的 approval_policy 与
  projects.<ws>.trust_level，hooks.json 事件名）。trust / force|yolo /
  approval_mode / sandbox / project_hooks 各自取值、各自来源；
  缺文件或缺键 = unknown，绝不默认允许/拒绝。
- **argv 轴来自 adapter 镜像**：`permission_argv_axes` 放在
  cursor/codex adapter 内（真相之源），test_adapters 把镜像与真实
  launch argv 逐旗标对账。
- **明示证明范围**：`proves` = CLI 身份+版本+轴值+来源；
  `cannot_prove` = agent 内部 shell 执行（控制器侧 PATH/denylist/help
  都不是 in-agent 审批）+ 绝不清除额度 cooldown/exhaustion/人工
  override（T005 语义；实现零健康写入，测试钉 exhausted+naive reset+
  override=1 在预检后不变）。
- **指纹绑定与失效**：sha256 绑 CLI 二进制+版本 / profile / model /
  effort / role / 五轴值+来源 / workspace / --check 首 token（控制器
  PATH 命中显式标注证明力为零）/ 各源 axes 内容摘要；probed_at 随
  记录不入哈希（同环境可复现）。失效 = 指纹变化或任一源 mtime 变化
  （touch 同内容也退役先前结果）；持久化
  `<data>/probes/preflight/<harness>-<profile>.json`。
- **秘密边界**：cursor authInfo/账号缓存根本不打开；codex hooks 只存
  事件名；测试逐串断言 email/userId/authId/authCacheKey/modelParameters
  不出现在落盘内容。

### 绿验证

- 门禁：`.venv/bin/python -m pytest -q tests/test_probes.py
  tests/test_adapters.py` → 90 passed（attempt 153；check log
  `.orx/runs/R009/check/T007/`）。
- 全量：`.venv/bin/python -m pytest -q` → 1000 passed、0 xfail
  （基线 984 + 新增 16）；docs/m1.2-report.md 重跑后按约定还原。
- 真机只读冒烟（零付费调用、零写入 harness 配置）：
  `orx agent preflight cursor --profile cursor-strong` 读出真实
  allowlist/Shell(ls)/sandbox disabled/--trust(adapter)/--force(profile)
  五轴分立；`orx agent preflight codex --profile codex-frontier` 读出
  projects trust=trusted、approval_policy 缺失=unknown、-s
  workspace-write(argv)、hooks 八事件名。

### 配置指纹示例（本任务真机读取）

cursor（cursor-strong/worker）：`approval_mode=allowlist (shell allow:
Shell(ls); deny: none)`、`sandbox=disabled`、`trust=true(argv)`、
`force_yolo=false(profile)`，fingerprint
`sha256:8128ae86332242c8ac06922eaf0b5087`。
codex（codex-frontier/worker）：`trust=trusted`、`approval_mode=unknown`、
`sandbox=workspace-write(argv)`、hooks 8 事件，fingerprint
`sha256:1d7636750c39a365bbcd5e324d199b95`。

### 真实/模拟来源、用量、缺失字段

全部行为测试为确定性替身（fake 二进制、tmp HOME/CODEX_HOME、零网络、
零付费调用）；真机仅只读配置读取。本任务不产生模型 usage observation
（preflight 无 completion）。缺失字段：cursor 的 project_hooks 轴为
unknown（无已知的 cursor 项目级 hooks 配置位置）；codex approval_policy
在本机缺失如实 unknown。耗时区间：约 2026-10-08 08:55–09:15 (+08:00)。

---

## T006 · 补齐宿主故障事实、可信观察与安全恢复入口

**输入commit**：`a4d96b4`（T005 交付，2026-10-07 22:37:14 +0800）。

### 本任务自身成为活体证据：attempt 150 的三次钟

本任务的第一次派发（attempt 150）在执行中被同一种宿主故障击中，成为本任务
要建的机制的直接一手证据（来源：ORX 库只读读取 + 新 `zcode_sessions.session_facts`
对真实 zcode 库的只读观察，全部原始值，未再定时区）：

| 事实 | 时刻/值 | 来源 |
| --- | --- | --- |
| 会话创建 | 2026-10-07T22:41:34.205+08:00（epoch 1791384094205） | zcode.session.time_created |
| claim | 2026-10-07T22:41:40+08:00（2026-10-07T14:41:40.555386+00:00） | orx attempts.started_at |
| 心跳 #1（phase=start-gate-green, "reading seams"） | 2026-10-07T22:41:55+08:00 | attempt_progress received_at |
| 会话最后活动（真实故障时刻） | 2026-10-07T22:55:05.238+08:00（epoch 1791384905238） | zcode.session.time_updated |
| 关闭（fail+retry） | 2026-10-08T08:32:00+08:00（2026-10-08T00:32:00.765468+00:00） | orx attempts.ended_at |

三个钟的间隔：claim→最后活动 13 分 25 秒（43 个模型步、9 次 Edit、1 次 Write
——即树中留下的未提交实现）；最后活动→关闭 **9 小时 36 分 55 秒**（控制器与
worker 同一 provider 账号，GLM 1308 5h 窗口；provider 重置串
"2026-10-07 23:26:04" 无时区，逐字保留——按 T005 语义恢复时刻未知，跨 slice
不自动放开，窗口重置后 2026-10-08 08:30 探针绿）。墙钟跨度（9h50m）不代表
模型运行时（13m25s）。已记录的 fail reason 把故障时刻近似为 "~22:42"；
机器读取精化为 22:55:05（worker 在心跳后继续工作了约 13 分钟）——原记录
保留不改，本段即补充纠正证据（新 `orx task note --kind correction` 的用例）。

终止事实的来源链：控制器（launcher）直接观察到子代理进程退出 → 以具体
reason 执行 `orx task fail` → `orx task retry` 改派 attempt 151（本次交付）。
attempt 150：session `sess_subagent_agent_02f3dfad-daf2-4dcc-b344-e84683b0ba61`，
nonce `orx-assignment:b2487450-dd8c-4b58-a036-0b24eff0ca31`；
attempt 151：session `sess_subagent_agent_9698a61b-4938-46d2-a08d-6080856fa572`，
nonce `orx-assignment:368d16e8-ad51-4eda-966d-518d38908bde`。

### 红信号（接手时）

- 树中留有 attempt 150 的未提交半成品：dispatch.py/zcode_sessions.py 已改、
  tests/test_host_recovery_facts.py 未跟踪；`orx run` 输出的恢复提示文本引用了
  `orx task observe/recover/note` 三个**尚不存在**的 CLI 子命令（提示与现实不符）。
- 半成品测试 6 红：separation 文本缺 "silence is not death" 短语；nonce 回退
  查找用 `attempt_update(session_ref=None)` 擦绑定无效（None=不改是既有语义）；
  `_open_worker_attempts` 未按任务过滤（T002 的停泊 attempt 误计为第二个
  writer）；recover 后旧 attempt 迟到心跳断言 `attempt_closed` 与冻结门禁
  顺序冲突（任务非 running 时状态门先拒——test_host_progress_e2e 既有先例）；
  run done 断言在 T002 未完成时必红；CLI/skill 两个面未实现。

### 实现选择

1. **`zcode_sessions.session_facts`（只读终态事实）**：单会话的
   created/last_activity/archived/last_part（epoch 毫秒原值，从不再定时区）与
   model_steps/edit_calls/write_calls 计数（json_extract；json1 缺失时如实
   unreadable，绝不补零）。每个事实带来源；`verdict` 恒为 None——"无活动"
   是事实不是死亡判定，心跳通道的沉默是另一个本函数根本不读的事实。
   列缺失→unreadable，不猜不补。mode=ro + query_only，绝不写。
2. **`dispatch.task_observe`（独立可调用的只读诊断）**：任意任务状态可用
   （事后复盘入口）。身份绑定 attempt/nonce/session（session_ref 优先，其次
   nonce 只读查找——查到的 id 只报告不落库，绑定身份是 claim/complete 的职责）；
   保留 claim 事实（claimed_at/session_ref+来源）；progress 窗口（G006 语义，
   silence 是观察不是死亡）；closure 事实；`activity_after_last_report` 为布尔
   事实（失联仍在写=观察，不升格为存活判定）；`separation` 文本显式三分
   真实故障/观察/关闭三钟。全路径零写入（测试断言 ORX 状态快照与 zcode 库
   字节级不变）。
3. **`dispatch.task_recover`（显式恢复入口）**：操作者必须提供确认终止事实
   （1-512 字符，缺省/空白=usage 错 exit 2；ORX 永不自行判死、永不由心跳
   静默认定死亡）。仅接受 running+开放 host worker attempt（即 session 断裂
   留下的 parked-writer 状态；waiting_host 无 writer、failed 已关闭各有其门）。
   关闭 attempt 时 failure_reason = 操作者事实 + 机器读取行（claimed_at/session/
   last_activity/archived/edits/writes/model_steps/heartbeat 窗口/closed_at）——
   a130 误报防护：记录的关闭理由再也不能声称 "never claimed / no changes"
   而证据库里存在 claim 与改动；操作者事实与观察矛盾时两者都保留。单
   writer：先关闭被终止 attempt 再开 dispatch（fail+retry 的组合单步）；旧
   attempt 迟到交付被既有 stale 门拒绝、迟到报告被 closed/superseded 门拒绝、
   旧 nonce 永不覆盖新 attempt。
4. **`dispatch.task_note`（追加式操作者通道，封闭词表）**：
   `shared_failure`——控制器与 worker 同故障域（如同一配额账号）都倒下：记录
   事实并保持可解释停留（开放 attempt 保持所有权、迟到交付仍可受理、无任何
   自动继续；无实际恢复执行者就不承诺自动恢复）；`correction`——历史误报的
   补充纠正证据：追加一条引用原 attempt、逐字引用其 result/failure_reason 的
   事件，绝不 SQL 改库。两种都只写一条 task event，不改任何状态/attempt/
   verification/usage。
5. **`task_complete` 门禁顺序修正（引用 attempt 时）**：引用了 attempt id 的
   交付先判身份（文档校验之后——既有排序契约"先证据后身份"保持不变），身份
   stale 时无论任务当前状态都给出 stale-submission 诊断（recover 后任务已
   runnable，迟到调用者必须得知自己的 attempt 已关闭，而非被状态消息误导）；
   未引用 attempt 的路径状态门顺序不变。
6. **心跳门禁顺序不動（冻结）**：recover 后任务 runnable 期间旧 attempt 迟到
   报告按冻结契约得 `task_not_running`（状态门先拒，与既有 e2e 先例一致）；
   新 attempt claim 后（任务 running）旧 attempt 报告得 `attempt_closed`。
   测试按此对齐，不改冻结门禁。
7. **CLI 三面**（`orx task observe/recover/note`，与提示文本一致）：输入类
   拒绝 exit 2（`RECOVERY_INPUT_REASONS`：termination_fact_invalid/
   note_kind_invalid/note_detail_invalid/note_attempt_required），--json 信封
   error={reason,message}（与 heartbeat §5 同律，渲染函数共用）；observe 人类
   输出逐事实带来源、末行 `verdict: none (observation only)`。
8. **skills/orx-controller 恢复章节**重排为 1-6 步：有界等待→先读事实
   （observe，never a verdict）→确认死亡才 recover（需 termination fact，
   与机器事实并录；手工 fail+retry 仍可用）→绝不第二个 writer→同故障域
   shared-failure 停留（记录事实、不自动继续）→correction 追加不改史。
   全文不越 G006 §11 红线（自动化/liveness 宣称逐行测试钉住）。

### 绿验证

- 新 tests/test_host_recovery_facts.py **17 passed**：三钟区分（02:05 故障/
  01:30 观察/开放 closure）+字节级只读证明；无库/无 session/nonce 回退的
  诚实降级；never-claimed 仅在为真时呈现；termination fact 边界（空白/超长
  exit 2、拒绝零写入）；机器事实并录（claimed_at/session/edits=35/writes=3
  入 failure_reason；closure 08:30 落后故障 6h25m）；单 writer + nonce 轮换
  （旧 attempt 迟到交付 stale×2、迟到报告 task_not_running→attempt_closed、
  新 attempt 正常交付 run done）；非 parked-writer 状态拒绝；shared-failure
  停留（零状态移动、心跳续报、迟到交付仍受理、observe 呈现 note）；
  correction 追加（原行/原事件不动、逐字引用、zcode 库字节不变）；失联仍在写
  保持纯观察（overdue+一分钟前活动并存、verdict None、恢复仍需事实）；
  CLI 三面 exit code/信封/人类输出；run 恢复面新入口与 G006 §10 冻结合同
  字符串共存；skill 教学 + 红线逐行。
- 门禁三文件（test_host_progress_e2e + test_health + test_quota）
  **73 passed**；全量 **984 passed、0 xfail**（基线 967 + 17 新增）。
- `session_facts` 对真实 zcode 库实跑一次（上表 attempt 150 会话），
  found=True、unreadable=[]、verdict=None。

### 真实/模拟来源

- 事故时钟/编辑计数：测试为确定性替身（伪 zcode 库 + 冻结 ORX 时钟）；
  上表"三次钟"为真实库实读。attempt 150 的失败/重试为真实事件（非注入）。
- 无任何付费模型调用用于验证；一切断言确定性。

### 配置指纹（本任务有效值）

driver=host（zcode-worker profile）、`worker.max_check_rounds=3`、
`worker.progress_timeout_min=60`（默认）、`worker.max_same_blocker_attempts=3`；
无 quota_domain 键（独立域）。G006 契约 §1-§11 未改动（门禁顺序、错误表、
观察字段、无副作用边界原样）。

### 用量来源

host driver 的 attempt 不经 adapter 启动，ORX 无其 usage 行（host 语义）；
模型活动观察来源为 zcode 会话计数（model_steps/edit/write）。attempt 150：
43 步/9 Edit/1 Write（约 13m25s 活动跨度）；attempt 151 为本会话，交付后由
控制器门禁与独立 verifier 判定。

### 缺失字段

- attempt 150 会话 archived 为 NULL（事实，非缺失）；provider 重置串无时区
  （逐字保留，恢复时刻未知——T005 语义，不发明时区）。
- zcode 真库 json1 可用；其他安装缺 json1 时计数事实标 unreadable。
- ORX 侧无 host worker 的 token 用量观测（G009 T010 范围）。

### 耗时区间与提交

- attempt 150：claim 2026-10-07 22:41:40 +0800 → 终止 ~22:55 +0800（活动
  13m25s）；关闭 2026-10-08 08:32:00 +0800。attempt 151（本次）：claim
  2026-10-08 08:32:51 +0800 → 交付（本地提交见下）。
- 提交：`G009/R009 T006`（含 attempt 150 的未提交半成品折叠）。
