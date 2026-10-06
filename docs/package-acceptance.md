# 安装包级验收（0.3.1，G007 T004）

状态：随 G007 T004 建立。本文是安装包级验收的运行手册：入口、依赖、
fixture 来源、链路内容、隔离保证与失败退出行为。实现全部在
`scripts/check_release.py`（链路各段是可导入的普通函数），测试固化在
`tests/test_package_acceptance.py`；两者共用同一次
wheel 构建 + 临时 venv 安装，验收对象永远是**构建出的 wheel**，不是源码
检出，也没有任何 skip 路径（每一段在每次运行时都会执行）。

## 1. 入口

| 入口 | 命令 | 说明 |
| --- | --- | --- |
| 测试 | `uv run pytest -q tests/test_package_acceptance.py` | 12 个断言，模块级复用一次构建/安装/链路 |
| 脚本 | `uv run python scripts/check_release.py` | 打印完整 JSON 报告；发现问题退出非零 |
| 脚本（保留产物） | `uv run python scripts/check_release.py --workdir <dir>` | 构建/venv/scratch 产物保留在指定目录便于排查 |
| fixture 再生成 | `uv run python scripts/check_release.py --generate-fixture tests/fixtures/release-0.3.0` | 用真实 v0.3.0 代码重建样例库并重写 PROVENANCE.md |

`--workdir` 缺省时使用全新临时目录并在成功后删除。

## 2. 依赖与前置

- `uv`（构建 wheel、创建临时 venv 并安装；typer/pydantic 依赖来自本地
  uv 缓存或既有网络权限，与源码开发环境一致）；
- `git`，且本地存在 tag `v0.3.0`（commit
  `0f2629e6988ba5082a876257c910d78457bd1338`）——fixture 的再生成与
  旧版本拒读检查都要 `git archive v0.3.0`；
- venv 解释器 Python ≥ 3.12（v0.3.0 的 `requires-python`；即本仓库
  开发环境的解释器）；
- **不需要网络**：配额预检关闭（`ORX_QUOTA_PREFLIGHT=0`），不调用
  `orx quota` / `orx watch` / analytics 看护；stand-in 的 host 工作全部
  停靠为 `host_required`，不启动任何付费模型。

## 3. 链路内容

全部步骤在仓库外的临时目录里，通过临时 venv 的 `<venv>/bin/orx` 控制台
脚本或其解释器执行：

1. **构建**：`uv build --wheel` 到调用方目录（不碰仓库 `dist/`）；
2. **干净安装**：全新 venv + wheel 安装；无 editable、仓库不在
   `sys.path`，`orx` 只能来自临时 site-packages；
3. **包内资源探针**：从仓库外的 scratch 目录、隔离环境下 import，
   报告 agents/skills 的解析位置；三个原生角色定义与默认技能必须在
   wheel 内；
4. **preset**：全新用户层安装三个角色（无 "install manually" 注记，
   与打包副本逐字节一致）；已有自定义 `orx-worker.md` 时保留不动；
5. **技能**：从安装包枚举全部打包技能（orx-agent、orx-controller、
   orx-pbv）并安装；对一个安装副本制造漂移后 `orx skill update` 必须
   从包内副本刷新（逐字节一致）；
6. **stand-in Goal**（`run_cli_flow` 的 `standin` 段）：init → goal →
   plan submit → run（停靠 host 工作）→ claim → heartbeat → 旧版
   `{summary, commands, artifacts}` evidence 被**按名拒绝**（exit 1，
   任务状态不变）→ 结构化交付结果被接受 → `orx verify submit` 独立
   verdict → T002 交付 → run `done`。全程 `--json` envelope 与
   exit codes 0/1/2 断言（1 = 领域拒绝，2 = click 用法错误）；
7. **fixture**：`tests/fixtures/release-0.3.0`（见 §4）；验收先做结构
   检查（schema v8、无 v9+ 表、无 nonce 列、核心表非空、PROVENANCE
   记录的 sha256 与文件一致），再做再生核对（用真实 v0.3.0 代码重跑
   生成，归一化逻辑 dump——屏蔽生成式 uuid/时间戳与生成根路径——必须
   与已提交 fixture 完全一致）；
8. **升级链**（`check_upgrade_chain`，docs/upgrade-0.3.1.md §6 的机器
   执行）：复制 fixture（源样例只读）→ `VACUUM INTO` 一致性备份 →
   已安装新版以 `orx status` 打开副本触发 v8→v11 自动迁移 → 对全部
   既有表的全部既有列逐行核对身份与数据（`meta` 仅
   `schema_version` 8→11）→ 新增面默认值检查（新增表精确等于
   replan 六表 + `attempt_progress` 且为空；`attempts` 仅增 `nonce`
   列且旧行全 NULL）→ **真实 v0.3.0 读取入口**（其自身 CLI，经
   `git archive` 源码在 wheel venv 解释器上运行）拒读 v11 库（exit 1，
   `schema version 11 is newer than supported version 8`）且文件字节
   不变 → 备份恢复到新目录后由真实 v0.3.0 读回样例 Goal → 源样例
   sha256 前后一致。

## 4. fixture 来源与再生成

`tests/fixtures/release-0.3.0/` 提交四个文件：`state.db`（schema v8 样例
库）、`config.toml` / `profiles.toml`（v0.3.0 `init` 写出的项目默认层）、
`PROVENANCE.md`（来源与摘要记录）。

- 来源：本仓库 tag `v0.3.0`（commit `0f2629e6…`）的 `git archive`。
  数据库**只由 v0.3.0 自己的 `Store`/`dispatch` 代码写入**（生成脚本
  断言 `orx.__version__ == "0.3.0"`），不是把当前 schema 手工降级冒充；
  验收的再生核对（§3 第 7 步）每次运行都会重新证明这一点。
- 样例内容：一个 Goal / 一次规划 / 两个任务；T001 以 v0.3.0 时代的
  legacy evidence 交付并通过命令与 agent 双验证；T002 已 claim、处于
  RUNNING——即升级文档 §6 描述的"升级时刻还有在途任务"的真实切面。
  `planning_assignments` / `external_events` / `inbox_items` 为空（这些
  表在 v0.3.0 只由 host 规划分发或 GitHub watch 写入，属网络路径，
  验收不触发；PROVENANCE 如实记录各表行数）。
- 升级/恢复检查只操作副本：`state.db` 以 immutable 只读方式打开
  （不产生 `-wal`/`-shm` 伴生文件），结束后 sha256 必须与 PROVENANCE
  记录一致。
- 再生成会改变 uuid/时间戳（内容等价但字节不同），因此**不要**为了
  "刷新" 而随手再生成；只有生成逻辑本身变化时才再生成并更新
  PROVENANCE。

## 5. 隔离保证

链路中每个子进程环境都是重新构造的最小集合：`HOME` 指向 scratch，
`ORX_CONFIG_DIR` / `ORX_DATA_DIR` / `ORX_ZCODE_AGENTS_DIR` 指向 scratch
子目录，`ORX_QUOTA_PREFLIGHT=0`，无 `PYTHONPATH`、不继承任何 `ORX_*`。
技能 canonical 目录（`~/.agents/skills`）与 symlink 目标因此落在
scratch HOME 下；真实用户配置、数据、角色目录、技能目录与当前项目的
`.orx/state.db` 不会被读写。v0.3.0 代码运行在 wheel venv 解释器上、
`sys.path[0]` 指向归档源码，typer/pydantic 来自 wheel 自身依赖集，
满足 v0.3.0 声明的版本范围。

## 6. 失败退出行为

- 任一段发现问题时，`scripts/check_release.py` 在 stderr 逐条打印
  `problem: …` 并以 **exit 1** 退出（stdout 仍是完整 JSON 报告）；
- 链路段自身无法运行（构建失败、venv 安装失败、git archive/tag 缺失、
  生成器或探针非零退出）抛 `ReleaseCheckError`，打印
  `{"error": …}` 并以 **exit 1** 退出；
- 命令行参数错误（argparse）为 **exit 2**；
- 测试入口（pytest）以断言失败呈现，没有 skip：fixture 缺失、tag 缺失、
  结构不符、升级差异、拒读缺失、备份不可恢复都会让对应测试失败。
