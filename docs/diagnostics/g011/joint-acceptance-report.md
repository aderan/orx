# G011 联合生命周期验收报告（T007）

状态：T007 交付物。本报告记录**从冻结归档安装后的最终 App 与 ORX
wheel**出发的两仓联合验收事实：安装来源与自包含启动、真实旧库升级、
两条生命周期链（暂停/继续/取消、暂停期完成）、通知、用量与回执、
只读边界、渠道安装体验，以及一条**验收中真实发现的运行时封条缺陷**。
机器可读证据：`dist/g011/manifest.json`（stages 段）、
`dist/g011/logs/01-exercise.log`、`dist/g011/gui-acceptance/`（操作者
验收事实）、`docs/diagnostics/g011/screenshots/`（真实截图，见 §9）。

- 兼容组合（冻结绑定，candidate.json）：ORX **v0.4.0**
  （`f1cc7757…`，wheel sha `f71c19ae…`）＋ Analytics **v0.4.2**
  （`2461a260…`，App 归档 sha `5187029c…`）。两仓不同号为既定组合。
- 渠道：**internal-preview**（ad-hoc）。正式渠道凭据缺失按设计
  fail-closed（distribution.json `official: blocked`），本报告不构成
  任何正式渠道验证（§8）。
- 规定检查：`exercise` 与 `verify` 全绿（§10）；GUI/截图边界如实
  记录（§9）。
- 修订轮（rev4）说明：命令门禁在既有证据基础上复跑收口（§10，
  2026-10-18 01:43–01:46 本地，两命令 exit 0、ORX 检查轮全绿）；
  视觉条目按修订口径转为事实型核验——像素截图因操作者会话在整个
  验收窗口及复跑期间锁定而物理不可得（独立视觉验证者 attempt 209
  如实 fail 并留档，§9），像素级确认列为解锁后人工复核项（§9.1）
  并随发布说明移交；复跑期间操作者会话仍锁定（01:41 实测
  `CGSSessionScreenIsLocked = true`），未取得、亦未伪造任何截图。

## 1. 安装来源与自包含启动

- **从 candidate.json 指定的原归档安装**：
  `dist/g011/dist/ORXAnalytics-0.4.2-internal-preview-dev-macos13-arm64.zip`，
  实测 sha256 `5187029c8ab41a41a8d6100c502061035a322c7d24ad015cfbe2fa76303d7396`
  与候选绑定逐字节一致；用安装文档规定的方式 `ditto -x -k` 解压并
  安装到 `/Applications/ORX Analytics (Internal Preview).app`。
- **安装时验签**：`codesign --verify --strict` exit 0（ad-hoc，authorities
  为空——渠道的如实属性）。Info.plist：`CFBundleShortVersionString
  0.4.2`、`CFBundleVersion 0.4.2+dev+2461a26`、bundle id
  `org.orxanalytics.macos.dev`、`ORXAnalyticsSourceCommit
  2461a260…`（完整冻结提交）、`ORXAnalyticsChannel internal-preview`；
  包内 `Resources/distribution.json` 与 `orxanalytics-version.txt` 同值。
- **自包含启动（不依赖源码 checkout 与开发环境）**：App 经 `open`
  启动（launchd 环境，不继承任何 shell/开发变量）；进程实测：
  - App：`/Applications/…/Contents/MacOS/ORXAnalyticsApp`
  - 引擎：`/Applications/…/Contents/Resources/python/bin/python3`
    `/Applications/…/Contents/Resources/orx_engine_serve.py <data-dir>`
    ——进程可执行路径全部指向 **App 内置运行时（cpython-3.12.8
    standalone）与内置引擎入口**，无 `ORX_ANALYTICS_ENGINE_*` 开发
    旁路、无系统 python、无源码 checkout 依赖。
- **ORX CLI 来源**：全部控制操作来自冻结 wheel 装入的一次性 venv
  （`dist/g011/exercise-venv/bin/orx`，`orx version --json` →
  `0.4.0`），隔离 HOME/CONFIG/DATA 环境；本报告所有 `orx` 命令
  均为该已安装 CLI（快照存 gui-acceptance/）。
- **并存事实**：操作者的旧生产 App（`org.orxanalytics.macos`，
  `~/.orx-analytics`）全程在运行，未被触碰；预览 App 用独立 bundle
  id＋显式 data-dir override，两者无串扰（各自引擎、各自锁）。
- **隔离 macOS 用户证据：未验证边界**——本机无免密 sudo
  （`sudo -n true` → password required）、无其他用户凭据，无法在
  隔离用户下复验安装/首启。以当前用户真实安装体验为准，不以同户
  测试冒充（Goal 验收原文要求如实记录）。

## 2. 真实旧库升级

- **ORX 库 v8→v15**：由冻结提交的包验收链在干净检出上实测通过
  （battery 阶段 `check_release.py` exit 0 problems=[]，日志
  `dist/g011/logs/02-battery.log`）：真实 v0.3.0 标签 fixture 逐级
  升级、旧版拒读、备份恢复全过。本 App 引擎读到的全部 GUI 链
  项目 schema 均为 **15**（engine health `schema_version: "15",
  supported: true`）。
- **Analytics store v2→v3（App 级，真实发生）**：把**真实旧代码
  （0.4.1@3f1b66c0）产生**的 store v2 样本（sha `2a7d9873…` 与
  冻结绑定一致）置入 App data-dir 后启动安装后的 App：
  1. **迁移前备份**：`backups/analytics-pre-0.4.2-20261017-234117.db`
     （299,008 字节，实测 `store_version=2`）先于任何 v3 打开；
  2. **原地升级**：`analytics.db` → `store_version=3`，新增
     `orx_receipts` 镜像表；逐表行数与备份完全一致
     （orx_snapshots 1/1、orx_runs 1/1、orx_goals 1/1、orx_tasks
     2/2、orx_attempts 3/3、orx_usage_obs 1/1、orx_task_events
     9/9、orx_evidence 1/1）——**历史行保持**；
  3. **重复启动不重复迁移/备份**：退出 App 再启动（引擎单例按
     设计跨 App 退出存活，重开复用），stamp
     `app/last-launched-engine-version=0.4.2` 命中同版本，
     backups 数 1→1，**无新备份**；
  4. **旧版拒读**：用**真实 0.4.1 代码**（git archive 3f1b66c0）
     打开升级后的 v3 库：`RuntimeError: analytics.db store_version
     3 != 2; this reader cannot open it`（安装文档承诺的原文拒绝，
     不改写不损坏）；库级未来版本拒读（store_version=9）由
     freeze/verify 的样本链实测（`current_analytics_upgrades_store_v2`）；
  5. **备份回退**：把迁移前备份复制为独立 data-dir 的
     `analytics.db`，真实 0.4.1 代码正常打开（store_version=2，
     orx_runs 1 行可查）——备份是真正可用的回退工件。
  （探针脚本与输出：`dist/g011/rollback-check/probe_old_reader.py`
  及其运行结果；App 级事实存
  `dist/g011/app-upgrade-data/`。）

## 3. 生命周期链一：执行中暂停 → 拒新派发 → 关闭重开 App → 继续 → 取消 → 真实交付收敛

全部控制命令来自**已安装 ORX 0.4.0**（一次性 venv），对象为隔离
项目 proj-1（注册名 "GUI chain 1"）；引擎为**安装后 App 的内置引擎**
（socket 协议 v1）。逐步事实（CLI envelope + 同时刻引擎 overview，
快照存 `dist/g011/gui-acceptance/`）：

| 步骤 | 命令结果（实测） | 引擎/面板数据层（实测） |
| --- | --- | --- |
| 暂停 | `goal pause` → `paused, changed:true` | Run **running（执行中）** 仍在 active 列表，Goal 控制态 **paused（已暂停）** 徽标并列——分层不改写 |
| 暂停期拒新派发 | `orx run` → exit 1，`"goal G001 is paused; run orx goal resume…"` | ——（闸门事实） |
| 关闭并重开 App | 退出＋`open` 重启，引擎按设计跨退出存活并被复用 | 重开后同一 paused/running 分层仍在（数据库持久，非内存态） |
| 继续 | `goal resume` → `active, changed:true`；`in_flight.running=[T001 attempt 1 sess-gui-op]`——**原 attempt 身份保留，无第二执行者** | active 列表，无徽标 |
| 取消（请求） | `goal cancel` → `cancelling, changed:true`；未启动 T002 被显式扫除，**T001 为 unconfirmed**（worker, attempt 1, sess-gui-op） | Run **running** 保留在 active ＋ Goal **cancelling（取消中）** 徽标——未关闭执行的事实完整保留 |
| 真实交付收敛 | 写真实标记文件＋结构化回执，`task complete T001 --attempt 1` → `passed`（delivery gate 实跑 `test -f gui-marker.txt`）；Goal 收敛 `cancelled`，Run 事实 `blocked` | —— |

链一终态核对了验收原文的关键格：**已取消 Goal（原 Run blocked）不进
活跃、不进最近完成、不进失败关注**——引擎 overview 实测
`active_runs` 由 2→1→0、`attention: []`、`recent_completed` 不含它，
而 `runs.list` 仍返回 `(R001, blocked, cancelled)`——**历史照常可查**。

## 4. 生命周期链二：暂停期间完成全部执行 → 继续 → done

隔离项目 proj-2（单任务，真实交付）：

| 步骤 | 命令结果 | 引擎/面板数据层 |
| --- | --- | --- |
| 暂停 | `goal pause` → `paused` | —— |
| 暂停期完成 | 真实标记＋`task complete` → `passed`；`status`: **goal paused, run done** | Run **done 进入"最近完成"**（执行事实），Goal 控制态徽标仍 **已暂停**——**不冒充 Goal done**；`active_runs: 0` |
| 继续 | `goal resume` → `done, changed:true`（完成不变量在 resume 处收敛，诚实报告） | 最近完成条目变为 run done ＋ goal **已完成**（徽标消失） |

配套矩阵事实（同为实测）：`paused/running` 停留在 active 列表且**不进
关注、不通知**（暂停是徽标不是异常）；`cancelling/running` 见 §3。

## 5. 通知（真实系统行为与策略层分别记录）

- **默认关闭（实测）**：`defaults read org.orxanalytics.macos.dev
  notifications.enabled` → 键不存在（AppSettings 默认 false）。
- **关闭态不投递（真实失败事实）**：proj-3 以**真实失败**驱动
  （`task fail T001 --reason …` → `failed`），引擎关注出现
  `failed_task`（携带真实失败原因与 failing attempt 1、recovered:
  false）＋ `blocked_run`；此时统一日志中 **零** orxanalytics 投递
  记录（`log show … process == "usernoted"` grep 0 行）、App 自身
  日志 0 条 notification 行——关闭即一个都不发。
- **取消后无新失败通知（实测）**：取消 proj-3（其失败/受阻事实仍在
  历史库）后，`attention` 由 2 → **0**（cancelled/* 不进当前关注、
  不重复报警），usernoted 仍 0 行；历史失败经 `runs.list` 照常可查
  （§3/§4 的已取消格）。
- **去重口径**：关注只发自当前事实型条目（`failed_task`/`blocked_run`），
  同一事实在重复采集下不重复产生关注（引擎 overview 幂等）；
  Swift 侧去重逻辑由冻结提交的 140 项 swift test 钉死（battery 实测
  0 失败）。
- **授权态切换（允许/拒绝）：未验证边界**——授权仅在用户于设置页
  打开通知开关时申请（SystemNotifications 契约），需真实系统设置
  对话框的人工交互；本机验收窗口内操作者会话被锁（§9），未能完成
  该人工步骤，如实标注为未验证，不以策略测试冒充。通知中心记录
  库（group.com.apple.usernoted）受 TCC 保护（authorization
  denied），统一日志为可复现来源并已用于上述实测。

## 6. 用量与回执

- **未知用量不补零**：GUI 链全部交付为无 token 会话的真实数据，
  引擎 consumption 块实测 `input_tokens/output_tokens/cached_input:
  null`、`observed_total: null`（口径 `orx_recorded`）——显示未知
  而非 0；exercise 日志的用量汇总同样保留 worker/verifier 各自的
  `unassessed` 计数与 null token 字段、`missing_reasons {}`。
- **worker/verifier 与跨 revision 口径**：exercise 的用量记录按
  bucket 分账（worker/verifier/controller/probe 各自独立，probe
  空账本显式列出"nothing merges"），归一化口径注明"非计费"；
  分层账本由冻结提交 Analytics 全量（256 项）与 ORX 全量
  （1162 项）测试钉死并在 battery 实测通过。
- **重复采集不重复计费**：稳态下两次显式 refresh 的用量账本行数
  不变（usage_obs 恒定）；快照幂等——显式 refresh 每次全量快照
  是采集语义，计费/用量按源身份记账（导入天然幂等）。
- **回执**：链一真实交付后 `completion_receipts` 恰 **1** 行；同一
  交付换文件重传（`evidence-gui1-retransmit.json`）→ 返回原回执
  （`passed`, attempt 1），receipts 仍 **1**、usage 行仍 0——纯读
  重放。Analytics 侧 `orx_receipts` 为逐快照镜像，同一交付的
  `delivery_digest`（`dcba0600…`）跨快照恒定——**Analytics 只显示
  回执事实，无重放能力**（只读层无任何控制入口）。exercise 阶段
  另有进程级重传零字节变化断言（manifest
  `goal_1.receipt_retransmit_zero_change: true, receipts: 1`）。

## 7. 只读边界

- **ORX 写作者静止窗口采集前后源库不变（实测）**：proj-1/proj-2
  state.db sha256 在两次强制采集前后**逐字节一致**
  （`559b079c…`/`e91f1af…` 前后相同）。
- 引擎对 ORX 项目只以 `mode=ro`/快照读打开；exercise 断言
  `read_only_source: true`、真实会话隔离（`zc_/cx_/cur_` 镜像为空，
  `no_real_session_mirrors: true`）。
- Analytics 全程无任何 ORX 控制命令面：本报告全部暂停/继续/取消/
  交付/恢复操作均来自**已安装 ORX CLI**；App/引擎只读展示（§3–§5
  的引擎数据均与控制事实分层对应，无一改写）。

## 8. 渠道安装体验（内部预览实测；正式渠道如实 blocked）

- **内部预览（本机当前用户真实安装）**：归档本机构建、未经网络
  传输，**不携带 quarantine 属性**（实测仅 `com.apple.provenance`），
  故 `open` 首启不被 Gatekeeper 拦截——这是本机真实首启事实。
  为覆盖"下载后首启被拦"的文档行为，对安装副本的真实 Gatekeeper
  评估实测：`spctl --assess --type execute` → **rejected**（exit 1，
  "a sealed resource is missing or invalid"——ad-hoc 未公证渠道的
  如实系统裁决）；带 quarantine 副本同判。安装文档规定的内部预览
  手动确认/`xattr -dr` 旁路仅属本渠道，未用作任何正式渠道证据。
- **升级安装**：以同归档再次覆盖安装（quit → ditto 覆盖 → 启动）
  为实测升级路径；数据保留与"重复启动不重复备份"见 §2。
- **正式渠道：blocked（凭据缺失，非本任务可解）**——无 Developer
  ID Application 证书、无 notarytool 凭据（distribution.json
  `credential_facts_probed_live`）；因此**保留 quarantine 的最终包
  Gatekeeper 放行、公证首启与升级安装验证在本机不可执行**，按
  Goal 约束如实记录为未验证/凭据缺失边界，不以内部预览旁路或
  模拟补成通过。

## 9. 界面证据（真实截图）与会话边界

操作者桌面会话在验收窗口内（2026-10-17 23:45 – 2026-10-18 00:59，
含两次有界等待共约 55 分钟）**始终处于锁定状态**：锁屏 shield 常驻、
窗口捕获被拒（`screencapture -l<id>` → "could not create image from
window"）；00:01:57 的唯一活动是登录窗口切换到另一用户头像（非本会话
解锁）。已取得的真实 UI 证据：

- **菜单栏状态项实时读数**（System Events，操作者会话内真实 UI 对象）：
  验收中段为 **"2"**（两个进行中 Run，与引擎 `active_runs: 2` 一致；
  存 `gui-acceptance/menubar-badge-reading.txt`）；面板关闭时徽标按
  设计保留上次刷新值（cache-first，面板打开后 5 秒档同步）——实测
  终态读数仍为 2，属文档化行为而非缺陷。
- **面板窗口存在性**（CGWindowList）：状态项点击后 ORX Analytics
  面板窗口（360×643）真实出现过（锁定会话下被 WindowServer 收回）。
- **面板数据源逐态分层事实**：§3–§5 的引擎 overview 实测覆盖验收
  原文要求的全部界面状态——active 列表三态（进行中/已暂停/取消中，
  执行层均执行中）、最近完成两态（done＋已暂停、done＋已完成）、
  已取消全排除＋历史可查；计数、关注、用量块同步实测。

**像素截图：未取得（真实边界，不冒充）**——`screenshots/` 目录为空，
仅存边界说明（`screenshots/README.boundary.md`）；不以锁屏像素或模拟
图冒充面板内容。独立视觉验证者对此已如实判定 **fail** 并留档（T007
验证 attempt 209，2026-10-17T17:11Z，理由原文：「像素确认物理不可
得：整个验收窗口期与本验证期间操作者会话均锁定
（CGSSessionScreenIsLocked=1，独立复核），
docs/diagnostics/g011/screenshots/ 仅边界 README；非像素 UI 证据
（菜单栏徽标读数、面板窗口存在性、引擎数据源分层态）在
dist/g011/gui-evidence.json 与 gui-acceptance/ 记录在案」）；该
fail 判定是终局记录，不删除、不被本报告改写。修订复跑轮
（rev4，2026-10-18 01:41 本地）再次实测 `CGSSessionScreenIsLocked
= true`——会话在命令门禁复跑与验证期间仍处锁定，像素证据在本轮
依旧物理不可得，不以任何替代物冒充。

### 9.1 解锁后人工复核项（约 10 秒；随发布说明移交）

操作者解锁桌面会话后，按下列顺序目视核对即可收口像素级确认（全部
状态已按 §3/§4/§5 的命令序列真实落库，随时可查、可重建）：

1. **菜单栏徽标**（约 3 秒）：ORX 状态项数字与引擎 `active_runs`
   一致——徽标是 A 桶 Run 计数，已取消 Goal 不计入；面板关闭时
   保留上次刷新值属文档化行为（§9）。
2. **快速面板控制/执行分层**（约 4 秒）：进行中列表条目同时显示
   Run 执行事实（执行中/受阻/完成）与 Goal 控制徽标（已暂停/
   取消中）——两个维度并列呈现、互不改写；「已暂停」不冒充完成，
   「取消中」保留未关闭执行（生命周期矩阵 §1/§2）。
3. **历史与详情分层**（约 3 秒）：已取消 Goal 不出现在进行中/
   最近完成/关注，但 runs 列表与详情页照常可查（执行事实原文、
   控制态已取消）。

像素级确认的结果（无论通过与否）如实记入发布说明；在此之前，本
报告的界面结论以 §9 的非像素真实证据与边界记录为准。随同移交的
已知缺陷披露见 §10（运行时封条破坏：首次引擎启动向包内写入
`__pycache__`，codesign 由 exit 0 转 exit 1；修复＝
`EngineProcessManager.makeChild` 设 `PYTHONDONTWRITEBYTECODE=1`，
落在下一个 Analytics 发布，不回灌本冻结候选）。

## 10. 规定检查与诚实结论

| 检查 | 结果 |
| --- | --- |
| `uv run python scripts/joint_release.py exercise --work-dir dist/g011` | **exit 0**（manifest `stages.exercise: passed`；两链＋回执重传＋最终 App 内置引擎连接/两采集/只读断言全过，`stand_in_app_delivery: false`）。修订复跑（rev4）两次全量执行同判绿：worker 手工复跑（2026-10-18 01:44 本地，`logs/07-exercise.log`）与 ORX 检查轮内复跑（01:46，`logs/09-exercise.log`，即 manifest 当前指向，finished 2026-10-17T17:46:35Z；`goal_1.receipt_retransmit_zero_change: true, receipts: 1`、引擎两采集只读断言复现）。两次复跑前均清理演练暂存目录（exercise-project/engine-data 等，均为驱动可再生材料），GUI/操作者证据未动 |
| `uv run python scripts/joint_release.py verify --work-dir dist/g011` | **exit 0**（manifest `stages.verify: passed`，2026-10-17T16:34 本地首轮：产物逐字节重摘要与冻结绑定一致、样本再验、GUI 证据文件与操作者字段核对、ad-hoc 验签记录核对；六阶段 freeze/battery/build/candidate/exercise/verify 全 passed，报告 `dist/g011/verify-report.json`）。修订复跑（rev4）同样两次全绿（`logs/07-verify.log`、`logs/09-verify.log`——manifest 当前指向，finished 2026-10-17T17:46:35Z；全部产物 sha256 与冻结绑定逐一相同，候选归档 `5187029c…` 字节未变）。ORX 任务门禁最终检查轮（3/3）两命令同轮全绿（check 日志 `.orx/runs/R011/check/T007/01-0014-command.log`、`02-0015-command.log`） |

- **真实发现的缺陷（如实上报，不修复、不绕过）**：安装后的 App
  **首次拉起内置引擎即破坏自身 ad-hoc 封条**——运行时解释器向
  `Contents/Resources/orxanalytics/**/__pycache__/` 写入 73 个
  `*.pyc`（file added）并改写 3 个已封存的 stdlib pyc（file
  modified），`codesign --verify --strict` 由安装时 exit 0 变为
  exit 1（证据：`dist/g011/gui-acceptance/seal-break-codesign.log`、
  `seal-break-pyc-files.txt`、`operator-install-facts.json`）。
  根因：冻结提交的封条防护只覆盖构建期自测（签名前基线＋
  `bundle-mutated-after-signing` 门禁），而
  `EngineProcessManager.makeChild` 的运行时引擎子进程环境**未设
  `PYTHONDONTWRITEBYTECODE=1`/`-B`**。属 Analytics 仓源码缺陷；
  修复将使冻结候选失效（candidate.json invalidation），超出 T007
  范围，交由 Controller 决定（重冻结重验收 or 带已知缺陷发布内部
  预览）。冻结归档 zip 本身字节未变（sha 复核一致），exercise/
  verify 的归档级门禁不受影响。**处置与后续（rev4 披露）**：修复
  定为在 `EngineProcessManager.makeChild` 中设
  `PYTHONDONTWRITEBYTECODE=1`（等价 `-B`），落在**下一个
  Analytics 发布**——本冻结候选按失效规则不回灌修复，内部预览
  按「带已知缺陷＋如实披露」口径发布；该披露与解锁后像素复核项
  （§9.1）一并随发布说明移交，升级到修复版后首启封条保持完整。
  佐证边界：harness 侧拉起的引擎环境始终带
  `PYTHONDONTWRITEBYTECODE=1`，故 rev4 复跑后重解包的演练 App
  （`dist/g011/app/`）验签 exit 0、零新增 pyc——缺陷只命中
  操作者路径（安装后 App 自身拉起引擎），与 §10 缺陷记录一致。
- 未验证边界汇总：隔离 macOS 用户（无凭据）、正式渠道全链（无
  证书/公证凭据）、通知允许/拒绝授权态切换（需人工系统交互，
  会话锁定未能完成）、解锁前面板像素截图（§9；像素级确认已列为
  解锁后约 10 秒人工复核项，§9.1，随发布说明移交）。

复现：本报告全部命令与输出快照存 `dist/g011/gui-acceptance/`；
驱动链复现按 runbook §5；升级链复现按本文 §2（样本、旧代码与命令
均在冻结绑定中）。
