# ORX 0.3.1 发布说明

发布日期：2026-10-06。发布形式：GitHub aderan/orx `main` + tag `v0.3.1`
（轻量 tag，与 v0.3.0 一致）。升级与回退手册：
[docs/upgrade-0.3.1.md](upgrade-0.3.1.md)。

0.3.1 是 0.3.0 之后的收尾版本：四个已实现的能力面（下文 §1）、三个
必须缺口的修复（§2）、数据库 schema v8 → v11 的自动迁移（§3），以及
一份在干净安装上实测的安装包级验收（§5）。三个明确延后的事项见 §4
——它们没有实现，也不在本版宣传范围内。

## 1. 四项成果

### 1.1 交付前门禁（delivery gate）

`orx task complete --evidence <file>` 现在校验**结构化交付结果**：
`{status, summary, checks, artifacts}`——`status` 取
`passed | failed | blocked`，`checks` 每项是 `{command, exit_code, log}`
（检查无法运行时 `exit_code`/`log` 为 `null`）。旧版
`{summary, commands, artifacts}` 形状被**按名拒绝**（exit 1，逐字段报
缺），任务状态不变，重交不受影响。门禁只做形状校验；`passed` 仍由
独立 verifier 判定——complete 从来不等于 passed。

### 1.2 重规划差异检查与成果引用（replan precheck + artifact provenance）

新计划版本只能经共享 precheck 门禁生效：`orx plan check --file` 先出
只读差异报告，`orx plan submit --file` 重新跑同一 precheck 后原子激活。
新计划必须声明新旧对应关系（每个新任务 classified `new` / `confirm` /
`redo` / `continue`，每个旧任务有 disposition，`redo` 带具体
`redo_reason`，`confirm` 列出仍须通过的 `confirm_verification`）。先前
成果**只能经声明的对应关系作为 artifact 引用，绝不按任务号引用**；
任务号不是身份。precheck 失败时上一修订保持激活、继续执行。结构检查
不裁决语义——redo 理由是否成立、confirm 是否充分，仍是独立 verifier
与 Controller 的判断。R003 回顾中的 token 节省数字已撤回：ORX 对重规划
不宣称任何经验证的 token 节省。

### 1.3 配额预检与退避（quota preflight + backoff）

`orx quota [--force] [--json]` 报告三个有配额来源的 harness（codex /
cursor / zcode）的尽力而为实时用量，60 秒缓存，`--force` 强制刷新；
绝不启动 agent，不可达的 provider 报 `unknown` 而非抛错。
`orx run` / `orx plan` / `orx verify` 路由前跑同一预检：报告已达限额
的 provider 其 profiles 被 gate 为 `exhausted` 并带重置时间，过期
exhaustion 自动释放，operator 的 `orx resource set` 覆盖永远优先。
`ORX_QUOTA_PREFLIGHT=0` 整体关闭预检。

### 1.4 host 进展报告与会话身份修复（progress reports + session identity）

host worker 用 `orx task heartbeat <task> --attempt <id> --phase ...`
**显式**报告进展：逐条 append-only、绑定 attempt、按 attempt 递增
sequence、ORX 时钟 `received_at`；不覆盖、不回填。`orx status` /
`orx task list` / `orx timeline` / `orx run` 恢复面显示**当前** attempt
的最新报告（`unknown` / `reported` / `overdue`，阈值
`worker.progress_timeout_min`，默认 60 分钟）。`overdue` 只给 CHECK
HINT（先核查原 worker 会话再考虑 fail/retry），不自动 fail、不重试、
不开第二写入者；attempt 的合法迟到交付无论报告多旧都仍被受理。
报告是观测不是存活证明：ORX 不跑定时器、无自动心跳、无 lease、无
keepalive。显式 `task fail` + `task retry` 后新 attempt 开新窗口，旧
attempt 的后续报告与交付被拒（`attempt_closed` / `attempt_superseded`）。

会话身份：`task claim --discover-session` 把 host worker 的真实会话
身份绑定到 attempt（对 zcode 会话库的确定性只读检索）；park 不再盖
controller 的环境引用；`task complete` / `task fail` 支持 `--session`
迟到绑定。schema v11 给 attempt 加一次性 nonce 身份锚（verifier 跨度
与发现、replan 关闭孤儿 attempt），修复旧版本把不同会话误判为同一
worker 的观测错位。

## 2. 三个必须缺口的修复

1. **安装包补齐原生角色定义**：wheel 现在携带 `orx/agents/`
   （orx-worker、orx-verifier、orx-verifier-strong）。0.3.0 干净安装执行
   `orx preset install zcode` 会报
   `definition not packaged with this ORX (install manually)`；0.3.1 起
   preset 直接安装，且已有文件永不覆盖（`preserved existing`）。
2. **角色定义与交付协议对齐**：`agents/orx-worker.md` 的 TASK 段改为
   示范现行结构化交付结果（旧形状正是 §1.1 门禁按名拒绝的对象），
   并补齐 START GATE / BLOCKED EXIT / 同会话 CHECK-FIX LOOP /
   DELIVERY GATE、claim 与 attempt 身份、heartbeat 进展报告规则；
   三个角色定义随源码统一更新。
3. **去本机路径依赖**：controller 技能的 analytics 看护不再硬编码
   `~/Sources/Tools/orx-analytics` 本机路径，改为只认显式配置的
   `ORX_ANALYTICS_BIN`（绝对路径或 PATH 上的名字），无默认位置、不探测
   checkout 目录；未配置、未安装或启动失败时跳过看护并照常继续 ORX
   循环，不阻塞、不循环重试。

## 3. 数据库 schema v8 → v11

0.3.1 第一次打开 0.3.0 的库时自动迁移 v8 → v9（replan 对应关系六张
表）→ v10（`attempt_progress`）→ v11（`attempts.nonce`）。三步全部
纯增量，不改既有行、不回填历史。迁移是复制-替换（先 WAL checkpoint、
在副本上执行、成功后原子替换），任一步失败原库保持原样。**0.3.0 代码
拒绝打开 v11 库**（`schema version 11 is newer than supported version 8;
upgrade orx`）。回退唯一安全路径是恢复升级前的 SQLite 一致性备份——
绝不能手改 `meta.schema_version`。备份时点与步骤见升级手册 §6。

## 4. 明确延后（不在本版，也未宣传）

- **状态页对应关系呈现**：replan 对应关系目前在 `orx plan check` 差异
  报告与数据库中，`orx status` 尚不呈现；
- **后台自动恢复**：`overdue` 只提示核查原会话，确认死亡与 fail/retry
  是人的决定，ORX 不自动执行；
- **并行 worktree**：写入执行仍为串行（有效 CLI 并行为 1）。

## 5. 安装包级验收

`uv run python scripts/check_release.py`（运行手册
[docs/package-acceptance.md](package-acceptance.md)）在干净临时环境
实测：wheel 构建、全新 venv 安装、包内 agents/skills 资源探针、preset
安装（无 "install manually"）、技能安装与漂移刷新、stand-in Goal 全
流程（legacy evidence 按名拒绝 → 结构化交付被接受 → 独立 verdict →
run done，exit codes 0/1/2 与 `--json` envelope 断言），以及真实
v0.3.0 样例库（tag `v0.3.0` 代码生成）的 v8→v11 升级、逐行数据核对、
旧版本拒读与备份恢复链。发现问题退出非零；无 skip 路径。测试固化在
`tests/test_package_acceptance.py`。

## 6. 升级入口

从 0.3.0 升级：[docs/upgrade-0.3.1.md](upgrade-0.3.1.md)。要点：暂停
写作者；在新版首次打开旧库**之前**做 SQLite 一致性备份（数据库 +
项目配置 + 运行材料）；editable 用户切 checkout（不做
`uv tool upgrade`），wheel 用户 `orx update`；`orx skill update` 刷新
技能；三个角色定义按 §3 备份、比较、显式替换（保留自定义）。

## 7. 版本与渠道

- 单一版本源：`src/orx/__init__.py` 的 `__version__`（pyproject 经
  hatch 动态读取，两者不会漂移）。`uv.lock` 的 orx-agent 条目为
  editable 源、不含静态版本，`uv lock --check` 通过。
- 发布渠道沿用现状：push 到 `aderan/orx` 的 `main` 并打轻量 tag
  `v0.3.1`，指向通过全量测试、构建与包级验收的发布提交。
