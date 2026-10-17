# G011 联合验收运行手册（T004）

状态：T004 交付的权威运行手册。实现全部在 `scripts/joint_release.py`
（各阶段是可导入的普通函数），行为固化在 `tests/test_joint_release.py`；
单仓安装包链（wheel 内容、preset、升级链）仍在
`scripts/check_release.py` / `tests/test_package_acceptance.py`（见
docs/package-acceptance.md）。本手册只覆盖两仓联合层：冻结、构建、演练、
验证、分发核对。

## 1. 入口与阶段

| 阶段 | 命令 | 说明 |
| --- | --- | --- |
| 自检 | `uv run python scripts/joint_release.py self-test --work-dir dist/g011/harness-selftest` | 全套驱动机制 + 替身 App 交付（真实冻结提交、真实 wheel、App 自己的引擎 serve 入口）；报告明确声明**不主张产品验收** |
| 冻结 | `uv run python scripts/joint_release.py freeze [--work-dir dist/g011] [--channel internal-preview\|official]` | 解析两仓完整提交、从提交读版本、校验样本溯源（含旧代码再生比对）、记录渠道与运行时，写 manifest.json |
| 构建 | `uv run python scripts/joint_release.py build [--build-app\|--app-archive PATH]` | 从冻结提交的一次性 `git archive` 检出构建 ORX wheel 与 Analytics sdist/wheel；绑定最终 App 归档。缺 App 归档即 fail-closed（App 是 T006 的交付物） |
| 演练 | `uv run python scripts/joint_release.py exercise` | 冻结 wheel 装入一次性 venv，通过已安装 CLI 驱动隔离项目的暂停/继续/取消/暂停期完成/回执重传，并连接**最终 App 内置引擎**（App 自带 serve 入口 + socket 协议 v1）完成两次采集与只读断言 |
| 验证 | `uv run python scripts/joint_release.py verify` | 重算全部产物 sha256、核对产物身份与冻结绑定、要求全部阶段证据与日志、要求 App/GUI/签名证据；任何缺失即失败，**绝无 skip 后通过** |
| 远端核对 | `uv run python scripts/joint_release.py verify-remote [--channel-root DIR]` | 消费者侧：核对已发布渠道根（T008 发布）中产物逐字节一致、绑定一致、渠道隔离（内部预览不带 DMG、正式不带 dev zip）。渠道根不存在即 fail-closed |
| 样本再生成 | `uv run python scripts/joint_release.py generate-samples --dest tests/fixtures/joint-release` | 用**真实旧版提交**重建 Analytics store v2 样本并重写 PROVENANCE.md |

退出码：0 阶段绿；1 阶段失败（JSON 到 stderr，含稳定机器码，同时如实
记录进 manifest 的 stages 段）；2 用法错误。

## 2. manifest 绑定（清单即验收锚）

`<work>/manifest.json` 的 `binding` 段绑定：

- **两仓完整源码提交**：`orx.commit` / `analytics.commit`（全 sha）；
  版本 `orx.version` / `analytics.version` 一律从提交内容读出
  （`git show <commit>:src/.../__init__.py`），**绝不读工作区**；
- **渠道**：`channel`（internal-preview / official）；构建产物风味映射
  internal-preview→dev zip、official→dist DMG（`CHANNEL_APP_MODE`）；
- **依赖运行时**：`runtime.harness_python` / `harness_sqlite` / `uv` /
  `platform`、App 固定运行时（从冻结提交的 build_app.sh 解析的
  python-build-standalone 名与 sha256）、构建后补充
  `runtime.orx_dependencies`（冻结 wheel 装入一次性 venv 后的依赖闭包）；
- **样本**：`samples.orx_old`（指向既有 `tests/fixtures/release-0.3.0`，
  记 tag/commit/sha256/schema 8）与 `samples.analytics_store_v2`
  （真实旧提交、生成命令、sha256、store_version=2）；
- **最终产物 SHA256 与日志引用**：`artifacts.*.sha256` +
  `artifacts.*.built_from`；`stages.<stage>.log` 指向
  `<work>/logs/NN-<stage>.log`（验证阶段逐一核对存在性）。

同一 HEAD 上重复 freeze 的 `binding` 内容完全一致（时间戳除外）——
测试钉住这一点。

## 3. 一次性检出与"未提交内容不进构建"

- 所有构建/验证检出都是 `git archive <commit>` 解包：只含提交树，无
  `.git`、无工作区文件。原工作区的未提交报告（含受保护的
  docs/m1.2-report.md）在任何情况下都进不了构建。
- freeze 如实记录两仓工作区脏文件数（`worktree.dirty_files`）——记录
  是为了可审计，排除靠的是 archive 语义本身。
- 历史标签（v0.1.0…v0.3.1，尤其 v0.3.0=0f2629e6…）保持原样：freeze
  校验 v0.3.0 仍指向 PROVENANCE 记录的提交，现有 package acceptance
  链继续可用；常量与 check_release.py 锁步（测试断言相等）。

## 4. 样本来源（真实旧版，绝不冒充）

- **旧 ORX 样本**：既有可追溯 fixture `tests/fixtures/release-0.3.0`
  （真实 v0.3.0 代码产生，schema v8；来源见其 PROVENANCE.md）。联合
  fixture 只引用不复制。
- **Analytics store v2 样本**：`tests/fixtures/joint-release/analytics-store-v2/`
  由**真实旧版提交** `3f1b66c0234db3cf84d6c2957e34d00bab1b5cee`
  （0.4.1，`STORE_VERSION="2"`；store v2→v3 迁移落在其后的 07ab676，
  故这是最后一个 store-v2 时代提交；analytics 仓无标签，完整提交即锚）
  产生：`git archive` 该提交 → 其自身 `Store`/`registry`/
  `import_orx_project` 写 analytics.db（生成器断言
  `orxanalytics.__version__ == "0.4.1"`、读回 `store_version == "2"`），
  导入源为 release-0.3.0 fixture 的暂存副本（rollback journal 规范化，
  只读；源 fixture sha256 前后断言不变）。
- PROVENANCE.md 记录提交、生成命令、表行数与 sha256。**再生核对**：
  freeze/verify 每次都用旧代码重跑生成，比对归一化逻辑 dump（屏蔽
  生成式 uuid/时间戳/绝对路径）——已提交样本必须是旧代码的真实产物，
  不是改当前库版本号冒充。再生成只应因生成逻辑变化而执行。
- **升级与拒读**：当前 0.4.2（克隆 venv）打开 store v2 样本**副本**原地
  升级到 v3（新增 orx_receipts 镜像、行保留、源字节不变），未来
  store_version=9 拒绝；旧 0.4.1 代码面对当前 schema v15 项目按闸门
  拒绝（源码回退导入）且源不动。ORX 侧 v8→v15 升级链继续由
  check_release.py 负责。

## 5. exercise 覆盖（已安装 wheel + 最终 App 内置引擎）

通过冻结 wheel 装入的一次性 venv 的 `orx` 控制台脚本，在仓库外隔离
项目上（全新 HOME、ORX_CONFIG_DIR/ORX_DATA_DIR/ORX_ZCODE_AGENTS_DIR、
ORX_QUOTA_PREFLIGHT=0、无 PYTHONPATH）：

1. **暂停/继续**：claim 后 `goal pause`（changed=true）；暂停期
   `orx run` 被门禁拒绝（exit 1，envelope 如实说 paused）；heartbeat
   仍受理；
2. **暂停期间完成**：在暂停中交付 T001（passed），Goal 保持 paused；
   引擎此刻采集（collect #1）必须呈现分层事实——Run 仍在 active、
   Goal 控制态原文 `paused` 镜像、绝不冒充 done；
3. **回执重传**：同一交付在新进程重传——纯读重放，state.db 字节不变、
   completion_receipts 恰 1 行；
4. resume→T002→独立 verdict→goal/run done；
5. **取消**：第二个 Goal 带存活执行者取消（cancelling + unconfirmed
   列表）、`task recover --termination-fact` 收敛为 cancelled、
   history/usage 仍可查；collect #2 后已取消 Goal 绝不聚合进
   "最近完成"；
6. **引擎连接**：解包最终 App 归档，经 **App 自带的**
   `orx_engine_serve.py` 入口（捆绑运行时、隔离 data-dir、
   ORX_ANALYTICS_NOTIFY=0、socket 协议 v1）连接；注册的只有隔离
   项目；两次 refresh 采集（变化源恰 2 个 snapshot、不改源字节、
   zc_/cx_/cur_ 镜像为空——真实会话隔离）。

## 6. 失败关闭（无 skip 后通过）

稳定机器码（`StageFailure.code`，测试逐一钉住）：

`app-artifact-missing`（build/exercise 缺最终 App；信息指名 T006）、
`missing-step-evidence`（前置阶段缺失或日志文件缺失）、
`skipped-not-accepted`（任何 skipped 记录都是失败）、
`artifact-hash-mismatch`（哈希变化）、`artifact-identity-mismatch`
（产物版本与冻结绑定不符——源码回退/错误产物）、`commit-mismatch`
（产物不是从冻结提交构建）、`channel-artifact-mismatch`（渠道风味错）、
`gui-evidence-missing`（缺操作者 GUI 证据——自动化**不弹 GUI、不发
系统通知**，GUI 证据只能由操作者提供）、`signature-evidence-missing`
（内部预览至少需可验证的 ad-hoc 签名；正式渠道必须 Developer ID +
公证）、`channel-root-missing` / `remote-hash-mismatch` /
`remote-binding-mismatch` / `channel-separation-violation`（分发侧）、
`historical-tag-moved`（历史标签被移动）、`samples-invalid`。

失败同样写进 manifest（status=failed + code），`orx task` 式的诚实失败
可被后续阶段看到。

## 7. 隔离与运行材料

- 每个子进程环境都是重构的最小集合（见 §5）；analytics 层通过
  ORX_ANALYTICS_HOME/--data-dir 隔离、ORX_ANALYTICS_NOTIFY=0；引擎
  socket 路径过长时自动改道短临时目录并在停止时把证据带回工作目录。
- **兼容性事实（T004 实测）**：SQLite ≥ 3.52 在最后一个连接关闭时会
  清理 WAL sidecar，使静止 ORX 项目对 analytics 的 `mode=ro` 只读打开
  失败（"unable to open database file"）。驱动因此把演练 venv 钉在
  harness 自身解释器（版本与 SQLite 记录进 manifest.runtime），App 侧
  捆绑的是固定 3.12.8 运行时不受影响。
- 运行材料放 `dist/g011/`（git 忽略）；`<work>/EVIDENCE-INDEX.md` 是
  **脱敏证据索引**（相对路径、无环境转储、无绝对用户路径），提交的
  报告只引用它。manifest/EVIDENCE-INDEX 每阶段自动刷新。
- 引擎进程由驱动全生命周期管理：启动即登记，阶段退出/异常统一停止
  （`stop_all_engines`），不留持有 flock 的孤儿引擎。

## 8. 当前状态与后续（T006–T008 衔接）

- self-test 绿：冻结/样本/升级链/拒绝路径/替身演练全过，报告明确
  `product_acceptance_claimed: false`。
- build/exercise/verify/verify-remote 现在**按设计 fail-closed**：最终
  App 归档尚不存在（T006 经冻结检出的 `scripts/build_app.sh` 构建，
  或 `--app-archive` 绑定）；GUI/签名证据由 T007 操作者提供；渠道根由
  T008 发布。届时从冻结提交起全链路重跑即可，无需改驱动。
