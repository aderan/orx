# ORX 0.4.0 发布说明（兼容组合：ORX v0.4.0 + Analytics v0.4.2）

发布日期：2026-10-17。发布形式：GitHub `aderan/orx` 的 `main` +
轻量 tag `v0.4.0`（与 v0.3.0/v0.3.1 一致；冻结提交与推送由 G011
收尾阶段执行，本文定义发布内容与渠道规则）。升级与回退手册：
[docs/upgrade-0.4.0.md](upgrade-0.4.0.md)。

## 0. 兼容组合与三个版本轴

| 版本轴 | 值 | 说明 |
|---|---|---|
| ORX 软件版本 | **0.4.0** | 首个 0.4.x；单一来源 `src/orx/__init__.py` |
| ORX 数据 schema | **v15** | 读取门接受且仅接受 8–15（docs/observability-contract.md 为权威） |
| Analytics 软件版本 | **0.4.2** | 独立仓库 `aderan/orx-analytics`，0.4.x 收口版本 |
| Analytics 存储 schema | **store v3** | 可从 v1/v2 原地升级；未知未来版本拒绝 |

**经验证的兼容组合是 ORX v0.4.0 + Analytics v0.4.2**：Analytics
0.4.2 只读接受 ORX schema 8–15，未知未来版本拒绝并隔离来源。两个
仓库的软件版本**不必同号**——版本轴各自独立演进。历史标签
（v0.3.0、v0.3.1、analytics 的历史 tag）保持原样；新标签只打在新
冻结的受验提交上，绝不移动或重写旧标签。

## 1. 自 0.3.1 以来的成果

### 1.1 Goal 生命周期控制（schema v13/v14）

`orx goal pause` / `resume` / `cancel`：暂停是持久的 Goal 级状态
（启动门矩阵拦住一切新执行者，在途执行者保留 attempt/跨度/nonce/
会话，事实提交仍落库）；取消是两阶段收口——未启动的工作显式
cancelled，**运行中的执行绝不由沉默推断终结**，只由真实事实（迟到
交付、verdict、`orx task recover --termination-fact`）关闭，Goal
收敛到 `cancelled`，全部历史保持可查。Goal 控制状态与 Run 执行事实
分层表达：`goals.status` 原文呈现，从不改写 `runs.status` 的任务
派生语义。契约见 [docs/goal-lifecycle.md](goal-lifecycle.md)。

### 1.2 幂等完成回执（schema v15）

`orx task complete` 的接受结果持久化为 `completion_receipts`：一次
接受一张、write-once、与 attempt 关闭同事务提交。同一交付重传重放
存储的原响应、零写入；身份相同而内容/元数据冲突则拒绝。回执只是
**重放面**——它不是验证通过的证明（`verifications` 仍是权威），
也不能凌驾当前所有权（被替换/被取消的任务仍按陈旧判定）。Analytics
0.4.2 只镜像回执事实（digest/身份/交付状态/时间），不镜像
`receipt_json` 重放负载。

### 1.3 恢复与用量归属硬化（schema v12 起）

`usage_observations.source` 收 `zcode_db`：host 会话的用量从 zcode
数据库按会话子树只读回读、按 `(session_id, logical_request_id,
attempt_index)` 去重，同 attempt 同源幂等。nonce 权威的会话发现
（`ORX_ASSIGNMENT=<nonce>` 命中即绑定，目录事实记录为
`directory_matched` 而非过滤条件）；结算中的请求行如实计数
（`settling_request_rows`），未结算不占用 per-attempt 单例槽；
closure 结果内嵌 `usage_backfill` 事实。

### 1.4 观测读取面扩展（contract 第八至十一次修正）

读取门白名单扩至 **8–15**；新增 `goal_lifecycle` /
`goal_lifecycle_events` 生命周期读数与回执读取语义；`orx usage`
归一化呈现（cache subset / cache separate 按各 harness 自身语义
派生并标注，缺操作数为 NULL 不补零；是工作量度量，不是计费）。
docs/observability-contract.md 为权威。

### 1.5 Analytics 0.4.2 配套收口

orx-analytics 0.4.2（store v3）与本版构成兼容组合：接受 schema
8–15；Goal 控制状态（active/paused/cancelling/cancelled/done）与
Run 执行事实分层展示（如 paused Goal 下仍在 running 的 Run 交叉
呈现、互不改写）；`orx_receipts` 镜像表只取回执事实。Analytics
保持只读分析层：不承担调度、恢复或重放权；`ORX_ANALYTICS_BIN`
未设置只意味着外部看护不可用，不阻塞 ORX 任何流程。

## 2. 数据库 schema：旧库 → v15

0.4.0 第一次打开 0.3.x 的库时自动迁移到 v15（v8→…→v15 全链纯增
量：replan 六表、attempt 进展、nonce、`zcode_db` 来源、
`paused`/`cancelling` Goal 状态、`completion_receipts` 回执表；不改
既有行、不回填历史）。迁移是复制-替换，任一步失败原库保持原样。
**0.3.x 代码拒绝打开 v15 库**（`schema version 15 is newer than
supported version …; upgrade orx`）。回退唯一安全路径是恢复升级前
的 SQLite 一致性备份——绝不能手改 `meta.schema_version`。备份时点、
失败恢复与回退步骤见升级手册 §5。

## 3. 安装包级验收

`uv run python scripts/check_release.py`（运行手册
[docs/package-acceptance.md](package-acceptance.md)）在干净临时环境
实测：wheel 构建、全新 venv 安装、包内 agents/skills 资源、preset
安装（无 "install manually"）、技能漂移刷新、stand-in Goal 全流程
（legacy evidence 按名拒绝 → 结构化交付被接受 → 独立 verdict →
run done），以及真实 v0.3.0 样例库的 v8→v15 升级、逐行核对、旧版
拒读与备份恢复链——升级期望值锚定 `orx.state.CODE_SCHEMA_VERSION`
（0.4.0 为 15）。发现问题退出非零；无 skip 路径。测试固化在
`tests/test_package_acceptance.py`。

## 4. 升级入口

从 0.3.x 升级：[docs/upgrade-0.4.0.md](upgrade-0.4.0.md)。要点：
暂停写作者；在新版首次打开旧库**之前**做 SQLite 一致性备份（数据库
+ 项目配置 + 运行材料）；editable 用户切 checkout（不做
`uv tool upgrade`），wheel 用户 `orx update`；`orx skill update`
刷新技能；角色定义按显式流程备份、比较、替换；配套 analytics 按
其指南升到 0.4.2（先停自己负责的 watcher、备份数据目录，store
v1/v2 首次打开自动升 v3）。0.3.0→0.3.1 的历史指南保留在
[docs/upgrade-0.3.1.md](upgrade-0.3.1.md)（§3/§4/§7 仍现行）。

## 5. 版本与渠道

- 单一版本源：`src/orx/__init__.py` 的 `__version__`（pyproject 经
  hatch 动态读取，两者不会漂移）。`uv.lock` 的 orx-agent 条目为
  editable 源、不含静态版本，`uv lock --check` 通过。
- 发布渠道沿用现状：push 到 `aderan/orx` 的 `main` 并打轻量 tag
  `v0.4.0`，指向通过全量测试、构建与包级验收的冻结提交；历史标签
  原样不动。
- 内部预览与正式分发的渠道边界已由 G011 T005 收口（决定与依据固定在
  [docs/diagnostics/g011/distribution.json](diagnostics/g011/distribution.json)，
  构建前校验、两渠道结果分槽记录、无静默降级）：内部预览是明确标注
  渠道/受众/Gatekeeper 限制的 ad-hoc zip（App 名 ORX Analytics
  (Internal Preview)，渠道元数据进 Info.plist、包内清单、归档名与逐
  归档构建记录），不是正式安装包；正式通道（dist/DMG）完整实现真实
  Developer ID 嵌套＋外层签名、hardened runtime、notarytool 公证、
  stapler 装订与 spctl 安装评估，凭据缺失（本机无 Developer ID
  Application 证书、无 notarytool 存储凭据，实测同基线）即失败关闭
  ——机器可读原因、不产出归档、不降级为 ad-hoc，不伪造签名/公证
  结果；正式安装说明以 spctl 接受为通过标准，清除 quarantine 永不
  属于正式渠道（仅内部预览渠道的手动确认步骤）。
