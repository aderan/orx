# 恢复与成本治理实施报告（G009）

本报告是 docs/recovery-execution-goal-2026-10-07.md 定义的实施报告（"证据、暂停点与最终交付"）。
每个任务记录：输入commit、红信号、实现选择、绿验证、真实/模拟来源、
attempt/session/nonce、配置指纹、用量来源、缺失字段、耗时区间与提交。
逐任务追加；Goal 终验（T012）时按八条验收汇总证据表。

---

## T010 · 统一 provider 用量口径并补齐 host/controller/probe 观测

**输入commit**：T009 交付后 2bcbe40（attempt 159；host 子代理会话
sess_subagent_agent_5d27b110-b133-4aa3-8700-20e1905c42b0，nonce
orx-assignment:bcef68e5-89c6-4874-a1af-b9ee234e3ac7 经
claim --discover-session 绑定成功——本仓库根无 symlink，走既有精确
匹配路径；/tmp 别名路径由新增确定性 symlink 测试钉住）。

**红信号（先写行为回归，全部失败后转绿；15 例新增）**：
normalized_usage 词汇 6 例（test_adapters：zcode/codex 子集口径
processing=input+output 缓存绝不叠加、cursor 独立桶
input+cache+output 且 cacheRead>>input 正常、缺失字段 None 不为零、
未知 harness 零主张、子集矛盾 cached>input 时 uncached=None 且原始
字段原样保留）；e2e 归因 5 例（test_host_progress_e2e：伪 zcode 库
session/part/model_usage 上的会话树聚合去重与 error 零值诚实、
目录别名（symlink 拼写）下 nonce 发现绑定、usage zcode 记录子树/
幂等/重叠拒绝/host_session_unbound 缺失留痕/非 host-zcode 拒绝、
controller/worker/verifier/planner/probe 五本账分立且 controller 只记
根会话自身、controller unbound/unavailable 显式缺失不为零）；
state v12 迁移 4 例（test_state：CHECK 加宽不回填、zcode_db 单例
幂等/冲突、迁移失败原库保全、备份副本独立迁移）。

**实现选择**：

1. **统一词汇在 adapters.base.normalized_usage**（展示层推导，从不
   入库）：HARNESS_CACHE_SEMANTICS 固定两义——zcode/codex
   cache_subset（input 已含缓存，processing=input+output，加缓存即
   双算；uncached=input−cached，cached>input 矛盾时 None 不裁剪）、
   cursor cache_separate（三桶相加，input 桶即未缓存）、shell/未知
   无主张。NORMALIZED_NOT_BILLING_NOTE 固定"处理量非账单"措辞，
   CLI/JSON 两面都携带；不新增 fee/cost 字段。
2. **zcode 侧读回（zcode_sessions）**：session_tree（parent_id 递归，
   只读）+ model_usage_aggregate（按 (session_id, logical_request_id,
   attempt_index) 去重并计 dropped；error 行全零 token 计入
   error_rows_zero_tokens——零是占位不是免费证据；computed_total 汇总
   provider 自有列=input+output，从不把缓存加在 input 之上）+
   session_tree_usage 组合。子树=自身+后代，父（控制器）永不混入。
3. **目录别名修复（T009 活体缺口）**：discover_attempt_session 的
   directory 匹配改为两侧 os.path.realpath 归一——项目根已 resolve
   （macOS /private/tmp/...）而会话记录生成时拼写（/tmp/...）时同样
   命中；不同目录永不误匹配。本 attempt 的 claim 绑定即活体证据。
4. **入库（state v12）**：usage_observations.source CHECK 加宽
   zcode_db（v2/v7 同款 backup-replace 迁移，加宽不改列不回填，
   旧值逐字保留）；usage_record_host/usage_record_zcode 收敛为共享
   _usage_record_singleton（每 attempt 每 source 单例：相同幂等、
   不同 ConflictError、入库即清 usage_missing_reason）。
5. **归属面（dispatch.usage().attribution）**：roles 按 attempt.role
   分账（controller/planner/worker/verifier + 未知角色排序在后），
   每本带 attempts/observed/unassessed/missing_reasons 分布/原始
   token 和（_token_sum 缺失即 None）/normalized processing 与
   uncached 的部分和 + normalized_incomplete_observations 计数；
   controller 本=根会话（ORX_SESSION_REF）自身 usage 的只读活查，
   子代理会话归各自 attempt，绑定到无 attempt 的后代列
   unattributed_sessions 单列不并入；probe 本恒在、注明"探针从不跑
   completion，空是设计"——单列防漏算防混入。ZcodeDbUnavailable→
   unavailable、无 env→unbound，皆显式非零。
6. **补录入口 dispatch.usage_zcode_record + `orx usage zcode`**：
   仅 host zcode attempt；session 解析=显式 --session > attempt 绑定
   >一次 nonce 发现（**绝不读 ORX_SESSION_REF**——host 运行内它命名
   控制器会话，把控制器子树记进 worker 正是本命令要防的双算）；
   重叠防护双向（他 attempt 会话在本子树内、或本会话在他子树内皆
   ConflictError）；不可解析记 host_session_unbound（MISS 词汇新
   成员）；error 零值行或去重丢弃时 accuracy=estimated；model_usage
   不可读返回 unreadable 不写库。

**绿验证**：门禁两文件 75 passed（16.6s；attempt 159 check 2/3 轮）；
全量 1022 passed、0 xfail（基线 1007+15）；docs/m1.2-report.md 全量
重跑后按约定还原。schema 版本 11→12 的既定涟漪全部随版本更新：
observability-contract.md gate 放行 12（第六修正案：zcode_db、
host_session_unbound、目录别名、归一化展示规则与"非账单"边界；
consumption 权威序 native_cli>zcode_db>host_report>output_estimate）、
gate 测试 12=ok/13=refuse、21 处 ==11 断言、包验收链期望改为锚定
orx.state.CODE_SCHEMA_VERSION（scripts/check_release.py——tests/
test_package_acceptance.py 导入执行的验收基建，按 tests 桶处理并在
交付报告显式披露）。

**真实/模拟来源、用量、缺失字段**：本任务零付费模型调用（15 例
新增测试全部确定性：伪 zcode 库+伪造 token 行+冻结断言）；实现期
对真实 ~/.zcode/db.sqlite 仅做两次只读 PRAGMA/schema 查询取证
（mode=ro）。本 attempt 的 ORX usage 仍缺（host attempt 观测需任务
完成后由控制器补录 `orx usage zcode --attempt 159`——round 记录如
实保留该缺失，不用估算填充）。已知限制：cursor cacheWriteTokens
未入库（ incident 中恒 0，capture 词汇未扩展——后续任务可加）；
controller 本的会话号依赖 ORX_SESSION_REF，CLI 终端里通常 unset
（显示 unbound，是诚实缺失不是零）。

**耗时区间与提交**：实现+验证约 4 小时会话墙钟（含探索与全量
两跑）；提交见 git log `G009/R009 T010`。

---

## T009 · 运行隔离真实微任务，验证投产 profile 与切换接手者

**输入commit**：T008 交付后 40a5fcb（attempt 157；host 控制器会话
sess_698a4146-176f-465e-9357-39e3344878fa 认领执行——本任务要求
host_context，由具备原生宿主启动能力的执行者组织，不交 CLI）。

**隔离仓库**：/tmp/orx-g009-micro（git e3d3672 预置红 add → 74c1cef
预置红 mul；stdlib unittest，无外部依赖）。结构与原始证据：
docs/diagnostics/g009-microtask-2026-10-08/（evidence.json + 四个
attempt 的 delivery/check/diff/preflight 文件）。

**真实闭环（4 次付费启动，每 profile 首轮一次，权限阻塞即收口）**：

| attempt | profile | 墙钟 | 结果 | 关键事实 |
|---|---|---|---|---|
| 1 (R001) | cursor-strong (CLI, grok-4.7-medium) | 54.3s | **blocked（正确诊断）** | T007 预检先于付费启动即预测 allowlist Shell(ls) 阻塞；真实启动证实；`delivery blocked (environment/tool blocked):` 前缀关闭，零工作区改动，exit 0 不再覆盖 blocked |
| 2 (R001) | codex-frontier (CLI, gpt-6.1-sol) | 61.9s | **passed（CLI 闭环）** | 一行范围内修复（- b → + b），agent 内 unittest exit 0，结构化 passed 交付，控制器门禁复跑 OK；usage 89121/624/78336 native_cli exact |
| 3 (R002) | zcode-worker (host, 真原生子代理 GLM-5.3 max) | 58.0s | **passed（host 闭环）** | start gate 红（预期基线）→ 一行 mul 修复 → 绿；nonce c4a0484e；门禁复跑 OK；R002 done |
| 4 (R002) | zcode-verifier-flash (host, 真原生子代理) | 56.1s | **pass（独立验收）** | 自行重跑 unittest（2 tests OK）、核对 diff 范围、连 .pyc 与测试源一致性都核了——不是只信 worker 文本 |

**其余启用 profile 状态**：cursor-frontier/cursor-economy——probe ok
(logged_in)，但与 cursor-strong 同一 cli-config.json 权限域，同源阻塞
成立，不重复烧启动；codex-frontier 双重实证（进程+认证+闭环）；
zcode-verifier-flash 另有 R009 本身 T001–T008 的 8 次真实派发；
zcode-verifier-strong 配置在位未被路由触达（如实未验证）；orx-host
为 preset 占位（未验证）。planner 角色：codex 进程/认证实证 + G008
真实 codex 规划历史；本 Goal 计划来自权威模板（host-manual）。

**观察到的缺口（交 T010）**：host attempts 3/4 的 usage observation
缺失（已知 21/26 模式）；`claim --discover-session` 未绑定 session_ref
（nonce 逐字携带、CLI attempts 经信封绑定成功，host 疑 /tmp↔/private/tmp
项目根别名致发现未命中——原始事实见 evidence.json，归因待 T010 核验）。
披露用原始计量：host worker subagent_tokens 659,724、verifier 22,981
（处理量口径，非账单）。

**遗留决策（不属本 Goal 权限）**：cursor 系全部档位在当前生产权限
（approvalMode=allowlist 仅 Shell(ls)）下无法执行内部 shell。修复需
用户决定 Cursor 全局权限调整；本 Goal 明确不修改用户全局权限。

**耗时区间与提交**：4 次真实启动共 ~230s 墙钟；本任务只改
tests/docs/diagnostics（生产代码零改动），全量基线维持 1007 passed
0 xfail（无代码变更，未重跑全量）。提交见 git log `G009/R009 T009`。

---

## T008 · 建立可重复的故障注入与组合验收矩阵

**输入commit**：T007 交付后工作树（attempt 155；session
`sess_subagent_agent_93f38566-8edc-478c-97cf-e119cb3d563f`）。
**范围**：仅测试与文档（tests、本报告、验证计划、IMPLEMENTATION_PLAN）；
生产代码零改动。

### 方法

以 docs/recovery-validation-plan-2026-10-07.md §第二层 12 行矩阵 + GLM 1308
注记 + §退避与切换组合验收 + §探针适用范围为清单，逐行定位 T001–T007 已落地
用例，识别组合缺口后补 7 例确定性测试，再把全表回写验证计划文档（"第二层
执行矩阵：逐行证据定位"节，标记执行时点全绿）。注入纪律与验收要求一致：只
伪造外部进程（PATH 脚本、捕获的 CLI 信封）、提供商响应与时钟（store 内伪造
reset 值/冻结 ORX 时钟缝隙），被测 dispatch 从不 mock。

### 新增用例（7 例，先按现有语义写红点核对后转绿）

1. **认证失效 e2e**（test_health.py::
   test_auth_failure_is_bounded_no_reroute_no_login_loop）：登录失效响应 →
   恰一次失败 attempt（无同 slice 换挡——auth 不属 RESOURCE_FALLBACK_KINDS）、
   profile 门禁、二次 slice 零启动、retry 后路由下一 rung 通过、全程单次
   auth attempt（无登录循环）。
2. **中文 GLM 1308 × driver**（test_routing.py::
   test_reroute_on_chinese_glm1308_quota_parks_host_rung）：额度类失败走
   driver 正确的 host 停泊（事故类别的故障在 T003 分支矩阵上的补位）。
3. **host × 权限抑制**（test_routing.py::
   test_host_worker_same_permission_blocker_is_suppressed_too）：host 经
   `orx task complete` 的同条件权限阻塞三次后不再停泊第 4 次，三处留痕，
   恰 3 个 blocked attempt。
4. **抑制按任务域**（test_routing.py::
   test_suppression_is_task_scoped_other_tasks_still_get_the_profile）：
   T001 抑制后 T002 仍获该 profile（独立 streak 1 vs 3，per-task attempt
   计数定位）。
5. **同 profile 配额 × override**（test_routing.py::
   test_operator_override_outranks_learned_exhaustion_on_the_same_profile）：
   学习 exhaustion 让位给操作者 override（primary、override=1、学习免疫），
   clear 后学习恢复。
6. **控制器同故障域**（test_routing.py::
   test_shared_quota_domain_gates_the_controller_until_a_recoverer）：
   planner 与 worker 同 quota_domain；worker naive-reset exhaustion 后
   plan_route 两次同样 RoutingError（如实停留），操作者为 planner override
   后 planning 重开，worker 门禁不受牵连。
7. **陈旧身份 × 换挡**（test_host_progress_e2e.py::
   test_stale_cli_attempt_cannot_deliver_after_resource_reroute）：
   CLI 限流换挡停泊 host 后，死亡 rung 迟到交付 stale 拒绝、状态零变化，
   新 owner 唯一 completion 入库。

### 发现的生产缺口（记录，未在本任务修复——生产代码不在写权限内）

- **抑制的重启用缺少配置指纹消费者**：同任务同 profile 的抑制解除目前只能
  靠该 profile 记录一次异指纹/异类结果，但被排除的 profile 无法再被启动来
  记录该结果（鸡生蛋）；T007 的权限配置指纹（preflight_invalid_reasons）
  尚无任何机制消费来自动重开考虑。当前如实行为=控制器改路由/配置或换
  profile（remedy 文案所述）。候选后续任务：run_slice/route 消费
  "条件指纹变化"（preflight 指纹 ≠ blocker 事件时戳时的条件）作为显式
  重开入口；在那之前矩阵以任务域作用域用例钉住现状。

### 绿验证

- 门禁：`.venv/bin/python -m pytest -q tests/test_health.py
  tests/test_quota.py tests/test_routing.py tests/test_probes.py
  tests/test_host_progress_e2e.py tests/test_delivery_gate_e2e.py`
  → 189 passed（基线 182 + 新增 7；check log `.orx/runs/R009/check/T008/`）。
- 全量：`.venv/bin/python -m pytest -q` → 见下方提交时记录（1007 passed、
  0 xfail 预期：基线 1000 + 7）；docs/m1.2-report.md 不重跑不触碰。
- `git diff --check` 干净。

### 真实/模拟来源、用量、缺失字段

全部新增用例为确定性替身（fake 脚本/捕获信封/伪造 reset 值），零付费模型
调用、零网络；本任务不产生 usage observation（无 completion）。缺失字段：
第三层真实微任务（真权限边界、nonce、真实执行）明确不在确定性矩阵内，由
T009 验收，验证计划文档已按非宣称边界记录。耗时区间：约 2026-10-08
09:20–10:20 (+08:00)。

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
