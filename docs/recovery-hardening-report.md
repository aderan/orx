# 恢复与成本治理实施报告（G009）

本报告是 docs/recovery-execution-goal-2026-10-07.md 定义的实施报告（"证据、暂停点与最终交付"）。
每个任务记录：输入commit、红信号、实现选择、绿验证、真实/模拟来源、
attempt/session/nonce、配置指纹、用量来源、缺失字段、耗时区间与提交。
逐任务追加；Goal 终验（T013）时按八条验收汇总证据表。
报告自 T006 建立：T001–T005 的任务记录在 IMPLEMENTATION_PLAN.md 的
G009/R009 轮次记录与 docs/diagnostics/ 各目录，不在本报告补写。

---

## 收尾（2026-10-08，用户决策后）

**收尾① Cursor 完全权限 + cursor-strong 真实闭环**（98814ff/2716ba4）：
用户决策授予完全权限——`~/.cursor/cli-config.json` `approvalMode`
allowlist→unrestricted（备份 `cli-config.json.bak-2026-10-08`；deny 保持空、
sandbox 保持 disabled；未走 profile force 旗标，全局配置即权限源）。T007
预检即时反映新轴值（指纹失效重算）。真实验证：隔离仓新预置红 sub 缺陷，
cursor-strong 单次真实启动 1:53 完整闭环（nonce c411dfb9、真实 session
c6cbecfc、恰好一行修复、agent 内 unittest 4 tests exit 0、结构化 passed
交付、门禁后 passed、R004 done、usage 32798/3315/150912 native_cli exact）。
原 T009"cursor 权限阻塞留用户决策"限制解除并实证；证据 addendum 追加于
docs/diagnostics/g009-microtask-2026-10-08/evidence.json。观察项：worker
网络断连重试 complete 被状态机正确拒绝（illegal transition），complete
幂等性列为后续小项。

**收尾② host attempts 用量自动补录**：
`dispatch._usage_autobill`——host zcode attempt 到达终态的五个闭合点
（worker complete / failed/blocked 交付 / task fail / task recover /
verifier verdict submit）自动读取会话子树用量并记录 zcode_db 观测行；
永不抛错（补录不得弄坏它跟随的闭合）；不可记录结果一律陈述为 missing
reason 而非清零：子树冲突=`zcode_subtree_conflict`、聚合不可读=
`zcode_unreadable`、无会话=`host_session_unbound`（沿 T010 语义）；zcode
库不存在则安静跳过（ORX 无从知晓，attempt 留给 usage 视图计 unassessed）；
配置门 `[usage] auto_backfill_zcode`（默认 true，分层 + `orx config set`
可写；env 层为 int-only 故不设 env 键）。手动 `orx usage zcode --attempt`
保留为重同步路径。附带修复 G009 运行中发现的 CLI 缺陷：`orx task complete
--session` 此前被 CLI 层静默丢弃（attempt 157 session_ref 为 NULL 的根因），
现已转发。观测契约 missing 词表补两义。测试
tests/test_usage_autobill.py 7 例全确定性（伪 zcode 库；含真实 claim→
complete 流程、冲突陈述、配置关闭、库缺失安静跳过、CLI attempt 永不
autobill、CLI 转发契约）。全量 1043 passed 0 xfail（1036+7）。

**已知边界（沿终审一致）**：attempt 164（根会话绑定为 worker attempt）
持有控制器子树所有权期间，R009 其余 host attempts 的逐 child 回填被防双算
守卫拒绝——该 run 的 host 用量以 zcode 侧原始事实（observe/subagent_tokens
披露）为准；此后新 attempt 的用量在闭合点自动入账。

---

## T012 · 执行兼容、打包、迁移及全量验证，整理使用文档

**输入commit**：T011 交付后 cef9e7f（attempt 163，session
sess_subagent_agent_13533091-e3f3-4ec9-ace2-b5ea77a416aa，claim
2026-10-08T03:56:52Z；START GATE 首轮 prescribed 两命令即绿——本任务
以既有全量绿基线为起点，零付费模型调用，全部验证为本地命令）。
usage：本 attempt 无 usage 观测行（纯本地命令任务，如实缺失非零）。

**红信号（先写行为回归，再修文档；无新生产代码）**：T010 已把读取契约
第六修正案推进到 v12（gate SQL `IN ('8','9','10','11','12')`），但仓库
三处使用文档仍停在 v11 时代，其中一处被回归钉住为"当前事实"——钉住的
就是错的。`tests/test_delivery_gate_e2e.py` 的 README 观测契约测试先行
更新（允许清单断言 8/9/10/11→加 12、钉住旧串 "8, 9, 10, and 11" 不得
残留），对未修 README 实跑红（`'v8/v9/v10/v11/v12' in flat` 失败），
随后修 README 转绿。

**实现选择（文档与既有机器，最小改动）**：

1. **README 三处**：Usage 节观测表述——表族 v8/v9/v10/v11→+v12、
   "v9, v10, and v11 amendments were additive"→加 v12、允许清单
   "8, 9, 10, and 11"→"8, 9, 10, 11, and 12"（与被引契约的 gate 一致）；
   Docs 索引同步 v8/v9/v10/v11/v12；升级提示框 "schema v8 migrates to
   v11 automatically" 改为诚实双值表述（发布 0.3.1→v11；当前未发布树
   →v12——同一 0.3.1 版本号下源码树与发布包的 schema 已分叉，README
   不得只说其中一个）。docs/upgrade-0.3.1.md 是 0.3.1 发布故事（G007
   冻结），不改写。
2. **docs/package-acceptance.md §3 第 8 步**：升级链描述随 T010 的
   `orx.state.CODE_SCHEMA_VERSION` 锚定更新——"v8→v11 自动迁移"→
   "v8→当前版本（锚定 CODE_SCHEMA_VERSION，G009 T010 起为 v12）"，
   补记 v12 相对 v11 仅放宽 `usage_observations.source` CHECK 收
   `zcode_db` 行、无新表新列；v0.3.0 拒读消息引用本次实测原文
   `schema version 12 is newer than supported version 8; upgrade orx`，
   并说明脚本断言的是稳定子串（随版本前行）。改前实测该文档与机器
   行为不符：链路已升 v12 而文档仍写 v11。
3. **README 不加新节、不改写**（按交接约束）：G009 新命令面
   （task observe/recover/note、agent preflight、usage zcode、doctor
   permissions、budget/check_timeout 配置键）的成文在 T004–T011 已
   落 docs/routing-strategy.md Recovery/预算节、observability-contract
   第六修正案与本报告各条目，不在 README 复制第二份。

**兼容、打包与迁移执行（全部既有机器；未新增检查、未发布）**：

- **全量**：`.venv/bin/python -m pytest -q` **1036 passed, 0 xfail**
  （START GATE 首轮与文档修改后终验各一跑；docs/m1.2-report.md 被
  测试重写的纯生成差异——cutoff/mtime/修订号等活性读数——按约定
  还原提交内容）。
- **打包（wheel 链）**：既有入口 `uv run python scripts/check_release.py`
  全绿（exit 0、problems 空）：`uv build --wheel` 构建于临时目录
  （仓库 dist/ 不动）→ 干净 venv 安装 → 包内 agents/skills 资源探针 →
  preset 全装+保语义 → 全部打包技能安装与漂移刷新逐字节一致 →
  stand-in Goal 全闭环 → fixture 结构与再生核对 → 升级链 v8→12
  逐行身份核对+真实 v0.3.0 拒读（字节不变）+备份恢复读回。
  **版本保持 0.3.1**：pyproject 与 `src/orx/__init__.py` 未动，无新 tag、
  无发布——发布由用户决定（硬边界）。
- **打包（sdist 面，追加一次性验证）**：`uv build --sdist` 成功，sdist
  以 repo 布局携带全部资源真源（agents 3 角色 + skills 3 技能含
  orx-pbv references 共 11 个文件）；由 sdist 再构建的 wheel 同样含
  全部 6 个打包资源（orx/agents/*、orx/skills/*/SKILL.md）——源码安装
  路径与 force-include 一致。
- **迁移覆盖核对（无缺口，未无理由重复）**：schema v12 由
  tests/test_state.py 四例（v11→v12 增量不回填、zcode_db 单例幂等/
  Conflict、迁移失败保原库、v11 备份/副本照常迁移）+
  test_m12_acceptance.py（legacy v6→12 迁移+列语义）+ 包验收链
  （v8→12 全行身份）覆盖，全部在全量内跑过；打包/技能资源变动由
  包验收链逐字节钉住（T011 对 skills/orx-controller、skills/orx-agent、
  agents/orx-worker.md 的改动因此自动受验）。
- **诚实审计**：`git diff 5589242..HEAD -- tests/ src/ scripts/` 无
  skip/skipif/xfail 新增（唯一命中为 T001 xfail(strict) 红基线脚手架的
  注释文字；标记本体已由 T002/T003 按约定移除，当前 tests/ 零
  skip/xfail 标记）；仓库无 formatter/linter 配置（无 ruff/black/
  flake8/mypy 等配置与依赖，pyproject 仅 hatch+pytest），如实说明，
  不为凑检查引入工具。

**绿验证**：README 回归先红后绿；`tests/test_delivery_gate_e2e.py`
33 passed；全量 1036 passed 0 xfail；`git diff --check` 干净；
`uv run python scripts/check_release.py` exit 0 且 problems 空。

**证据**：docs/diagnostics/g009-t012-packaging-2026-10-08/
（check-release.json 完整链路报告与 stderr、sdist-build.log、
sdist-contents.txt 含资源清单与 sha256、wheel-from-sdist.log
sdist→wheel 资源核对）。

**限制（如实）**：sdist→wheel 资源核对与 sdist 清单为一次性验证，未
固化为测试——wheel 链已有全量固化，sdist 形态无既有检查机器，遵循
"不无理由重复/不新造凑检查"约束；本任务零付费模型调用，故无 A/B 或
用量对照可记；事故勘误维持只在仓库（外部 ZCode memory 更新超出本
任务与仓库范围）。

---

**attempt 163 → 164 完成事实（控制器补记，2026-10-08 12:46 +0800）**：
attempt 163（worker 子代理）完成全部文档/打包工作后在最终全量门禁执行中
撞 GLM 1308（5h 窗口，reset 13:30:54 无时区；会话侧最后活动 12:13:01，
55 模型步/9 Edit）——本 Goal 第二次同域配额故障。控制器 observe/note
shared-failure/recover 按协议关闭 163（真实故障 12:13 vs 关闭 12:46，
33 分钟；对照事故 6h26m），attempt 164 由控制器（host）认领收尾：
审查半成品 diff → 还原测试再生的 docs/m1.2-report.md → 全量 1036
passed (110.65s) → git diff --check clean → skip 审计零新增 →
打包验收 problems=[]（版本保持 0.3.1，未发布）→ 本提交。两次 attempt
的工作在同一个提交中交付，163 的用量由 zcode 会话侧事实记录（observe），
164 无 ORX usage 观测行（控制器会话，如实缺失非零）。

## T011 · 用对照实测压缩 controller 与 worker 上下文，形成预算策略

**输入commit**：T010 交付后 18d10f4（attempt 161；host 子代理会话
sess_subagent_agent_07ae8e5e-ad3e-4ff4-9391-71296872fbaf，claim
--discover-session 绑定，check_rounds 0/3——本任务无命令验证条目，
agent 检查由独立 verifier 判定；本任务全程预算：付费启动恰 1 次，
见下文 A/B）。

**红信号（先写行为回归，14 例新增 tests/test_context_budget.py，全红
后转绿）**：提示词压缩 4 例（每条 prescribed check 恰出现一次、brief
契约省 >1000B 且保留任务绑定事实、CLI/exit 通道四条款+机器交付条款
逐字钉住、host_mode=subagent 停泊走 brief 而 self 停泊走 full）；
预算配置 3 例（内置默认=校准样本值、TOML 解析+校验+非法值拒绝、
env 层覆盖 origin=env）；预算事实与门禁 5 例（按 harness 归一化
求和+未知不补零、recorded 处理量超限硬停三处留痕+提额恢复点、
attempt 数门禁把未知用量的尝试如实计数、软告警只注记不拦截、
run 级硬停 defer 全部新启动）；诊断长挂 1 例（check_timeout_sec=1
时 sleep 3 有界超时）；解析器线性回归 1 例（200KB 无括号 slug 流
<5s——修复前实测 110s——倍增输入 <3x，带括号样例仍正确发现）。

**实现选择**：

1. **worker 提示词压缩（零信息损失的两处）**：每条验证条目原先出现
   两次（"How your work will be checked" 明细 + preflight 行），合并为
   一处——preflight 行即检查清单，标题 "How your work will be checked"
   保持不变（test_assignments 既有钉住不受影响）；四条款交付契约对
   host subagent worker 是与 agents/orx-worker.md 的逐字重复（harness
   注入该定义），host_mode=subagent 的停泊改携 brief 引用（保留
   预算数、任务命令、升级语义，指名四条款按定义 verbatim 适用）。
   CLI/external/self 通道无定义可证已加载：保留完整内联契约（G009
   T002 的 CLI 机器交付条款照旧）。verifier 提示词本就短（事故数据：
   首 input 中位 6,242.5 vs worker 72,245），不动。
2. **预算策略（config 分层 env > project > user > builtin，全部
   可 orx config set）**：`budget.soft_warn_task_processing_tokens=8M`
   （告警：微任务 ~90K 处理量、host 侧原样 659,724、a130 一小时真实
   工作 25.74M——8M 在病理区前早、正常工作后晚）、
   `budget.hard_stop_task_processing_tokens=50M`（T003 全链 ~44M：
   50M 放行 a130+a134 合法链，阻断二次延续）、
   `budget.hard_stop_task_attempts=8`（T003 用 6 次）、
   `budget.hard_stop_run_processing_tokens=150M`（全会话归一化
   ~155.8M）。归一化沿用 T010 词汇（zcode/codex=input+output、
   cursor 三桶相加，非账单）；**未知用量绝不补零**：无观测的 attempt
   计入 attempts_unassessed 并由可数的 attempt 门禁兜底，观测行存在
   但 harness 无语义时计 attempts_unnormalized 不入和。硬停在
   run_slice 派发前生效（任务级三处留痕：routing_errors+task event
   dispatch_budget_stopped+预算载荷；run 级 defer），任务保持
   runnable、证据/验证行/attempt 历史全部可查——**恢复点=调高阈值**，
   软告警只注记（task list 每任务携带 budget 观察；run 输出顶层
   budget）。构建在 T004 抑制缝（同为"不启动+可解释停止"）与 T006
   恢复事实（不删任何已记录事实）之上。
3. **诊断长挂边界 `runtime.check_timeout_sec=600`**：check/门禁/verifier
   命令五处调用点换用该值（worker/planner/verifier 启动仍用
   command_timeout_sec）；a130 在一次 pytest 卡顿中耗 ~42 分钟静默等待
   的形态，今后在 600s 处变成有界红结果（修或升级）。既有
   test_check_timeout_semantics_match_verify 相应迁移到新键（语义测
   试不变，键随缝移动）。
4. **解析器性能回归**：model_catalog 的线性括号扫描（修复已在树，
   注释留有 110s/200KB 实测）钉住：绝对界 5s（22x 余量）+倍增界
   <3x + 带括号样例功能正确。引用 a130 ~42 分钟诊断事故；不再把
   O(n²) 列为待修根因。

**对照实测（可重复；基线为已记录实测，未重复付费基线）**：

*静态·受控（确定性，零付费；measure.py 重跑即得）*——同一任务行
（R009 全部 13 个任务，state 只读副本）同一输入下，旧渲染器（父
commit 逐字引用，与归档提示词交叉验证差异仅 ~129B 行折叠）vs T011
渲染器：

| 形态 | 旧 (B/任务) | 新 (B/任务) | 节省 |
|---|---:|---:|---:|
| host subagent（brief 契约） | 5,639（均值） | 3,836 | **-1,803（-32.0%）** |
| CLI / self（full 契约） | 5,639 | 5,379 | -260（-4.6%） |
| 真实归档派发提示词（含 nonce/失败上下文，原始事实） | 5,459–6,712（T006 最高，携失败链） | — | — |

*真实·小任务 A/B（恰 1 次付费启动；Controller COST BOUND 授权的上限）*
——同形微任务、同 profile（codex-frontier）、同隔离仓：

| 指标 | T009 基线（旧提示词） | T011 A/B（压缩后） | Δ |
|---|---:|---:|---:|
| 上下文（派发提示词字节） | 6,467 | 5,146 | -1,321B（注：基线携失败链块，非同类对比；受控同类 -4.6%） |
| 请求（启动数） | 1 | 1 | 0 |
| 缓存输入 | 78,336 | 75,776 | -2,560 |
| 未缓存输入 | 10,785 | 9,654 | -1,131 |
| 输出 | 624 | 533 | -91 |
| 处理量（codex 子集口径） | 89,745 | 85,963 | -3,782（-4.2%） |
| 检查轮 | 1 | 1 | 0 |
| 正确率 | passed，一行修复 | passed，一行修复（sub: +→-，R003-scope.diff） | 保持 |
| 墙钟 | 61.9s | 70.0s | +8.1s（单对噪声） |

*代表性开发任务（改后=静态测量+诚实声明，未做付费重跑）*：基线为
事故实测（host worker 首 input 中位 72,245 vs verifier 6,242.5 ≈11.6x；
根会话均值 184,113/峰 287,769；a130 累计 25.74M、其中 ~42 分钟卡在
O(n²) 解析诊断）。改后可测部分：ORX 生成提示词 scaffolding
-32.0%（host subagent）；不可测部分如实声明：harness 工具 schema 与
系统注入主导 worker 首 input 体量且非 ORX 生成，未测量；完整付费
重跑未执行（COST BOUND），不可宣称端到端 32% 节省。

**限制（原文公布）**：单对 A/B 的 token 差（-4.1% input）方向与静态
scaffolding 节省（-4.6%）一致，但 N=1、会话/缓存状态差异主导，不能
归因为压缩单独贡献；host subagent 的 -32.0% 为纯静态测量（平台禁止
子代理再派子代理，无法付费 A/B）；A/B 未跑独立 verifier（成本上限
一次启动），验收信号=确定性交付门禁复跑+单行 scope diff；cursor
域权限未变不重跑；bytes/4 为 token 估算非分词器实测。

**绿验证**：新增 14 例先红后绿；全量 `.venv/bin/python -m pytest -q`
**1036 passed, 0 xfail**（基线 1022+14；迁移 1 例既有超时测试到
check_timeout_sec）；`git diff --check` 干净；docs/m1.2-report.md 未
触碰。

**证据**：docs/diagnostics/g009-t011-context-2026-10-08/（measure.py
可重跑+static-measurement.json；ab-run/ 含 R003 提示词/执行日志/
交付文档/模型解析/scope diff/ab-evidence.json 与 R001 基线提示词）。

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

---

## T013 · 依据完整证据独立终审 Goal（八条验收证据表）

**终审执行**：host 控制器会话 sess_698a4146（host_context 任务，控制器亲执，
attempt 166；2026-10-08 13:0x +0800）。审计基线：git 5589242..65481b5
共 12 个任务提交；两个事故回放终态均 exit 0（replay.py / replay_cli_to_host.py，
2026-10-08 复跑）；全量 1036 passed 0 xfail（T012 复跑 110.65s）；12/13 任务
passed 且每任务均有独立 verifier verdict（T013 本表为第 13 项）。

| # | 验收（goal.json 逐字） | 状态 | 证据与观测时间 | 限制 |
|---|---|---|---|---|
| 1 | CLI 进程成功/结构化交付/门禁相互区分；blocked/failed/缺失/非法不因 exit 0 或绿门禁判 passed | **达成** | T001 红基线 50e68ac + T002 五事实分层 9018f03（extract_cli_delivery 复用 verify 单一 schema；blocked/failed 走 _record_failed_delivery；protocol_error 矩阵）；tests/test_delivery_gate_e2e.py 绿/红门禁双向钉住；T009 真实启动活体证明（2026-10-08 09:47 cursor-strong exit 0+blocked → attempt blocked、task failed、零改动）；回放 replay.py exit 0（2026-10-08 复跑） | shell exit-channel 兼容边界保留（DELIVERY_CHANNEL_EXIT） |
| 2 | CLI/host/external 切换遵循各自契约；CLI→host 产生有效宿主指派，不再报 no adapter for harness zcode | **达成** | T003 fc7b8c9：_reroute_resource_failure 按 driver 三路分发（host=_park_task 正式指派带新 nonce/attempt；external=waiting_external 正式契约）；回放 replay_cli_to_host.py exit 0（RED→GREEN）；tests/test_routing.py 分支矩阵（CLI→CLI/→host/→external/host→CLI/host→host） | 切换有界=每任务每 slice 至多一跳（设计边界） |
| 3 | 权限/认证/额度/限流/代码失败分别诊断；同 blocker 不反复启动；退避与修复预算有界；override 不被自动清除 | **达成** | T004 b9acd98（failure_class/blocker_fingerprint/streak；max_same_blocker_attempts=3；代码红永不抑制）+ T005 a4d96b4（时区诚实 naive reset 不自动放开；探针≠额度；quota_domain 共享故障域；中文 1308 词汇；override 三路免疫）+ T008 40a5fcb 矩阵 12 行逐行定位（auth 有界、全部不可用有界停止等） | T008 记录生产缺口：抑制重启用缺 T007 指纹消费者（后续任务候选，未修） |
| 4 | 宿主恢复区分真实故障/观察/关闭；保留 claim/改动/原始时区；不心跳判死；无两个 writer 或旧 nonce 覆盖 | **达成** | T006 222872f（observe 三钟分立 verdict 恒 None / recover 需操作者终止事实先关闭再重开+nonce 轮换 / note 追加不改史）+ **两次活体事故恢复**：T006 attempt 150（2026-10-07 夜 1308，故障 22:55→关闭 08:32）与 T012 attempt 163（2026-10-08 午 1308，故障 12:13→关闭 12:46=33min，对照原事故 6h26m）；tests/test_host_recovery_facts.py 17 例（迟到交付 stale、失联仍在写纯观察） | 无常驻 daemon（设计边界）；自动接续明确不做 |
| 5 | 预检+注入+真实微任务形成验证入口；≥1 真实 CLI + ≥1 真实 host 完整闭环；其余 profile 有状态证据 | **达成** | T007 0ab2210（五权限轴指纹 `orx agent preflight`，真机读出 allowlist Shell(ls)）；T008 40a5fcb（矩阵可重复）；T009 2bcbe40 四次真实启动：**CLI 闭环**=codex-frontier 61.9s（一行修复→内部测试→结构化交付→门禁复跑→passed，usage 89121/624/78336 exact）；**host 闭环**=zcode-worker 真原生子代理 58s（同链路→R002 done）；独立验收=zcode-verifier-flash 56s pass；其余 profile 状态表在 docs/diagnostics/g009-microtask-2026-10-08/evidence.json（2026-10-08 09:46-09:55 观测） | cursor 系生产权限（allowlist 仅 Shell(ls)）无法内部 shell——修复需用户决策全局配置，本 Goal 明确不改；zcode-verifier-strong 未被路由触达=如实未验证 |
| 6 | 用量区分原始字段/归一化/缓存/未缓存/输出/缺失；不重复累计；controller/worker/verifier/probe 分账；处理量不冒充账单 | **达成** | T010 18d10f4：normalized_usage（zcode/codex 缓存⊂input、cursor 独立桶）；session_tree 去重；dispatch.usage 五本账；schema v12 增量迁移；`orx usage zcode` 回填实证（attempt 159=152 请求 31760702/76608/31552768 exact，2026-10-08 10:58）；防双算活体证明（2026-10-08 13:00 批量回填被子树重叠正确拒绝）；NORMALIZED_NOT_BILLING_NOTE 全 surface | 本 run host attempts 覆盖缺口如实：根会话绑定为 worker attempt 后子树失去逐 child 回填资格（防双算的代价）；controller 账本终端常 unbound=诚实缺失；cursor cacheWriteTokens 未入库（incident 恒 0） |
| 7 | 对照实测证明上下文治理效果；保留正确率/验收/恢复；公布原始指标与限制；可配置软提示+有界预算 | **达成** | T011 cef9e7f：静态 host scaffolding -32.0%（measure.py 可重跑）；真实 A/B 一次付费启动（codex 处理量 89745→85963=-4.2%，正确率保持：passed/一行修复/1 启动 1 检查轮）；budget.* 分层可配（soft 8M/硬停 50M-task、150M-run，阈值引实测样本，未知≠0）；check_timeout_sec=600 诊断长挂有界；parser 线性回归钉住；限制原文公布（N=1 噪声、代表性任务未付费重跑、-32% 纯静态） | 代表性开发任务改后=静态测量+声明，无完整付费对照（成本边界） |
| 8 | 新增回归/全量/打包迁移通过；完整实现+可重取证据+修正后事故结论+独立验收+逐阶段本地提交 | **达成** | 全量 1036 passed 0 xfail（887→1036，+149 例，零新增 skip）；git diff --check clean；check_release problems=[]（0.3.1、资源 6/6、VACUUM INTO v8→v12 链）；逐任务提交 50e68ac→65481b5（12 个）；事故结论修正=docs/cursor-system-analysis-2026-10-07.md（取证版）+ 两次 G009 活体事故准确记录（T006/T012 条目）；独立验收=13/13 任务独立 verifier（本表为 T013）；证据可重取=docs/diagnostics/g009-* 四个目录+冻结回放 | 无版本发布（0.3.1 保持，tag/发布留用户决策）；外部 ZCode memory 勘误在仓库外另行处理 |

**结论**：八条验收全部达成；两个 P0 缺陷由红回放转常规回归并修复；执行期内
两次真实同域配额故障被本 Goal 自建的恢复机制正确处置（观察/恢复/单 writer
全程成立）。Goal G009 满足其完成定义（真实 CLI 与 host 微任务闭环、独立
验收、用量证据齐备且限制如实公布），可判 done。

**后续建议（不在本 Goal 范围，不借此扩权）**：T008 记录的抑制重启用指纹
消费者；cursor 全局权限调整的用户决策；host usage 观测的自动补录；
常驻观察/通知服务的成本收益评估。
