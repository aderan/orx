# 待执行 Goal：ORX 交付、恢复与成本治理（2026-10-07）

仅交接计划，尚未创建live Goal或启动实施。权威Goal与IR见 `docs/diagnostics/recovery-hardening-2026-10-07/`，任务细节见 `docs/recovery-execution-goal-2026-10-07.md`。接手后写入真实Goal ID；保留以上历史章节。

## Stage 1: 先修复交付与driver分发
**Goal**: 完成 T001–T003。
**Success Criteria**: 详见权威IR各任务验收；阶段独立检查与本地提交完成。
**Tests**: 详见IR verification，实施时先red再green。
**Status**: In Progress（R009：T001–T003 已全部交付，见下方轮次记录；阶段终验待独立 verifier）

### G009/R009 轮次记录

- **T012（2026-10-08 交付，attempt 163）**：执行兼容、打包、迁移及全量
  验证，整理使用文档。零生产代码改动、零付费调用。(1) 文档回归先行：
  T010 已把读取契约推到 schema v12（gate SQL 8–12），但 README 观测表述、
  Docs 索引与 docs/package-acceptance.md §3 升级链描述仍停在 v11，其中
  README 允许清单被 test_delivery_gate_e2e 钉成"当前事实"——测试先行更新
  （加 12、钉旧串 "8, 9, 10, and 11" 不得残留）对未修 README 实跑红，再修
  文档转绿；README 升级提示框 "migrates to v11" 改诚实双值（发布 0.3.1→
  v11、当前未发布树→v12——同版本号下源码树与发布包 schema 已分叉）；
  package-acceptance 升级链描述随 CODE_SCHEMA_VERSION 锚定改写并引用实测
  拒读原文（`schema version 12 is newer than supported version 8; upgrade
  orx`），改前文档与机器行为不符（链已升 v12 文档仍写 v11）；README 不加
  新节（G009 新命令面成文在 routing-strategy Recovery/预算节、
  observability-contract 第六修正案与报告各条目，不复制第二份）。
  (2) 兼容/打包/迁移全走既有机器：全量 1036 passed 0 xfail（START GATE
  首轮与终验两跑；docs/m1.2-report.md 纯生成差异按约定还原）；
  scripts/check_release.py 全链 exit 0 problems 空（uv build --wheel 临时
  目录、干净 venv、包内资源探针、preset 保语义、全部打包技能逐字节漂移
  刷新、stand-in Goal 闭环、fixture 再生核对、v8→12 逐行身份+真实 v0.3.0
  拒读+备份恢复）；版本保持 0.3.1、无 tag 无发布（硬边界）；sdist 追加
  一次性验证（repo 布局 11 个资源文件、sdist→wheel 复构含全部 6 个打包
  资源）；v12 迁移四例+test_m12 legacy 链+包验收链全在全量内跑过，无缺口
  未无理由重复（T011 对 skills/orx-controller、skills/orx-agent、agents/
  orx-worker.md 的资源改动由包验收链逐字节自动受验）。(3) 诚实审计：
  git diff 5589242..HEAD 无 skip/skipif/xfail 新增（唯一命中=T001
  xfail(strict) 脚手架注释文字，标记本体已由 T002/T003 移除，当前 tests/
  零 skip/xfail 标记）；仓库无 formatter/linter 配置如实说明、不引入工具
  凑检查；事故勘误只在仓库（外部 ZCode memory 超范围）。
  docs/recovery-hardening-report.md 补 T012 条目+头部修正（终验指针
  T012→T013、T001–T005 记录位置说明）。证据
  docs/diagnostics/g009-t012-packaging-2026-10-08/（check-release.json、
  sdist-build.log、sdist-contents.txt、wheel-from-sdist.log）。

- **T011（2026-10-08 交付，attempt 161）**：用对照实测压缩 controller 与
  worker 上下文，形成预算策略。四件落地：(1) worker 提示词压缩零信息
  损失——每条验证条目从出现两次（清单+preflight 行）合并为一次（preflight
  行即清单，"How your work will be checked" 标题不变）；host_mode=
  subagent 的停泊改携 brief 交付契约引用（agents/orx-worker.md 由 harness
  注入且逐字携带四条款——省掉每提示词 ~1.8KB 的纯重复），CLI/external/
  self 保留完整内联契约+CLI 机器交付条款；verifier 提示词本就轻不动。
  skills/orx-controller 与 agents/orx-worker 同步双形态契约说明+预算
  停止语义。(2) 预算策略分层可配（env>project>user>builtin，全部可
  config set）：budget.soft_warn_task_processing_tokens=8M（微任务 ~90K
  处理量、host 原样 659,724、a130 一小时 25.74M——早于病理区晚于正常
  工作）/hard_stop_task_processing_tokens=50M（T003 全链 ~44M：放行
  a130+a134 合法链阻断二次延续）/hard_stop_task_attempts=8（T003 用
  6）/hard_stop_run_processing_tokens=150M（全会话 ~155.8M）；归一化
  沿 T010 词汇非账单；未知用量绝不补零（attempts_unassessed 计数+
  attempt 门禁兜底、无语义行计 attempts_unnormalized 不入和）；硬停
  在 run_slice 派发前三处留痕（routing_errors.budget+task event
  dispatch_budget_stopped+恢复点说明），任务保持 runnable 一切已记录
  事实可查=恢复点即调阈值；软告警只注记（task list 每任务 budget 观察
  +run 顶层 budget）；构建在 T004 抑制缝与 T006 不删事实上。(3)
  runtime.check_timeout_sec=600 诊断长挂边界：check/门禁/verifier 命令
  五处调用点换用（启动仍用 command_timeout_sec）——a130 一次 pytest
  卡顿 ~42 分钟静默等待的形态变为 600s 有界红结果；既有超时语义测试
  迁移到新键。(4) 解析器线性回归钉住（修复已在树）：200KB 无括号
  slug 流 <5s（修复前实测 110s）+倍增 <3x+带括号样例功能正确，引用
  a130 事故，不再列为待修根因。对照实测（可重复，基线=已记录实测不
  重复付费）：静态受控（measure.py，state 只读副本，旧渲染器父 commit
  逐字引用与归档提示词交叉验证 ~129B 行折叠差）——host subagent
  scaffolding -32.0%（5,639→3,836B/任务）、CLI 形态 -4.6%；真实 A/B
  恰 1 次付费启动（Controller COST BOUND 上限）：同形微任务同 profile
  codex-frontier——passed、一行修复（sub +→-）、1 启动 1 检查轮、
  input 89,121→85,430（-4.1%）、cached 78,336→75,776、uncached
  10,785→9,654、output 624→533、处理量 89,745→85,963（-4.2%）、
  墙钟 61.9→70.0s；代表性开发任务改后=静态测量+诚实声明（host 工具
  schema 非 ORX 生成未测量、完整付费重跑未执行、单对 A/B 的 token 差
  N=1 噪声主导不可归因压缩单独贡献、host subagent -32% 纯静态——
  平台禁孙代）。14 例新增先红后绿；全量 1036 passed 0 xfail
  （基线 1022+14）；付费启动恰 1 次。详见
  docs/recovery-hardening-report.md T011 +
  docs/diagnostics/g009-t011-context-2026-10-08/（measure.py+
  static-measurement.json+ab-run/ab-evidence.json）。

- **T010（2026-10-08 交付，attempt 159）**：统一 provider 用量口径并补齐
  host/controller/probe 观测。六件落地：(1) adapters.base.normalized_usage
  展示层统一词汇——zcode/codex=cache_subset（input 已含缓存，
  processing=input+output，加缓存即双算；uncached=input−cached，矛盾时
  None 不裁剪）、cursor=cache_separate（input/cacheRead/output 三桶相加，
  cacheRead>>input 正常）、shell/未知零主张；缺失字段 None 不为零；
  NORMALIZED_NOT_BILLING_NOTE 固定"处理量非账单"。(2) zcode_sessions
  只读会话树聚合：session_tree（parent_id 递归）+model_usage_aggregate
  （(session_id, logical_request_id, attempt_index) 去重、error 全零行计入
  error_rows_zero_tokens=零是占位非免费、computed_total=input+output 缓存
  从不叠加）；子树=自身+后代，控制器父会话永不混入 worker 本。
  (3) discover_attempt_session 目录匹配 realpath 归一——T009 活体缺口
  （/tmp↔/private/tmp 别名）修复，确定性 symlink 测试钉住。(4) state v12：
  usage source CHECK 加宽 zcode_db（backup-replace 迁移不回填；单例语义
  host_report/zcode_db 共享 _usage_record_singleton：相同幂等不同冲突）。
  (5) orx usage 新增 attribution 五本账：roles 按 attempt.role 分账带
  missing_reasons/unassessed 分布与 normalized 部分和+incomplete 计数；
  controller 本=根会话自身只读活查（子代理归各自 attempt、无主后代单列
  unattributed）；probe 本恒在且注明空是设计；unbound/unavailable 显式
  缺失不为零。(6) 补录入口 orx usage zcode：仅 host zcode attempt，session
  =显式>绑定>一次 nonce 发现（绝不读 ORX_SESSION_REF 防控制器子树记入
  worker 的双算），重叠双向拒绝，无会话记 host_session_unbound，error 零值
  行→accuracy=estimated。15 例新增先红后绿（normalized 6+e2e 归因 5+
  迁移 4，全确定性伪 zcode 库零付费调用）；门禁两文件 75 passed
  （check 2/3 轮）；全量 1022 passed、0 xfail（基线 1007+15），
  docs/m1.2-report.md 按约定还原。schema 11→12 涟漪随版本更新：
  observability-contract 第六修正案（gate 放行 12、consumption 权威序
  native_cli>zcode_db>host_report>output_estimate、miss 词表+
  host_session_unbound、归一化展示规则）+gate 测试+21 处断言+包验收链
  期望锚定 orx.state.CODE_SCHEMA_VERSION（scripts/check_release.py 为
  test_package_acceptance 导入执行的验收基建，按 tests 桶处理、交付报告
  显式披露）。已知限制如实记录：cursor cacheWriteTokens 未入库（incident
  中恒 0）、controller 本依赖 ORX_SESSION_REF（CLI 终端常 unbound=诚实
  缺失）、本 attempt 自身 ORX usage 仍缺待控制器补录。详见
  docs/recovery-hardening-report.md T010。

- **T009（2026-10-08 交付，attempt 157）**：运行隔离真实微任务，验证投产
  profile 与切换接手者。host_context 任务由控制器会话亲自组织：隔离仓库
  /tmp/orx-g009-micro（预置红测试+一行修复，stdlib unittest），四次真实
  启动各 profile 首轮一次：(1) cursor-strong 真实 CLI 54.3s——T007 预检先于
  付费启动即从真实配置预测 allowlist Shell(ls) 阻塞，启动证实，T002 语义下
  blocked 正确分类（零改动、exit 0 不覆盖、delivery blocked (environment/
  tool blocked): 前缀）；(2) codex-frontier 61.9s 完整 CLI 闭环（一行范围
  内修复→agent 内 unittest 绿→结构化 passed 交付→控制器门禁复跑 OK→task
  passed，usage 89121/624/78336 native_cli exact）；(3) zcode-worker 真原生
  子代理 58s 完整 host 闭环（start gate 红→一行 mul 修复→绿→门禁复跑→R002
  done）；(4) zcode-verifier-flash 56s 独立验收 pass（自行重跑测试、核对
  diff 范围与 .pyc 一致性）。其余 profile 状态见 evidence.json（cursor 同
  权限域不重复启动；zcode-verifier-strong 未触达如实未验证）。观察到缺口交
  T010：host attempts 无 usage 观测 + discover-session 未绑定（疑 /tmp 与
  /private/tmp 项目根别名）。cursor 权限修复需用户决策全局配置，本 Goal 不改。
  原始证据 docs/diagnostics/g009-microtask-2026-10-08/。

- **T008（2026-10-08 交付，attempt 155）**：建立可重复的故障注入与组合验收矩阵。
  仅测试与文档（生产代码零改动）：以验证计划 §第二层 12 行矩阵 + GLM 1308 注记 +
  退避切换组合 + 探针适用范围为清单，逐行定位 T001–T007 用例（含 T006 的
  tests/test_host_recovery_facts.py——不在门禁命令内但作矩阵证据引用），
  补 7 例确定性组合测试后把全表回写 docs/recovery-validation-plan-2026-10-07.md
  （新增"第二层执行矩阵：逐行证据定位"节，文件::用例 + 每行有限动作）。
  新增组合：(1) 认证失效 e2e——auth 不属资源换挡类，恰一次 attempt、无同 slice
  换挡、无登录循环、门禁跳过、retry 后下一 rung 通过（test_health）；
  (2) 中文 GLM 1308 额度 × driver——host 停泊分支的额度类补位（test_routing）；
  (3) host × 权限抑制——`orx task complete` 的同条件 blocked 三次后不再停泊，
  三处留痕；(4) 抑制按任务域——T001 抑制后 T002 仍获该 profile（独立 streak）；
  (5) 同 profile 配额 × override——学习 exhaustion 让位操作者 override，clear
  恢复学习；(6) 控制器同故障域——planner/worker 同 quota_domain 时 plan_route
  有界 RoutingError 如实停留，恢复者（override）出现才重开 planning；
  (7) 陈旧身份 × 换挡——资源换挡停泊 host 后死亡 rung 迟到交付 stale 拒绝、
  新 owner 唯一 completion。注入纪律钉住：仅 fake 进程/提供商/时钟，被测
  dispatch 不 mock（独立 verifier 按矩阵逐行复核）。发现并记录生产缺口（未
  修复，不在本任务写权限）：抑制的重启用缺少 T007 配置指纹消费者——被排除
  profile 无法记录异指纹结果来自破尾部 streak，重开只能靠控制器改路由/配置，
  见 docs/recovery-hardening-report.md T008。门禁六文件 189 passed
  （基线 182+7）；全量 1007 passed、0 xfail（基线 1000+7）。

- **T007（2026-10-08 交付，attempt 153）**：增加执行能力预检与验证结果的配置指纹。
  事故缝隙（a131/a132/a133）：控制器侧 PATH/denylist 检查与配置文件读取被当成
  "worker 能执行 checks"——真实 Cursor 配置 approvalMode=allowlist、shell allowlist
  仅 Shell(ls)、sandbox disabled，argv 有 --trust 无 --force，`ls` 能跑而
  uv/orx/pytest 在 agent 内被拒。落地四件：(1) `probes.build_permission_preflight`
  只读读取 harness 自己读的真实配置（cursor `~/.cursor/cli-config.json`；
  codex `CODEX_HOME`/`~/.codex` 的 config.toml+hooks.json），五轴分别取值各带来源：
  trust / force|yolo / approval_mode / sandbox / project_hooks——argv 轴经
  adapters 的 `permission_argv_axes` 镜像（cursor --trust 恒真、--force 仅
  profile.force、--sandbox 从不传；codex -s 按角色 workspace-write/read-only、
  bypass 旗标从不传、trust 在 projects 表），config 轴带文件路径与键名；
  缺文件/缺键=unknown 绝不默认允许或拒绝；cursor authInfo 与账号缓存根本
  不打开，codex hooks 只存事件名不存命令，存储仅指纹+轴值+来源。(2) 明示
  可证范围：proves=CLI 身份+版本+各轴值与来源；cannot_prove=agent 会话内部
  shell 执行（控制器侧 PATH、denylist、help 输出都不是 in-agent 审批），
  且绝不自动清除额度 cooldown/exhaustion 或人工 override（T005 缝合，
  build 全程零健康写入，测试钉 exhausted+naive reset+override=1 不变）。
  (3) 指纹 sha256 绑定 CLI 二进制+版本/profile/model/effort/role/五轴值与
  来源/workspace/--check 首 token（控制器侧 PATH 命中显式标注证明力为零）/
  各源 axes 内容摘要；probed_at 随记录但不在哈希内（同环境可复现）；
  失效=指纹变化或任一源 mtime 变化（touch 同内容也退役先前结果），
  `preflight_invalid_reasons` 给出原因，持久化于
  `<data>/probes/preflight/<harness>-<profile>.json`。(4) CLI `orx agent
  preflight HARNESS [--profile --role --check]`（prior 有效性对照渲染，
  --json 带 prior/prior_valid/invalid_reasons/persisted_path）与 doctor
  `permissions:cursor|codex` 检查（可读=ok、缺失=warn，逐轴展示并声明
  "proves nothing about in-agent shell execution"；byte-compat 的
  doctor_harness_checks 未动）。测试 16 例新增先红后绿：test_probes 11
  （事故形状指纹+无执行声明、秘密/账号标识不入库、四轴独立翻转、缺配置
  unknown 不默认、控制器 PATH 命中≠内部 shell、help/探针成功≠权限、
  指纹全绑定失效矩阵+持久化、额度/override 免疫、shell/zcode 拒绝、
  codex 真实轴+角色、doctor 检查）、test_adapters 2（两 adapter 镜像与
  真实 launch argv 逐旗标对账）、test_cli 3（命令端到端含失效对照、
  profile 错误路径、未知 harness）。真机只读冒烟：cursor 实配置读出
  allowlist/Shell(ls)/disabled/--trust-adapter/--force-profile 四轴分离；
  codex 读出 projects trust=trusted、approval_policy 缺失=unknown、
  hooks 八事件名。门禁两文件 90 passed；全量 1000 passed、0 xfail
  （基线 984+16），docs/m1.2-report.md 全量重跑后按约定还原。

- **T006（2026-10-08 交付，attempt 151；接续 attempt 150 的配额中断半成品）**：
  补齐宿主故障事实、可信观察与安全恢复入口。
  本任务第一次派发（attempt 150）自身成为活体证据：claim 22:41:40 +0800、
  心跳 #1 22:41:55、会话最后活动（真实故障时刻）22:55:05.238（43 模型步/9 Edit/
  1 Write，即树中半成品）、GLM 1308 5h 窗口击中 worker 与控制器同一 provider 账号
  （重置串 "2026-10-07 23:26:04" 无时区逐字保留，恢复时刻未知=跨 slice 不自动放开），
  控制器（launcher 直接观察进程退出）于 08:32 窗口重置后 fail+retry 改派 151——
  关闭落后故障 9h36m55s，墙钟跨度不代表模型运行时（13m25s）。已记录 reason 近似
  "~22:42"，机器读取精化 22:55:05，原记录保留（correction 语义）。
  四条缝：(1) `zcode_sessions.session_facts` 单会话只读终态事实
  （created/last_activity/archived/last_part epoch 原值从不再定时区 +
  model_steps/edit/write 计数，列缺失=json1 缺失如实 unreadable 绝不补零；
  每事实带来源，verdict 恒 None）；(2) `dispatch.task_observe` 任意状态可用的
  只读诊断（session_ref 优先、nonce 只读回退不落库；claim/progress/closure 三组
  事实+来源；activity_after_last_report 布尔事实；separation 显式三钟
  真实故障/观察/关闭；全路径零写入，测试钉 ORX 状态快照+zcode 库字节不变）；
  (3) `dispatch.task_recover` 显式恢复（必须操作者确认终止事实 1-512 字符，
  空白=usage exit 2，ORX 永不判死永不由心跳静默认定死亡；仅 running+开放 host
  worker attempt；failure_reason=操作者事实+机器读取行 claimed_at/session/
  last_activity/edits/writes/model_steps/heartbeat/closed_at——a130 误报防护，
  "never claimed/no changes" 再不能与证据共存；先关闭再开 dispatch=单 writer，
  迟到交付 stale、迟到报告 closed/superseded、旧 nonce 永不覆盖新 attempt）；
  (4) `dispatch.task_note` 封闭词表追加通道（shared_failure 同故障域可解释停留：
  开放 attempt 保持所有权、迟到交付仍可受理、无自动继续；correction 追加引用
  原 attempt 逐字引用其记录、绝不 SQL 改库；均只写一条 task event 零状态移动）。
  门禁修正：`task_complete` 引用 attempt 时身份 stale 诊断先于状态门
  （证据校验先于身份的既有排序契约不变；未引用路径不变）；心跳门禁顺序不动
  （冻结——非 running 任务状态门先拒 task_not_running，与既有 e2e 先例一致，
  新 attempt claim 后才 attempt_closed）。CLI 三面 observe/recover/note 与
  run 恢复提示一致落地（输入类 exit 2：termination_fact_invalid/note_kind_invalid/
  note_detail_invalid/note_attempt_required；--json error={reason,message} 与
  heartbeat §5 同律共用渲染）。skills/orx-controller 恢复章节重排 1-6 步
  （有界等待→先读事实→确认死亡才 recover 需 termination fact→绝不第二 writer→
  同故障域停留→correction 追加），红线逐行测试钉住。docs/recovery-hardening-report.md
  建立（本 Goal 实施报告，T006 条目含三次钟表、attempt/session/nonce、配置指纹、
  用量来源、缺失字段）。tests/test_host_recovery_facts.py 17 例全确定性（伪 zcode
  库+冻结时钟，零付费调用）；门禁三文件 73 passed；全量 984 passed、0 xfail
  （基线 967+17）。

- **T005（2026-10-07 交付）**：校准额度故障域、退避时间与未知状态的路由规则。
  事故校准三线（docs/cursor-system-analysis-2026-10-07.md 的事实基线）：
  (1) **时区诚实**——`quota.provider_quota_reset` 返回远端 provider 打印的重置时刻
  逐字原文（无时区字符串保持无时区：不补 Z、不锚定本地时区；provider 自己印的
  offset 原样保留；重置标记须在同行邻近，裸时间戳不算），`health.quota_reset_tz_source`
  标注 offset/naive；`quota_exhaustion_active` 对 naive/缺失/不可解析的重置一律保持
  门禁（恢复时刻未知=永不自动释放，虚拟时钟两侧均不释放），`_health_reason` 显示
  "timezone unknown (explicit re-eligibility required)"。`orx resource clear` 在
  exhausted 行上成为真正的显式恢复入口（override→0 且状态→unknown 候选，G005
  docstring 承诺的语义落地）；live preflight 报健康窗口同样构成显式再合格。
  (2) **探针成功≠额度充足**——`record_attempt_outcome(ok=True)` 命中"活动中的
  exhaustion"（未来或不可比较的重置）时只记录成功事实（last_success_at/streak 清零/
  last_error_kind 清空），不清门禁、不清 quota_reset_at；08:47 trivial 探针语义封死。
  cooldown（限流）与 auth 为每请求二元事实，成功照常恢复（语义不变）。override 行
  一切自动写入免疫（resource_learn 拒写，本次测试封口）。
  (3) **共享账号故障域**——profiles.toml 可选 `quota_domain` 键（无则独立；空值/非串
  报错），同域任意成员的活动 exhaustion 使整域不可路由：sibling 以
  `reject_reason=quota_domain_exhausted:<domain>` 入 routing_decisions 审计（其自身
  行健康——死的是共享额度，不伪装容量），域锚点重置过期（可比较且已过）时随锚点
  一起放开为候选；候选自身 override 豁免域门（operator 明示换账号时操作者赢）。
  provider 中文词汇进 `quota.provider_error_kind`（额度/配额/余额不足→quota、
  请求过于频繁/限流→rate、未登录/登录已过期/鉴权失败→auth、1308 仅按错误码形状
  匹配防数字误报），`health.resource_error_kind` 组合分类（adapters 英文模式优先，
  process_failure 时才升级中文词汇；`failure_class` 同一分类器，T004 真值表补两行
  中文 resource 例）。`dispatch._record_attempt_health`：分类升级 + 重置来源顺序
  （本地 CLI 渲染的 offset 可比较形式优先，其次远端 provider 逐字原文）。
  注入矩阵全部确定性（虚拟时钟=store 内伪造就绪值，零真实耗尽）：中文 GLM1308 端到端
  （真实 dispatch 栈、原始消息进 failure_reason、逐字 naive reset、跨 slice 永不自动
  放开、agent_status 时区标注、operator set 显式恢复）、共享域 sibling rung 跳过
  （reroute 不落同域、路由审计留痕）、全部不可用有界可解释停止（单错误列每个
  rung 原因、任务留 runnable、二次 slice 同样有界）、reset 前后（前不选、后仅候选
  unknown 永不虚假 available）、override 优先（域门/探针/预检三路免疫）。
  tests/test_quota.py 重校准 `test_success_clears_quota_reset` 为活动期保门禁+
  过期恢复双段，新增 6 例；test_health.py 新增 5 例+真值表 2 行；test_routing.py
  新增 6 例（域解析/非独立容量/域随 reset 过期放开/override 豁免/全不可用有界停止）；
  test_config.py 新增 quota_domain 解析与校验 2 例。门禁三文件 110 passed；
  全量 967 passed、0 xfail（原 948 + 19 新增）；docs/m1.2-report.md 全量重跑后
  按约定还原。

- **T004（2026-10-07 交付）**：建立失败类型、同条件 blocker 抑制与有界 check-fix 机制。
  恢复词汇 `health.failure_class`（permission/environment/code/resource/other，仅从已记录
  事实分类：blocked 交付 + 权限标记 → permission；blocked 其余 → environment；delivery
  failed / 红门禁 → code；quota/rate 文本 → resource（T005 域，仅归类不处置）；protocol_error
  与无模式崩溃 → other 永不抑制）；blocker 条件指纹 = sha256(归一化 reason 核心 + profile +
  harness + class)[:16]，归一化剥离 ORX 交付前缀与 `exit N:` transport 前缀（同一条件经不同
  信封记录指纹相等）；`health.blocker_streak` 数 (task, profile) 尾部同指纹 blocker 连续数，
  遇该 profile 的成功/异类失败/异指纹即断（条件确变则诚实重置）。
  有界启动门：新配置 `worker.max_same_blocker_attempts`（默认 3，逐层校验、`orx config set`
  可写），达限后 run_slice 以任务级 `exclude_profiles` 排除该 profile（routing.RouteRequest
  新增任务域排除，candidate reject_reason=`suppressed_blocker:<name>` 入 routing_decisions
  审计；profile 对其他任务仍可路由），梯次照常下落到下一 rung（含 T003 的按 driver 分发与
  `_reroute_resource_failure` 同样尊重排除）；无可路由档时停止并三处留痕——run 输出
  `routing_errors[].suppressed`（class/fingerprint/count/limit/blocker/remedy）、
  `dispatch_suppressed` task event、以及 task_retry 载荷 `suppressed_blockers`（重试转换仍
  可用，操作者意图不变，但控制器预先得知 run 不会再启动该 profile）。remedy 文案只指向
  条件变更：config change / routing change / human，绝不"再试一次"。代码红（code 类）永不
  计入抑制——修复路径完整保留（既有 max_check_rounds 会话内循环 + 带修复包的 retry）。
  同会话延续为实测事实（2026-10-07 只读 help 取证，零付费调用）：cursor 3.23.12
  `cursor agent --resume [chatId]`/`--continue`；codex-cli 0.160.0 `codex exec resume
  [SESSION_ID] [PROMPT]`（非交互可用）；shell harness 为裸可执行文件无会话概念；zcode 为
  host harness（会话延续即宿主会话模型）。`adapters.base.harness_resume_supported` 携带该
  事实，blocker 类失败的重试提示追加能力诚实行：可 resume 的 harness 指明可续同会话并给出
  命令；无会话 harness 明言每次都是新进程、修复包即全部上下文（a132 无行 blocker 同样获得
  分类指导——无 retained 行的 legacy 块仅对 blocker 类例外追加指导，code/other 保持字节兼容）。
  `_classify_prior_failure` 改用共享分类法（单一分类器）。tests/test_health.py 新增分类真值表
  （13 例）、指纹确定性/条件键控、streak 尾部计数与重置矩阵；tests/test_routing.py 新增
  事故端到端复刻（3 次 blocked 后第 4 轮不启动：attempt 仍 3、task 留 runnable、三处留痕
  断言）、下一 rung 下落（oksh 执行通过且不触第 4 次 cursor）、代码红永不抑制（4+1 次照常
  启动）、限额可配置（=2 时第 3 轮即抑制）、task_retry 载荷、RouteRequest 排除单元；tests/
  test_delivery_gate_e2e.py 新增 cursor/shell 两分支的 resume 能力提示契约。docs/
  routing-strategy.md 新增 Recovery 章节；skills/orx-controller 补抑制停止信号（不是代码
  问题，改变条件而非重试）。门禁 4 文件 114 passed；全量 948 passed、0 xfail（原 924 +
  24 新增）。

- **T003（2026-10-07 交付）**：资源失败后的下一档按 driver 分发，补齐有界切换与身份绑定。
  `dispatch._reroute_resource_failure` 由无条件 `_execute_cli_task` 改为按下一 rung 的 driver
  分发：cli → 本 slice 执行（原路径不变）；host → `_park_task` 正式宿主指派（waiting_host +
  指派文件携带自己的 ORX_ASSIGNMENT nonce 与新 attempt，与 `orx run` 停泊共用同一条代码）；
  external → 正式外部契约（waiting_external），绝不冒充 adapter。`run_slice` 按返回的
  (bucket, entry) 归类输出（started/failed/host_required/waiting_external），停泊任务计入
  parked_this_slice 防重复上报。有界性：每任务每 slice 至多一跳、失败档已不可路由不会重选
  （无环）；身份绑定：新档为新 attempt+新 nonce，失败 CLI attempt 原样保留历史。
  tests/test_health.py 唯一 xfail(strict)（T001 冻结的 CLI→host 红基线）按约定转常规绿；
  tests/test_routing.py 新增分支矩阵：CLI→host（事故复刻，断言 host_required/profile/nonce/
  prompt 锚点/attempt 历史）、CLI→external（仅正式外部契约，无 claim 不冒充）、单跳有界
  （双限流 rung 一 slice 恰两 attempt 后停 FAILED，显式 retry 后经 run_slice 恢复）、
  host→CLI 与 host→host（经 task fail 正式结束事实 + 资源耗尽事实 + 显式 retry，由既有
  run-slice 机器按 driver 再分发，新档新身份、旧档留痕）。冻结证据回放
  docs/diagnostics/cursor-2026-10-07/replay_cli_to_host.py 由红转绿
  （RED: no adapter for harness 'zcode' → GREEN: host assignment produced，exit 0，
  脚本未改动）。门禁 tests/test_routing.py+test_quota.py+test_host_progress_e2e.py+
  test_delivery_gate_e2e.py 89 passed；全量 924 passed、0 xfail（原 918 passed + 1 xfailed）。

- **T002（2026-10-07 交付）**：接通 CLI 结构化 delivery 与正式门禁，修正健康学习语义。
  五个事实分层落地：进程结果（transport）、harness 响应结果（adapter `response_error`：
  cursor 错误信封 / codex error 事件）、worker 交付结果（`adapters.base.
  extract_cli_delivery` 从最终消息解析；候选文档经 `verify.load_delivery_evidence`
  校验——单一 schema 所有者，不复制规则）、控制器命令门禁（仅 passed 交付进入）、
  独立 verifier（不变）。
  机器契约写入 CLI worker 提示词（FINAL DELIVERY MESSAGE：末尾一个 fenced json 交付块；
  退出码是 transport 不是判决）。行为：blocked/failed 交付按 host 同款
  `delivery blocked (environment/tool blocked):` / `delivery failed (worker-reported failure):`
  前缀关闭 attempt 并判 task 失败（不跑门禁，交付文档落盘为 completion evidence）；
  缺失/畸形/自相矛盾/信封反驳 passed 的交付显式 `protocol_error`；usage/session 全程照常记录。
  健康语义：exit-0 + blocked/protocol_error 不学习（不清 failure streak/cooldown、不判账号故障）；
  代码红（passed 交付+红门禁）不影响 profile 健康；passed/failed 交付照常学习成功。
  adapter 词汇：`DELIVERY_CHANNEL_MESSAGE`（cursor/codex）/`DELIVERY_CHANNEL_EXIT`（shell，
  兼容边界：退出码即交付通道，不套消息协议）。T001 的三处 xfail(strict) 标记全部移除、
  测试转常规绿；新增 e2e 正向/传输/提示词契约、解析器单元矩阵、健康语义、
  codex/shell 边界与 response_error 事实测试，并按真实输出形状补齐 cursor/codex 替身 fixture。
  skills/orx-agent 与 agents/orx-worker 同步 CLI 最终消息交付契约。全量 918 passed + 1 xfailed
  （唯一 xfail 为 T003 范围的 CLI→host 退避切换基线，未动）。

## Stage 2: 有界退避、修复及宿主恢复
**Goal**: 完成 T004–T006。
**Success Criteria**: 详见权威IR各任务验收；阶段独立检查与本地提交完成。
**Tests**: 详见IR verification，实施时先red再green。
**Status**: In Progress（R009：T004–T006 已全部交付，见上方轮次记录；阶段终验待独立 verifier）

## Stage 3: 建立提前验证能力
**Goal**: 完成 T007–T009。
**Success Criteria**: 详见权威IR各任务验收；阶段独立检查与本地提交完成。
**Tests**: 详见IR verification，实施时先red再green。
**Status**: Done（R009：T007–T009 全部交付，见轮次记录；T009 真实 CLI+host 双闭环达成，cursor 权限阻塞如实收口）

## Stage 4: 观测、上下文与预算治理
**Goal**: 完成 T010–T011。
**Success Criteria**: 详见权威IR各任务验收；阶段独立检查与本地提交完成。
**Tests**: 详见IR verification，实施时先red再green。
**Status**: In Progress（R009：T010–T011 已全部交付，见上方轮次记录；阶段终验待独立 verifier）

## Stage 5: 兼容终验与交付
**Goal**: 完成 T012–T013。
**Success Criteria**: 详见权威IR各任务验收；阶段独立检查与本地提交完成。
**Tests**: 详见IR verification，实施时先red再green。
**Status**: Done（R009：T012–T013 全部交付；T013 终审=八条验收证据表见 docs/recovery-hardening-report.md，Goal G009 判 done；未发布版本）

### 收尾（2026-10-08）

- 用户决策两项收尾完成：① Cursor unrestricted 授权（全局配置，备份在案）+ cursor-strong 真实闭环实证（隔离仓 R004，1:53 一行修复全链路 passed）；② host attempts 用量自动补录 `_usage_autobill` 五闭合点 + `[usage] auto_backfill_zcode` 配置门（默认开）+ missing 词表两义 + CLI complete --session 转发修复；全量 1043 passed。详见 docs/recovery-hardening-report.md 收尾节。

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

# G004：重规划差异预检与成果引用（2026-10-05 起；2026-10-05 归档）

Goal：新计划修订生效前，产出新旧任务对应关系与工作分类（已完成只需确认仍有效、
新增工作、确实需要重做＋重做原因），成果按可追溯的对应关系引用而非按任务编号。

**归档状态（T005 终审时记录）**：五阶段全部交付（阶段 3 的 `orx status` 呈现为
显式遗留开放项，未做、未声称）。契约权威文档 `docs/replan-contract.md`；端到端
回归 `tests/test_replan_e2e.py`（本地替身，无付费模型）；README 与两个 skill
已同步真实流程并由测试钉住。G004 的临时计划部分（五阶段待办清单）已并入下方
归档记录；本文件更早的 orx-pbv 历史原样保留。

硬约束（约束原文为据）：
- 不做旧 passed 状态自动继承：R003 复盘中"12M token 浪费在重做"的证据不成立
  （不同计划修订中的同号任务被误判为同一工作），不据此开发自动继承功能。
- M0/M1/M1.2 不变量保持：exit codes 0/1/2、`--json` envelope、additive
  integer-versioned migrations with backup-replace-restore。
- 契约：`docs/replan-contract.md`（权威字段与结构/语义边界）。

## 五阶段实施记录（归档）

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

### 阶段 2：dispatch 接线 — Done（T003，R004）
- [x] 共用差异预检 `dispatch._replan_precheck`：`validate_ir` 结构内检 +
      `validate_replan`（以 `replan_snapshot` 任务行）外检 + prior_revision 必须
      等于当前 active 修订 + 成果绑定检查（命名了本 Run 已记录 evidence 的
      artifact 必须属于已声明来源；错绑定位到任务拒绝，未记录路径留给语义审查）。
- [x] 新命令 `orx plan check --file`（只读）：不创建修订、不取消旧任务、不改
      Goal/Run/规划指派状态；仅落一条 `replan_reports` 审计行（revision NULL）。
      报告双形态（--json envelope + 可读文本）：新旧对应（含重编号）、四类分类、
      重做原因、契约差异（将取消/终态保留/验收覆盖）、旧任务去向、来源状态核对、
      引用问题；失败含分类（category/locus/message）与具体错误，exit 0/1/2 保持。
- [x] 手工 submit 与 CLI planner 自动提交同一门禁：`submit_plan` 单入口生效前
      重新预检（来源状态变化重新判定）；失败抛 `plan.ReplanCheckFailed`
      （PlanValidationError 子类，携带分类报告），原计划继续有效、规划指派与
      planner attempt 原样保留、失败报告落库审计。
- [x] 原子生效：作废旧修订+取消未终态旧任务+新修订任务+`replan_mapping_save`
      +报告绑定+关闭规划指派在单事务内，任一故障整体回滚（无部分生效修订）。
- [x] 修复提前重开：路由重规划不再立即置 planning/active；重开只在新修订落地时
      发生，失败重规划不丢 completed_at。
- [x] 既有重规划夹具显式声明关系（conftest `with_replan` 等字面量助手）；范围外
      夹具最小机械修正并披露（test_replacement_boundary / test_verify /
      test_verify_check / test_subagent_contract / test_observability /
      test_m12_acceptance）。
- 证据：`uv run pytest -q tests/test_revisions.py tests/test_replan_context.py
  tests/test_cli.py tests/test_handoff.py` 100 绿；全量 583 绿（10 失败修复后）。

### 阶段 3：持久化与呈现 — 落库部分 Done（T002，R004）；呈现为遗留开放项
- [x] 存储接口 + 映射/预检报告/可追溯成果来源落库（schema v9 additive 整数版本迁移：
      `replan_mappings`/`replan_task_mappings`/`replan_sources`/`replan_superseded`/
      `replan_reports`/`replan_artifact_sources` 六表；backup-replace-restore 保持；
      v8→v9 失败原库可用、WAL 已提交记录不丢；旧库升级后新表为空，历史缺失保持
      unknown，无回填、无 passed 继承）。
- [x] 多轮追溯与身份规则：来源以 (run, 修订, 任务) 解析并锚定任务行；成果出处保留
      Run/修订/来源任务/attempt/evidence/成果身份，同号任务不同修订或不同 attempt
      不混淆（`replan_trace_chain` 跨轮、拆分、合并、重编号、重新打开可查询）。
- [x] 观测读取契约同步 v9：gate 只接受明确支持的版本（8/9 白名单），既有查询不变，
      新增 replan_correspondence/replan_superseded/replan_artifact_provenance 三查询
      + 旧库构造夹具（seed.sql v9 子集）。
- [ ] `orx status` / 快照呈现新旧对应关系与分类。

### 阶段 4：成果按对应关系引用 — Done（T004，R004）
- [x] worker/verifier 提示词经 `artifacts` 边引用旧成果，不按任务编号
      （`dispatch._replan_reference_context`：分类含义、来源全身份+记录状态、
      来源 evidence 行号/attempt 号、每个成果的解析身份与存在性/变化状态；
      confirm 仅确认适用性+必要回归、redo 展示原因与范围、verifier 端显式标注
      recorded HISTORY 不得冒充当前验证）。
- [x] 交付时保存绑定 attempt 的成果来源与交付快照：`_record_replan_delivery`
      仅在门禁接受的交付上运行——每个解析成果经 `replan_artifact_source_add`
      记录来源任务自身 attempt/evidence 身份；交付快照
      （`deliveries/Txxx-rRR-aAAA.json`，修订+attempt 寻址）记录所有声明成果的
      存在性与 sha256，作为 evidence 行挂在完成 attempt 上；缺失/变化不默认有效
      （UNCHANGED/CHANGED/MISSING 如实展示，历史出处保留）。
- [x] 修复同号任务跨修订的 check 日志覆盖：`orx task check` 日志序列按
      (run, 任务号) 跨修订续数（`Store.verifications_for_task_in_run`），修订内
      布局不变、既有路径保持可读；交付快照按修订+attempt 寻址互不覆盖。
- [x] planner 端贯通：事实快照 evidence 携带行号/attempt 身份并渲染进提示词，
      规划规则要求经对应关系在 artifacts 里引用 evidence 路径。
- [x] 不变量保持：引用与旧通过记录不写 verification 结果（新任务 pending/
      runnable 起步、当前窗口为空、只经自己的门禁+独立 verifier 通过）；G003
      门禁/同 attempt 检查/交付前重跑/独立验证不变；无 schema 版本变化
      （state 仅增读接口）。
- 证据：`uv run pytest -q tests/test_assignments.py tests/test_replan_context.py
  tests/test_verify_check.py tests/test_verify.py tests/test_delivery_gate_e2e.py`
  70 绿（61 存量 + 9 新增）；全量 592 绿。

### 阶段 5：端到端回归与终审 — Done（T005，R004）
- [x] `tests/test_replan_e2e.py`（本地替身，无付费模型）：一次修订切换同时断言
      差异报告、持久化引用链、原子修订切换与新任务独立验证；场景覆盖重编号确认、
      同号不同工作（编号不构成对应，trace 为空）、新增与未完成延续、拆分（part
      边）、合并（多来源）、明确理由的重做（报告与提示词逐字）、多轮成果追溯
      （三轮 + 跨 prior_revision 回溯来源、每边独立成果出处与交付快照互不覆盖）、
      失败预检后旧计划继续执行至 done、确认任务当前回归红则不能通过（旧 pass
      不背书新任务；修复后经自己的检查通过）。
- [x] README 与 orx-agent / orx-controller skill 同步真实流程：生效前预检
      （`orx plan check --file`，submit 重跑同一门禁）、成果按对应关系出处引用
      （绝不按任务编号）、具体重做理由；显式声明历史未知与语义审查边界；
      不宣传 passed 自动继承、不宣传未经证实的 token 节省（R003 数字已撤回）。
      文档由 `test_docs_teach_the_real_replan_flow_and_its_boundaries` 钉住。
- [x] 全量回归绿（见下方轮次记录证据行）；G004 记录归档，临时计划部分并入本节，
      更早历史文档保留。
- 证据：`uv run pytest -q tests/test_replan_e2e.py` 5 绿；全量 597 绿。

## G004 轮次记录
- 2026-10-05 R4 T001：阶段 1 完成并自验（见上证据行）。
- 2026-10-05 R4 T002：阶段 3 落库部分完成——schema v9 additive 迁移 + `Store.replan_*`
  存储接口（对应关系/预检报告/成果出处）+ 多轮追溯 + 观测契约 v9 同步；
  `uv run pytest -q tests/test_state.py tests/test_subagent_contract.py
  tests/test_m12_acceptance.py tests/test_observability_contract.py` 56 绿。
- 2026-10-05 R4 T003：阶段 2 完成——共用差异预检（`_replan_precheck`）+ 原子生效
  单事务 + `orx plan check --file` 只读命令 + 手工 submit/CLI planner 同一门禁 +
  已完成 Run 失败重规划不再提前重开 + 夹具显式声明关系（见阶段 2 证据行）。
- 2026-10-05 R4 T004：阶段 4 完成——引用链解析进 worker/verifier 提示词（存在性/
  变化以交付快照为基线，缺失不默认有效、历史出处保留）+ 交付时绑定 attempt 的
  成果出处与交付快照落库（同号跨修订互不覆盖）+ 同号跨修订 check 日志续序修复 +
  planner 事实快照携带 evidence 身份（见阶段 4 证据行）。
- 2026-10-05 R4 T005：阶段 5 完成——`tests/test_replan_e2e.py` 五场景端到端
  （四维同时断言：差异报告/持久化引用链/原子切换/新任务独立验证，全部本地替身）；
  README 与两个 skill 同步真实重规划流程并声明历史未知与语义审查边界（测试钉住）；
  `docs/replan-contract.md` 阶段 5 状态更新；G004 记录归档（临时计划并入，
  `orx status` 呈现为显式遗留开放项）。全量 597 绿（592 存量 + 5 新增）。

---

# G006：host worker 进展报告与安全恢复观测（2026-10-06 提交）

目标及计划：[docs/host-progress-plan.md](docs/host-progress-plan.md)。
权威任务图：[docs/host-progress-plan.json](docs/host-progress-plan.json)。
Run：R006；五项任务串行，参考工作量 3–5 小时，按验收完成。
实施已完成，本节为归档记录（证据为准，见下）；实测报告：
[docs/host-progress-report.md](docs/host-progress-report.md)。

## 归档状态（T005 交付时记录）

五阶段全部交付，实测报告见 [docs/host-progress-report.md](docs/host-progress-report.md)
（只记录实际运行的命令与结果）。权威契约 `docs/host-progress-contract.md`（T001 冻结）；
端到端替身回归 `tests/test_host_progress_e2e.py`（冻结时钟，无付费模型、无真实 sleep）。
T005 的独立 agent 审查与 Goal 验收以 ORX verification 记录为准，此处不预支结论。

### 五阶段实施记录（归档）

- [x] Stage 1（T001，3a03e15）：`tests/test_session_identity.py` 导入恢复仓库惯例；
      契约冻结（命令/门禁/响应/存储/观察/阈值/无副作用/恢复边界/禁止宣称）。
      证据：全量 627 绿（`evidence-T001.json`）。
- [x] Stage 2（T002，4bd0c4b）：schema v10 additive `attempt_progress` +
      Store 三接口（add/latest/all）+ 只读观测契约 v10 同步；重开一致、迁移失败
      原库可用、WAL 保留、旧库不回填、按 attempt 隔离。证据：指定检查 exit 0
      （`evidence-T002.json`；提交记录 64 项指定测试绿）。
- [x] Stage 3（T003，979b837）：`orx task heartbeat` CLI + `dispatch.task_heartbeat`
      写入接口；输入校验 exit 2、七条所有权门禁单事务重核、全部拒绝无副作用。
      证据：指定检查 exit 0（`evidence-T003.json`；提交记录 107 项指定测试绿、
      全量 646+1）。
- [x] Stage 4（T004，18fef91）：`worker.progress_timeout_min` 分层配置（默认 60）+
      `progress_observation`（当前窗口/闭边界/时钟异常不误报）接入 status/task
      list/recovery 与 timeline `attempt.report` 历史。证据：指定检查 exit 0
      （`evidence-T004.json`；提交记录 164 项指定测试绿、全量 662）。
- [x] Stage 5（T005，本次）：端到端替身复演 §10 两场景（超时提示→恢复重浮现→
      原 attempt 合法迟到交付受理；确认死亡 fail/retry→新窗口→旧 attempt 迟到
      报告 `attempt_closed` 与迟到交付 `stale` 双拒）+ 文档钉 4 测试；
      README/skills 补显式报告时机、有界等待与先查原会话规则（不越 §11 红线）；
      技能安装同步仅经 `orx skill update` 现有入口。证据：全量 668 绿（662+6）、
      `uv build` exit 0（`.orx/runs/R006/check/T005/`）；实测报告
      `docs/host-progress-report.md`。

## G006 轮次记录
- 2026-10-06 R6 T001→T005 串行五任务全部交付；详细实测与命令日志见
  `docs/host-progress-report.md` 与 `.orx/runs/R006/check/T00*/`。
- 未宣称（契约 §11）：定时自动心跳、自动恢复、存活证明、租约、真实时长或
  token 节省数字一律不写；超时提示只建议核查原会话，死亡确认是人的决定。

---

# G007：ORX 0.3.1 发布收尾（2026-10-06 起）

目标：补齐三个必须缺口（wheel 缺 agents force-include、角色定义示范旧
evidence、controller 技能硬编码本机 analytics 路径），补升级与回退说明与
安装包级验收，更新版本号与发布材料后正式发布 v0.3.1，并更新本地安装与
技能。Run：R007；五任务串行（T001→T005），发布渠道沿用 GitHub
aderan/orx push + tag v0.3.1；本地 editable 安装更新走 `orx skill update`。

范围外（明确延后，不纳入本 Goal）：状态页对应关系呈现、后台自动恢复、
并行 worktree 均留后续版本。

## 五阶段实施记录

状态图例沿用：Not Started / In Progress / Done（以证据为准，不以文件
存在为准）。

### 阶段 1：打包补齐 + 角色定义统一 + 包级验收地基 — Done（T001，R7）
- [x] `pyproject.toml`：`[tool.hatch.build.targets.wheel.force-include]`
      增加 `agents = "orx/agents"`（与 skills 同法）；干净安装不再报
      `install manually`。
- [x] `agents/orx-worker.md` TASK 段统一到现行单 assignment 协议：新版
      evidence 示例（status/summary/checks[{command,exit_code,log}]/
      artifacts）、START GATE、BLOCKED EXIT、同会话 CHECK-FIX LOOP、
      DELIVERY GATE、claim/attempt 身份（`--attempt`）、heartbeat 报告
      规则、ORX_ASSIGNMENT 身份锚；旧 `{summary,commands,artifacts}`
      示例删除并声明其被拒。
- [x] `agents/orx-verifier.md` / `orx-verifier-strong.md` 核对只读独立
      判断与两行 verdict 契约（与 orx-agent 技能、dispatch verifier
      prompt 一致），补身份锚段落。
- [x] 示范 JSON 真实校验：`tests/test_delivery_gate_e2e.py` 从
      worker 定义中提取示例 JSON，经 `verify.load_delivery_evidence`
      零错误接受，并在临时 stand-in task 中填充真实 check 行后通过
      交付门禁；角色/技能示例同 schema 钉住。
- [x] clean-wheel 回归：`tests/test_phase_c.py` 钉 hatch force-include
      配置；`tests/test_package_acceptance.py` 构建真实 wheel、临时
      venv 安装、仓库外运行 probe，证明 orx 导入位置在临时
      site-packages、三个角色随包分发且 preset 安装内容与包内逐字节
      一致、无 `install manually` 提示、preset 保留已有文件回归通过。
- [x] `scripts/check_release.py`：构建、临时安装、包内资源与
      preset/skills 检查四段（可独立运行，`main()` 输出 JSON 报告、
      发现问题退出非零），由包验收测试调用。
- [x] `docs/upgrade-0.3.1.md` 起草：旧角色备份（cp 带 .bak-0.3.0）、
      diff 比较、显式替换（mv + `orx preset install zcode` 或手工合并
      后放回）与 preset 保留语义、新版 evidence 示例；数据库迁移与
      回退章节留给阶段 3。
- 证据：`uv run pytest -q tests/test_phase_c.py tests/test_skills_update.py
  tests/test_delivery_gate_e2e.py tests/test_package_acceptance.py` 绿；
  全量 `uv run pytest -q` 绿；`uv build` exit 0；`git diff --check` 干净。

### 阶段 2：controller 看护接入显式配置化 — Done（T002，R7）
- [x] analytics 看护改为明确配置、可选执行的外部调用：可执行文件来源、
      start/status/stop 条件、未安装与启动失败处理、仅清理由当前会话
      启动的看护；移除把本机 analytics 目录表述为固定位置的文字（历史
      测量中的真实路径保留为历史证据）；技能约束回归。
      — skills/orx-controller/SKILL.md 循环步骤 1 重写：可执行文件仅经
      `ORX_ANALYTICS_BIN` 显式解析（无默认位置、不探测），OPTIONAL 且
      不阻断循环（规划/执行/验证/收尾 never blocked），启动失败记一次
      继续，`watch stop` 仅限本会话启动的看护；分析实现维持独立仓库、
      ORX 不内置分析层、看护只观察不自动恢复。docs/observability-contract.md
      去除固定本机路径表述（保留独立仓库声明与 orx-analytics 名称，
      历史路径声明为 history, not defaults）。
      tests/test_delivery_gate_e2e.py 新增 2 项技能/契约约束回归
      （空白归一后钉住措辞，防止换行规避）。
- 证据：`uv run pytest -q tests/test_delivery_gate_e2e.py
  tests/test_observability_contract.py` 24 绿（22 存量 + 2 新增）；
  全量 `uv run pytest -q` 688 绿；`git diff --check` 干净。

### 阶段 3：升级与回退文档完成 — Done（T003，R7；角色部分 T001 起草）
- [x] `docs/upgrade-0.3.1.md` 角色文件备份/比较/显式替换与 preset 保留
      语义、新版 evidence 示例（T001 起草，待 T003 复核）。
- [x] 从 README 链接；补齐暂停写作者、SQLite 一致性备份、schema v8→v11
      自动迁移、旧版本拒读新版库、回退必须恢复升级前备份；分渠道更新
      步骤；`replan --context-file` 要求；README 观测版本改 v8/v9/v10/v11
      （保留历史契约版本语境）。
      — `docs/upgrade-0.3.1.md` §2 分渠道步骤（editable：git checkout
      v0.3.1、明确不做 `uv tool upgrade` 并引用 update.py 的拒绝对原文；
      wheel：`orx update`/`uv tool upgrade orx-agent`）；§6 备份与回退
      （新版首次打开旧库前暂停写作者并备份；`VACUUM INTO`/`.backup`
      一致性快照含 WAL 已提交帧，明示 cp 主文件会漏；连同
      config/profiles 与 `.orx/runs/` 运行材料；v8→v9→v10→v11 纯增量
      自动迁移、复制-替换失败保原库；旧版本拒读原文
      "newer than supported version 8"；回退=恢复备份，never hand-edit
      schema_version，恢复时移除残留 -wal/-shm）；§7 `replan
      --context-file`（可读非空、64 KiB/65536 字节、reason/intent/
      supporting material、分发前校验、代码层可选 vs controller 中途
      重规划先写 context 文件的流程要求，分开表述）。README：Usage 观测
      表述改 v8/v9/v10/v11（gate 恰收 8/9/10/11，保留 v8 立契约、
      v9/v10/v11 增量修订的历史语境）、Skills/update 段升级提示、Docs
      列表链接。tests/test_delivery_gate_e2e.py +5、
      tests/test_replan_context.py +2 文档钉住回归（空白归一，备份先于
      首次打开的顺序、WAL 一致备份、恢复式回退、editable 不走
      uv tool upgrade、skill 与角色文件两入口分开、文档 §3.4 evidence
      示例经真实校验器接受、context-file 限额与报错原文对齐代码常量）。

### 阶段 4：安装包级验收固化（完整链路） — Not Started（T004）
- [ ] 扩展 `scripts/check_release.py` 与包验收测试为完整可重复链路：
      构建最终源码 wheel、临时环境安装、agents/skills/preset 资源检查、
      preset 与全部打包技能安装、已有角色保留与技能刷新验证、仓库外
      stand-in Goal 全流程（规划/执行/新版 evidence 交付/独立 verdict）；
      真实 v0.3.0 schema v8 样例库副本升级到 v11 的逐行核对与旧版本
      拒读验证；不允许 skip 或只验证源码回退。

### 阶段 5：版本、发布与本地更新 — Not Started（T005）
- [ ] 版本单一来源升 0.3.1；四成果发布说明与三个延后事项；README 观测
      表述更新；docs/m1.2-report.md 测量截止时间戳刷新随发布提交；
      全量测试 + 构建 + 包级验收 + 独立审查后 push aderan/orx 并建
      tag v0.3.1（不强推不覆盖）；本地 editable 确认 + `orx skill
      update` 实跑 + 按升级说明备份/比较/更新本地旧角色定义；归档本
      阶段记录。

## G007 轮次记录
- 2026-10-06 R7 T001：阶段 1 交付（attempt 115）——打包、角色统一、
      示范 JSON 真实校验、clean-wheel 回归、check_release 四段、
      升级文档角色章节起草；证据见 `.orx/runs/R007/check/T001/`。
- 2026-10-06 R7 T002：阶段 2 交付（attempt 117）——controller 技能
      看护接入显式配置化（ORX_ANALYTICS_BIN、OPTIONAL、跳过/启动失败/
      清理边界）+ 观测契约去除固定本机路径表述 + 技能约束回归 2 项；
      证据见 `.orx/runs/R007/check/T002/`。
- 2026-10-06 R7 T003：阶段 3 交付（attempt 119）——升级/回退文档完成
      并 README 链接（分渠道步骤、备份先于首次打开、WAL 一致备份、
      v8→v11 自动迁移、旧版拒读、恢复式回退、replan context-file 两层
      要求；README 观测版本改 v8/v9/v10/v11）；文档钉住回归 7 项；
      证据见 `.orx/runs/R007/check/T003/`。

---

# G008：模型选择抽象升级为语义化 ModelSpec（2026-10-07 起；2026-10-07 归档）

目标：将 ORX 模型选择抽象从裸 slug 字符串升级为语义化 ModelSpec 能力
元组 + 可刷新模型目录 + 运行时按 adapter 解析：配置表达语义（家族/
版本/effort/speed/context），解析层吸收 Cursor CLI 的 slug 烘焙、
参数化 bracket 语法与目录演化，执行直达、无需人工 /model/Tune。
Run：R008；T001 起串行。背景实测（2026-10-07 控制器本机）：agent
--list-models 只枚举烘焙 slug（grok-4.7-*/kimi-k3-*/glm-5.2-*/
composer-2.5*/auto），参数化家族（claude-opus/gpt-5 等）可用但不被
枚举，bracket 语法为参数化通道。

范围外（明确不做，见 Goal 约束）：不依赖交互 UI（capability discovery
只用 --list-models/--help/运行回显；Tune 仅背景解释，不写成操作步骤）；
不重写 routing 策略（docs/routing-strategy.md 的分档与角色默认档位
不在本 Goal 内）；不含版本发布（发布由用户另行决定，版本号/tag/发布
材料零改动）。

**归档状态（T005 终审时记录）**：五阶段全部交付。机制权威文档
`docs/model-selection.md`（ModelSpec 形式、目录刷新/查看入口、各
adapter 解析规则、证据登记 E1–E5 与样本标记、诊断规则）；文档示例
经真实加载器与替身回放校验（`tests/test_model_docs.py`）；README 与
routing-strategy 已链接且分档/默认档位未动（测试钉住）。T005 的独立
agent 审查与 Goal 验收以 ORX verification 记录为准，此处不预支结论。

## 五阶段实施记录

### 阶段 1：ModelSpec 数据契约与语义配置加载边界 — Done（T001，R8）
- [x] `src/orx/models.py`（新）：ModelSpec 能力元组——provider、
      family+version 或语义 id（二选一，同时声明即身份不明错误）、
      effort、speed（含 fast 布尔别名，两种拼写同一维度不得同时声明）、
      context（'1m'/'128K'/'5000'/正整数归一为 token 数）、可选模型
      能力要求；固定版本 vs 显式浮动版本策略（具体版本=钉定；缺省或
      version = "latest"=浮动，一律归一为 None）；identity 稳定字符串
      （provider/family@version、family、或原样 id）；to_dict 为
      JSON 可表达的固定键集且可经 parse 往返；模块零 orx 内部依赖、
      不承担 CLI slug/bracket 解析（id 字符串保持不透明，测试钉住）。
      ResolvedModel 解析产物契约（spec/cli_slug/strategy/
      bracket_params/catalog_generation）——本阶段只定契约，适配器
      后续阶段填充。
- [x] `src/orx/config.py`：model 双形态（裸字符串=旧行为原样透传、
      语义 TOML 表）；effort 单一来源（表内 effort 必须与 profile
      effort 一致，冲突报错）；未知字段/非法类型/身份不明/维度重复
      声明错误全部带 profile 与 model.* 字段位置；Profile 增
      model_spec 字段（默认 None，直接构造兼容），model 恒为稳定
      身份字符串（无 dataclass/字典进入字符串契约的路径），
      to_dict 仅语义形态追加 model_spec 键（旧序列化形状逐字节不变）；
      配置分层与完整同名替换行为保持（语义 profile 整体替换测试）；
      migrate_profiles_to_user 语义表以内联 TOML 表写回（不再把
      dict 字符串化）；VALID_EFFORTS 改由 models.EFFORTS 单源派生。
- [x] tests/test_model_spec.py 新增 46 例（契约/归一/版本策略/错误
      词汇/不透明性/往返/ResolvedModel）；tests/test_config.py 新增
      24 例（语义加载与身份、序列化 JSON、旧形状兼容、直接构造兼容、
      错误定位参数化 16 形态、分层替换与补位、迁移往返）。
- 证据：`uv run pytest -q tests/test_model_spec.py tests/test_config.py`
  114 绿（44 存量 + 70 新增）；全量 `uv run pytest -q` 772 绿
  （702 存量 + 70 新增）；`git diff --check` 干净；日志
  `.orx/runs/R008/check/T001/`。

### 阶段 2：可刷新模型目录 — Done（T002，R8）
- [x] `src/orx/model_catalog.py`（新）：本地目录解析层——cursor 文本
      （`agent --list-models`：ANSI/零宽字符清洗、header/空行/Tip/散文
      永不成为模型、`(current)`/`(default)` 标记、`-fast`/`-<effort>`
      变体解码保留原 slug）与 codex JSON（`codex debug models`：
      非法 JSON/错误结构/全坏条目=发现失败，空目录=有效成功，
      supported_reasoning_levels 原词汇原顺序（含 ultra）、
      additional_speed_tiers、context/max_context_window、visibility）；
      参数化 bracket 语法证据（`<family>[context=1m,effort=high,
      fast=false]`）与枚举模型严格分开表达，示例家族仅为语法证据，
      不冒充支持清单；CatalogEntry/ParameterizedSyntax/ModelCatalog
      容器（source/generated_at/generation）可序列化往返；
      catalog_from_snapshot 容忍旧快照与损坏数据（缺即 unknown）；
      next_generation 只在成功刷新时递增；进程级 cached_catalog 按
      持久化 generation 对账，成功刷新/失败刷新均使同进程旧缓存失效。
      模块零模型名硬编码（测试钉住）。
- [x] `src/orx/probes.py`：HarnessSpec 增 catalog_format；快照增量
      新增 `model_catalog` 与 `catalog_error` 两键（位置不变
      `<user data dir>/probes/<harness>.json`）；发现失败语义明确——
      binary 缺失/非零退出/超时（MODELS_PROBE_TIMEOUT）/空输出/
      不可解析输出记录 catalog_error 且不写目录，旧目录绝不重标为
      新鲜成功；auth 与目录正交（互不伪造）；effort_selection 增加
      目录信号（codex levels）；持久化后显式失效目录缓存；无新增
      probe 调用（cursor 参数化证据复用已取的 help 文本）。
- [x] `src/orx/cli.py`：agent info 人类输出展示目录（数量、来源、
      时间、generation、current/default、families、参数化证据及
      “示例非清单”声明）或如实展示失败原因/无记录；agent probe
      人类输出加 models 行；--json envelope 与 0/1/2 退出码不变。
- [x] fixtures：真实 CLI 输出捕获（cursor-list-models.txt 含零宽
      字符与 Tip、agent-help.txt、真实形状裁剪的
      codex-debug-models.json）；tests/test_model_catalog.py 新增
      29 例（解析/变体/标记/参数化/失败与空目录区分/大目录 1200 条
      全量/往返/generation/缓存失效/无硬编码）；test_probes.py
      更新冻结形状并新增 8 例（invalid JSON、超时、空目录、auth
      失败不伪造、cursor 标记+参数化、generation 递进、刷新失败
      不重标旧目录+缓存失效、旧格式快照可读、1100 条 >256KiB 大
      目录经 probe 全量）；test_cli.py 新增 3 例（旧格式快照
      agent info 可读、目录失败展示原因非假成功、缺 binary 的
      失败行）并加强 3 例既有断言。
- 证据：`uv run pytest -q tests/test_model_catalog.py
  tests/test_probes.py tests/test_cli.py` 绿；全量 `uv run pytest -q`
  812 绿（772 存量 + 40 新增）；`git diff --check` 干净；真实 CLI
  只读验证（agent --list-models/--help、codex debug models 600KB
  全量解析往返）；日志 `.orx/runs/R008/check/T002/`。

### 阶段 3：运行时按 adapter 解析 — Done（T003）
- [x] cursor/codex resolver 消费 ModelSpec + 共享 launch_catalog：
      枚举变体 slug、effort+fast、裸枚举、未枚举且 help 有 bracket
      证据时合成参数；缺变体/不支持的 speed、context、capability
      返回诊断，不静默换模型。EffortOutcome 仍以目录变体（成功）
      或 bracket 回显（成功且匹配）确认。worker/planner/verifier
      共用该路径；解析失败不启动、不写完成或通过。shell/host
      身份字符串不变。决策（spec + cli_slug + strategy + generation）
      与 argv 分开落记录。

### 阶段 4：既有 profile 语义等价迁移与展示 — Done（T005，R8）
- [x] profiles.toml 既有裸 slug（gpt-6.1-sol、claude-opus-5-5、
      grok-4.7、composer-2.5、GLM account id 等）的语义等价表达与
      迁移路径：`docs/model-selection.md` 迁移表（每行给出语义等价与
      真实捕获目录上的同一解析目标，版本拼写 5-5/5.5 仅为表示差异；
      account id 为不透明 host 身份、不设语义形态）。
- [x] 展示/记录走身份字符串 + 完整 ModelSpec（T003 落地：Launch
      model_resolution 决策记录与 attempt 身份字符串）；等价性由
      T004 e2e（既有群体原样启动、语义表等价、routing 等价）+
      T005 `tests/test_model_docs.py`（文档示例经真实加载器、
      class/effort/capabilities/routing 顺序等价、EffortOutcome
      证据规则）钉住；与 routing-strategy 既有档位兼容（只做等价
      表达，不改档位——tier 表与角色默认档位原文由测试钉住未动）。

### 阶段 5：端到端回归与终审 — Done（T005，R8）
- [x] 本地替身端到端（语义配置 → 目录 → 解析 → 适配器 argv → 记录）：
      `tests/test_model_evolution.py`（15 例独立演化矩阵——前缀改名+
      版本升级的固定/浮动策略、变体增删、枚举↔参数化双向迁移、help
      参数证据收窄、真实捕获目录上的多候选歧义、目录格式变化与发现
      失败、codex 目录刷新演化、全程不换 auto/他族/他版本的扫掠）与
      `tests/test_model_selection_e2e.py`（12 例——既有裸 slug 群体与
      bracket 直写经真实加载器+真实捕获替身原样启动、语义表等价、五档
      effort、三角色 dispatch 实际 argv/EffortOutcome/决策记录、同进程
      与真子进程的刷新后启动、刷新失败/auth/超时/历史快照诊断、CLI
      probe/info 0/1/2 契约、host account id 与 routing 等价性）。
      揭示并修复两处 G008 缺陷：cursor resolver 此前对固定版本缺目录
      匹配时会走 bracket 合成、浮动家族无法跟随版本演化（新增 stem
      版本策略：固定=明确失败点名所提供版本、浮动唯一=跟随、多版本=
      歧义诊断）；CLI verifier 路径此前不记录 EffortOutcome。
- [x] README/docs 同步真实流程：`docs/model-selection.md` 成文——
      ModelSpec 形式、目录刷新/查看仅可编程入口（orx agent probe/
      info，Tune 仅背景段落且无任何操作步骤、测试钉住）、各 adapter
      解析规则与严格失败边界、Claude/GPT 未枚举走参数化 bracket
      （语法支持与具体模型/参数运行验证分开表述）、grok 前缀与版本
      演化实例、旧配置兼容与迁移、effort 验证证据（目录变体成功/
      bracket 回显/静默=provider_default）、证据登记 E1–E5（控制器
      2026-10-07 背景、T002 会话 help 成功与一次瞬时 list-models
      认证失败、T005 会话复测 2026.10.01-e373342、真实捕获/裁剪重建/
      仿真样本分别标记、明确声明 G008 未执行任何真实模型运行）与
      诊断规则表；README 启动说明改为与 resolver 实际行为一致并
      链接；routing-strategy 仅增加抽象层说明与链接（分档与角色
      默认档位不变，测试钉住）。`tests/test_model_docs.py` 13 例。
- [x] 归档本节记录（五阶段全部 Done，临时状态图例行移除；其他
      Goal 历史原样保留）；不含版本发布——版本号、tag、发布材料
      零改动，发布由用户另行决定。

## G008 轮次记录
- 2026-10-07 R8 T001：阶段 1 交付（attempt 127）——models.py 数据
      契约 + config.py 语义加载边界 + 70 例新增测试；证据见
      `.orx/runs/R008/check/T001/`。
- 2026-10-07 R8 T002：阶段 2 交付（attempt 128）——model_catalog.py
      解析层（cursor 文本/codex JSON/参数化证据分离）+ probes.py
      快照增量目录键与失败/刷新语义 + cli.py agent info/probe 展示；
      40 例新增测试；证据见 `.orx/runs/R008/check/T002/`。

- 2026-10-07 R8 T003：阶段 3 交付（attempt 134）——cursor/codex
      resolver 消费 ModelSpec + 共享 launch_catalog（快照优先、无快照
      时一次本地发现、进程内缓存）；base.py 增解析词汇
      （effective_model_spec / identity_token(s) / requested_dims /
      catalog_state / refresh_hint / ModelResolutionError），
      Launch 增 model_resolution 决策记录；dispatch 三角色启动路径
      接入并在解析失败时记录失败 attempt（不换模型、不写完成/通过），
      决策 JSON + argv 日志分开落证据；shell/host 契约不变；
      tests/test_model_resolution.py 34 例（语义→变体 slug / bracket
      合成 / 诊断口径 / EffortOutcome 确认规则 / 共享目录与
      generation 刷新 / 三角色与失败路径 / 身份字符串兼容）；
      证据见 `.orx/runs/R008/check/T003/`。

- 2026-10-07 R8 T004：阶段 5 测试面交付（attempt 136）——演化矩阵
  + 替身 e2e 共 27 例新增；修复 resolver 版本策略（cursor.py：
  `_stem_families`——cursor- 前缀剥离后的家族 stem 版本对账；固定
  版本缺枚举且有他版本=诊断（不静默换固定版本/不 bracket 伪装）、
  浮动唯一版本=跟随目录、多版本=歧义诊断）与 verifier
  EffortOutcome 记录（dispatch.py `_run_cli_verifier` 补
  actual_effort/effort_source）；全量 873 绿（846 存量 + 27 新增）；
  证据见 `.orx/runs/R008/check/T004/`。

- 2026-10-07 R8 T005：阶段 4+5 文档与终审交付（attempt 138）——
  `docs/model-selection.md` 成文（机制权威：三层边界、ModelSpec
  形式与版本策略、旧配置兼容迁移表、目录刷新/查看可编程入口与
  generation/失败语义、cursor/codex/shell/host 解析规则与 EffortOutcome
  证据通道、严格失败边界、Claude/GPT 参数化路径（语法支持 vs 运行
  验证分开）、grok 前缀与版本演化三版本实例、证据登记 E1–E5 与
  真实捕获/裁剪重建/仿真样本标记、无真实模型运行声明、诊断规则表、
  不变项声明）；`tests/test_model_docs.py` 13 例（文档 TOML 示例经
  真实加载器、语义/旧群体在回放真实捕获上等价——身份/class/effort/
  capabilities/解析目标、EffortOutcome 规则、routing 顺序双形态等价、
  来源与样本标记钉住、Tune 仅背景无操作步骤、README/routing 链接与
  分档不变钉住、IMPLEMENTATION_PLAN G008 收口钉住）；README cursor/
  codex 启动说明改为与 resolver 实际行为一致（旧 `<id>[effort=
  <mapped>]` 单形态表述移除）并加 Docs 链接；routing-strategy 仅增
  "Model declaration and resolution" 机制段与链接；本文件 G008 记录
  归档（五阶段 Done、临时状态图例行清理、其余 Goal 历史保留）；
  不含版本发布（版本号/tag/发布材料零改动）。证据见
  `.orx/runs/R008/check/T005/`。

# G010：Goal 生命周期管理 —— T001 真实执行身份与用量关联（2026-10-16 起）

主功能为 Goal 暂停与继续、取消与安全收尾；本任务是配套第一项：补齐真实执行
身份与用量关联（诊断 Analytics G002/R002 断点并修复）。诊断全文见
docs/diagnostics/g010/diagnosis.md；保护基线见
docs/diagnostics/g010/user-file-baseline.json。

## 五阶段实施计划（T001）

### Stage 1：只读取证与保护基线 — Done

项目索引只读定位 orx-analytics（R002）；逐段建立证据链（归档 assignment →
nonce 转交 → 会话结构 → 目录 → 发现时机 → 子树读取 → ORX 侧结果）；
记录 docs/m1.2-report.md 初始 sha256/暂存态/归属与起始 HEAD。产出：
user-file-baseline.json、provenance 数据（诊断报告表）。

### Stage 2：脱敏真实结构复现（红） — Done

fixture：tests/fixtures/g010/（两份归档原始提示词逐字节副本 + provenance.json
真实数值/来源/合成映射）。新增 7 例回归先红：跨目录 nonce 精确命中被目录
谓词滤空（'none'≠'unique'）、跨目录双命中旧逻辑误判 'unique'、
directory_filter 键缺失、worker/verifier 准真实闭环绑定 None、unsettled 被入账
（'recorded'≠'missing'）、重复补录误标子树冲突。证据：
docs/diagnostics/g010/red-green/01-red-run.txt（7 failed, 43 passed）。

### Stage 3：修复实际断点（绿） — Done

src/orx/zcode_sessions.py：nonce 权威发现（首 part nonce 命中即绑定，目录事实
记入候选 directory_matched 而非过滤；根会话 parent NULL 结构性排除；双命中仍
ambiguous；legacy marker 目录谓词保持必需；结果携带 directory_filter）；
model_usage_aggregate 新增 settling_request_rows（终态词表
completed/error/cancelled，真库全量观测）。src/orx/adapters/base.py：MISS 词表
新增 zcode_usage_unsettled、zcode_duplicate_backfill。src/orx/dispatch.py：
usage_zcode_record unsettled 分支（不写库）；_usage_autobill 区分重复补录
（already_recorded，不标缺失、不覆盖存量行）与子树冲突；五个闭合点结果携带
usage_backfill。绿证据：red-green/02-green-run.txt（门禁 4 文件 88 passed，含 CLI
unsettled 陈述域错误一例）；全量 1061 passed（含一次 m12 再生的工作区影响，
见基线 postscript）。

### Stage 4：零付费准真实闭环（worker + verifier） — Done

worker：park（归档 nonce 经 mint 替身注入）→ task claim --discover-session
（实际发现函数 × 归档原始提示词首 part 的伪库会话）→ task_complete（实际
闭合入口，delivery gate true）→ autobill 唯一 zcode_db 观测 = 真实数值 →
任务 passed；链 run/revision/task/attempt/nonce/session/usage 全串
（tests/test_session_identity.py）。verifier：verify_dispatch（归档 verifier
nonce）→ verify_submit --discover-session 绑定并闭合 pass、autobill 入账
真实数值（tests/test_subagent_contract.py）。覆盖矩阵：无匹配/歧义/库不可读
或不存在/未稳定/子树冲突/重复补录（新增 2 + 既有用例族）；缺失有原因、未知
不补零、controller 根会话拒绑有回归钉住。

### Stage 5：契约文档与轮次收口 — Done

docs/observability-contract.md 第七修正案（nonce 权威目录语义、
settling_request_rows、zcode_usage_unsettled、zcode_duplicate_backfill 非缺失
类、闭合结果携带 usage_backfill）+ 缺失原因表扩充。诊断报告（已证实根因/
排除项/未知项/R002 样本 vs 规划背景 16+1→18 差异算术/外部零写入声明）。
本文件 G010/T001 轮次记录。技能与 agent 定义未改动（身份锚点转交协议原样）。

# G010/T002：持久化 Goal 暂停与继续及统一启动门禁（2026-10-16）

主功能第一支：Goal 暂停与继续落地。设计全文见
docs/goal-lifecycle.md；实现事实与竞态论证见
docs/diagnostics/g010/t002-pause-resume.md。

## 交付内容

- schema v13：goals.status CHECK 增设 'paused'（父表重建，FK 延迟换表；
  迁移保留全部身份/历史/用量，失败不动原库；零回填）。读取白名单与
  schema_gate 扩至 8..13（observability-contract 第八修正案）。
- 状态机（machine.py）：Goal 转移表 + goal_pause/goal_resume（重复调用为
  显式幂等 no-op，changed=False；终态拒绝）；refresh 的完成写改为
  active→done CAS——paused 永不被 refresh 隐式解除，暂停期间全部完成则
  在 resume 时按既有不变量收敛为 done。
- 统一启动门禁（GoalPausedError）：planner/replan 路由、计划激活、
  worker 派发（host/external 停泊与 CLI 启动）、停泊 assignment 的
  claim、verifier 派发、降级 reroute——门禁读与占位写同事务（BEGIN
  IMMEDIATE），提示词文件写在事务外；已运行执行者不受抢占/杀死/改绑，
  事实提交（heartbeat/complete/fail/recover/note/observe/check/verdict）
  暂停期间照常。
- goal pause/resume：dispatch + CLI 文本/JSON 双接口；resume 不启动任何
  执行，返回 run/revision/assignment/attempt/nonce 原归属与恢复指引；
  paused 跨会话/跨进程持久；暂停期间不可创建第二个未结束 Goal。
- dogfood 隔离：m12 报告输出移入 pytest tmp_path（不再读写用户
  docs/m1.2-report.md）；用量回放改为对 tmp 私有快照库执行
  python -m orx usage record；新增子进程回归证明两文件逐字节不变、
  产物确实落在临时树。
- 回归：machine/goal/lifecycle/state/observability/m12 扩充（转移表、
  门禁矩阵、双确定性排序、并发 claim、并发 resume、子进程暂停/继续、
  新进程身份保持、迁移三态）。全量 1092 通过。

## 范围外披露

- tests/test_package_acceptance.py 的 upgraded_schema_version 断言
  12→13：范围清单遗漏的机械 schema 消费者，最小单行迁移并在此披露。
- sources.py 的 --auto-accept 在暂停时响亮失败（items 留 pending）：
  该文件不在本任务写范围；安全行为已记入 docs/goal-lifecycle.md。
- 取消与安全收尾（G010 另一半）未实现；本轮零改动。
- 本轮提交不得裹挟用户未提交的 docs/m1.2-report.md 改动（测试已不再
  触碰该文件）。

# G010/T003：Goal 取消请求、真实停止确认与安全收尾（2026-10-16）

主功能第二支：取消与安全收尾落地。设计全文见 docs/goal-lifecycle.md；
实现事实与竞态论证见 docs/diagnostics/g010/t003-cancel.md。

## 交付内容

- schema v14：goals.status CHECK 增设 'cancelling'（父表重建沿用 v13
  模式，FK 延迟换表；零回填，身份/历史/用量全保留）。observability
  读取白名单与 schema_gate 扩至 8..14（第九修正案，analytics 保持只读）。
- 两段式取消：active|paused -> cancelling（幂等请求，重复不重复收尾）
  -> cancelled（仅在无未确认执行时 CAS 收敛）。cancelling 不可 resume、
  仍占用唯一未完成 Goal 槽位；planning/执行中/blocked/paused 均可发起；
  done 终态拒绝迟到取消（完成与取消并发只产生一个合法收尾结果）。
- 明确取消未启动工作：pending/runnable/blocked/waiting_host/
  waiting_external 任务与无已派发 verifier 的 verifying 任务、等待中的
  planning assignment 及其 planner attempt——全部走合法任务机转移并记
  goal_cancel 事件，占位 attempt 以 result='cancelled' 关闭（不伪造
  started_at）。运行中的 worker/planner/verifier 绝不凭静默收尾：只在
  真实事实（迟到交付/裁决/fail/recover 终止事实）上收尾，每个收尾事实
  都会重跑收敛检查；cancel 输出与 orx status 列出未确认执行及可执行
  后续操作。
- 可管理 CLI 停止：Launch 携带只读 stop_check（goal 状态闭包），运行时
  以独立会话派生子进程，停止仅向本次启动的进程组发 SIGTERM（宽限后
  SIGKILL 升级），始终等待真实退出并把停止请求/信号/退出码记录在
  attempt 与 goal_cancel_stop 事件上；usage/session/执行日志照常完整。
  另一进程（真实 orx goal cancel 子进程）可在执行等待期间发起取消。
- 显式事实入口：取消中 task recover 关闭而不重派（任务落 cancelled，
  运营者事实与机器事实并排记录）；verdict 照常关闭其派发 attempt。
  取消终态后 revision/attempt/证据/验证/事件/用量仍可按 Goal/Run 查询，
  迟到写入被既有 stale/not-running 门拒绝；新 Goal 不影响旧 Goal 历史。
- 回归：runtime（可管理停止三态：正常/停止/升级）、machine（取消转移
  表 + 幂等）、goal（planning/paused 取消、CLI 双接口、槽位）、
  lifecycle（收尾矩阵、静默不收尾、双序完成竞态、claim 竞态、
  planning 取消、verifier 收尾、recover 不重派、跨进程 CLI 停止、
  终态后查询与新 Goal 隔离）。全量测试通过。

## 范围外披露（机械性 schema 消费者）

- tests/test_inbox.py、tests/test_m12_acceptance.py、
  tests/test_subagent_contract.py、tests/test_package_acceptance.py 中
  五处硬编码 schema 版本字面量 13→14（仅数字，含一处裸 SQL 与一处相邻
  注释）：沿 T002 先例最小迁移并在此披露；这些文件无任何行为改动。
- 本轮提交不得裹挟用户未提交的 docs/m1.2-report.md 改动（测试未触碰
  该文件）。
