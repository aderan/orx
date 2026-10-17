# 升级到 ORX 0.4.0（含 Analytics 0.4.2 配套）

状态：由 G011 T003 建立（2026-10-17），是**现行**升级指南。0.3.1 的
升级指南（[docs/upgrade-0.3.1.md](upgrade-0.3.1.md)）转为历史文档，
其备份/迁移/回退方法论被本文沿用并扩展到 v15 与 Analytics store v3。

本文面向从 0.3.x（0.3.0 的 schema v8、0.3.1 的 v11，以及 0.3.1 之后
未发布树上的 v12/v13/v14 库）升级到 0.4.0 的用户，并覆盖配套分析工具
orx-analytics 升级到 0.4.2（store v3）的可选路径。

## 0. 版本与兼容组合（三个独立的版本轴）

| 版本轴 | 当前值 | 说明 |
|---|---|---|
| ORX 软件版本 | **0.4.0** | 单一来源 `src/orx/__init__.py` 的 `__version__`（pyproject 经 hatch 动态读取） |
| ORX 数据 schema | **v15** | `.orx/state.db` 的 `meta.schema_version`；读取门（observability contract）接受且仅接受 8–15 |
| Analytics 软件版本 | **0.4.2** | 独立仓库、独立工具，与 ORX 不必同号 |
| Analytics 存储 schema（store） | **v3** | analytics 自己的 `analytics.db`；可从 v1/v2 原地升级，未知未来版本拒绝 |

**经验证的兼容组合：ORX v0.4.0 + Analytics v0.4.2**（store v3）。
orx-analytics 0.4.2 接受 ORX schema **8–15** 的只读快照，拒绝未知未来
版本（隔离该来源并告警，绝不代为迁移）。两个仓库的软件版本不必同号；
历史标签（v0.3.0、v0.3.1……）保持原样，绝不移动或重写。

0.4.0 相对 0.3.1 的数据库变化（全部纯增量，见 §5.2）：v12 收
`zcode_db` 用量来源；v13/v14 收 `paused`/`cancelling` Goal 状态；
v15 新增 `completion_receipts` 幂等回执表。

## 1. 升级前确认

- 记录当前版本：`orx version`（0.3.x 均可按本文升级）。
- 确认安装形态：本地 editable（`uv pip install -e`）或 wheel 安装
  （`orx update --check` 会报告安装来源与升级路径）。
- **暂停写作者**：升级开始前，让所有会写这个项目 `.orx/state.db` 的
  进程收尾或显式失败重试——正在运行的 ORX 会话（host worker /
  verifier 子代理、controller 循环）、`orx run`、任何挂着该项目的
  终端。确认没有进程再持有该数据库的写连接后再动手。原因见 §5.1：
  新版本第一次打开旧库就会迁移并替换数据库文件，迁移之后写入的任何
  数据都不在备份里，迁移期间的并发写入也可能被替换操作丢掉。
- 若使用 orx-analytics：先停止**自己负责**的 watcher（§4）；不是
  自己启动的实例不要动。

## 2. 更新 ORX 本体（分渠道）

两个渠道共用同一顺序，差异只在第 3 步：

1. 暂停写作者（§1）。
2. 备份（§5.1）：数据库一致性备份 + 项目配置 + 运行材料。**必须发生
   在新版首次打开旧库之前。**
3. 把 ORX 本体切到 0.4.0（分渠道，见下）。
4. 用任意 orx 命令（如 `orx status`）首次打开旧库，触发自动迁移到
   v15（§5.2）。
5. 技能刷新：`orx skill update`（与 0.3.1 相同，只刷新已安装技能，
   不触碰角色定义文件）。
6. 角色定义显式更新：备份、比较、替换——流程与 0.3.1 相同，见
   [docs/upgrade-0.3.1.md](upgrade-0.3.1.md) §3（该节是现行流程，
   历史文档中唯一仍通用的部分就是 §3 与 §4）。
7. 升级后验收（§6）。

### 2.1 本地 editable 用户

```sh
git fetch && git checkout v0.4.0   # 或对应发布提交
uv sync                            # 仅当依赖锁有变化时需要
```

- editable 安装直接跟踪源码 checkout，切到新 tag 后本体即是 0.4.0，
  无需重装。
- **不做 `uv tool upgrade`**：`orx update` 对 editable 安装会直接拒绝
  （"orx is an editable uv tool install; upgrade by pulling the source
  checkout and reinstalling, not via `uv tool upgrade`"）。本渠道没有
  `uv tool upgrade` 这一步。

### 2.2 wheel 用户（uv tool / pip 安装）

- uv tool 安装：`orx update`（内部执行 `uv tool upgrade orx-agent`），
  或手动 `uv tool upgrade orx-agent`。先 `orx update --check` 可确认
  安装来源与将执行的命令。
- 其他 wheel 安装方式（pip / venv）：按你原来的安装命令安装 0.4.0 的
  wheel 覆盖旧版。
- 安装完成后的首次运行即触发 §5.2 的自动迁移——所以 §5.1 的备份必须
  在安装/首次运行之前完成。

## 3. 技能与角色定义

与 0.3.1 的口径完全一致，流程见
[docs/upgrade-0.3.1.md](upgrade-0.3.1.md) §3–§4：技能只经
`orx skill update` 刷新；角色定义（orx-worker / orx-verifier /
orx-verifier-strong）的更新是**显式**的备份、比较、替换——preset 永远
不覆盖已存在的角色文件，漂移由 `orx doctor` 的 `agent_def:*` 检查点名。
两个入口分开，谁也不代劳谁。

## 4. orx-analytics 0.4.2 配套升级（可选工具）

Analytics 是独立工具：不装它，ORX 0.4.0 的一切照常；装了旧版
analytics（0.4.1 及更早），按本节与它自己的
[安装/升级指南](https://github.com/aderan/orx-analytics/blob/main/docs/installation.md)
升级到 0.4.2。

1. 停止**自己负责**的 watcher：`orx-analytics watch stop`（使用自定义
   数据目录时所有命令带同一 `--data-dir` / `ORX_ANALYTICS_HOME`）。
2. **备份整个分析数据目录**（默认 `~/.orx-analytics/`，含
   `analytics.db`、watch 状态、findings）——analytics 首次打开可能
   迁移它自己的存储（下条），旧工具未必能读新库，回退靠这份备份。
3. 升级工具本体：`uv tool upgrade orx-analytics`（uv/pipx/venv 渠道
   见其安装指南），`orx-analytics --version` 应报 **0.4.2**。
4. **store v1/v2 → v3 原地自动迁移**：0.4.2 首次打开 v1 或 v2 的
   `analytics.db` 时自动逐级升到 v3（v2→v3 增量加入 `orx_receipts`
   回执镜像表；v1→v2 加入 `attempts.nonce` 镜像列），既有行原样保留、
   不回填。迁移只发生在 analytics 自己的数据目录里，**绝不触碰任何
   ORX 或 agent 源库**（源库永远 `mode=ro` + `query_only`）。
5. **未知未来版本拒绝**：`store_version` 大于 3 的库（来自更新的
   analytics）被硬性拒绝（`analytics.db store_version N != 3; ...`），
   不会猜测、不会降级。回退到旧版 analytics = 恢复第 2 步的数据目录
   备份（先把本体装回旧版，再整目录恢复）。
6. 验收：`orx-analytics probe --project /path/to/orx-project` 应把
   ORX 项目的 schema（现在到 v15）报为受支持；`collect` 不再因 v13/
   v14/v15 隔离该项目。

边界（与代码一致）：0.4.2 只**镜像** ORX v15 回执的事实列（digest、
身份、交付状态、时间），不镜像 `receipt_json` 重放负载——回执是补交
方的重放面，分析层只读不重放；Analytics 不因此获得任何调度、恢复或
迁移权限。ORX 侧的 `ORX_ANALYTICS_BIN` 未设置只意味着外部看护不可用，
**不阻塞任何 ORX 流程**（controller 说明一次后照常继续）。

## 5. 数据库 schema 升级与回退

### 5.1 升级前备份（在新版首次打开旧库之前）

**时点**：新版本第一次打开旧库就会自动迁移（§5.2），迁移把库文件
改写为 v15；而回退的唯一安全路径是恢复升级前备份（§5.5）。所以备份
必须发生在"新版第一次打开旧库"之前——对 editable 用户是切换
checkout 之后的第一条 orx 命令之前，对 wheel 用户是安装新版后的首次
运行之前。暂停写作者（§1）之后、切换版本之前做备份最稳妥。

**数据库：用 SQLite 一致性备份，不要只 `cp` 主文件**。ORX 数据库以
WAL 模式运行，已提交的数据可能仍留在 `state.db-wal` 里，直接复制主
文件会漏掉这些数据。`VACUUM INTO` 与 `.backup` 都经由 SQLite 的事务
一致快照产出**单文件**备份（无 `-wal`/`-shm` 伴生文件）：

```sh
mkdir -p backup-0.3.x
sqlite3 .orx/state.db "VACUUM INTO 'backup-0.3.x/state.db'"
# 或等价：sqlite3 .orx/state.db ".backup 'backup-0.3.x/state.db'"
```

（`VACUUM INTO` 要求目标文件不存在；每次备份换一个新名字或先清空
目录。）

**同时保存项目配置与必要运行材料**——它们不在 `state.db` 里，回退时
同样要恢复：

```sh
cp .orx/config.toml .orx/profiles.toml backup-0.3.x/ 2>/dev/null || true
cp -R .orx/runs backup-0.3.x/runs
```

### 5.2 自动迁移：旧库 → v15

- 新版第一次打开旧库时**自动执行**，没有也不需要专用迁移命令；任何
  orx 命令（`orx status` 即可）都会触发。迁移从旧库自己的版本起步
  逐级执行：v8 → v9（replan 六表）→ v10（`attempt_progress`）→
  v11（`attempts.nonce`）→ v12（`usage_observations.source` CHECK 收
  `zcode_db`）→ v13（`goals.status` 收 `paused`）→ v14（收
  `cancelling`）→ v15（`completion_receipts` 回执表）。从 v11/v12/
  v13/v14 起步的库只执行缺失的步骤。全链纯增量：不改既有表的行、
  不回填历史——升级库的 replan/进度/回表态与新建库的可观测行为一致。
- 迁移策略是**复制-替换**（`src/orx/state.py` 的 `_migrate_copy`）：
  先做 WAL checkpoint，把主文件连同 `-wal` 伴生文件复制到同目录临时
  副本，在副本上依次执行迁移，全部成功、WAL 折回后才原子替换原文件。
- 迁移是升级动作里唯一改写数据库的步骤；它不改 `.orx/config.toml`、
  `.orx/profiles.toml` 与 `.orx/runs/`。

### 5.3 失败恢复

- 迁移中任一步失败：临时副本被删除，**原库保持原样**、仍可被旧版本
  打开——不存在"半迁移"状态。排查后（磁盘空间、文件权限、备份目录
  不可写等）再次用任意 orx 命令打开即整体重试。
- 迁移成功后才算升级完成；迁移后请立即按 §6 验收。若验收发现新版有
  阻断问题，按 §5.5 从备份回退（新版写过的数据会随回退丢弃，重要
  产出先导出）。

### 5.4 旧版本拒读新版库

0.3.x 代码打开 v15 库会立即报错退出——
`schema version 15 is newer than supported version 8; upgrade orx`
（0.3.1 报 `... than supported version 11`，具体数字随旧版本）——
且不会改动该文件。这是硬性拒绝，不是可绕过的提示：v15 库只能被支持
v15 的 ORX 读写。

### 5.5 回退：必须恢复升级前备份

- **唯一安全的回退路径**：本体回到旧版本（旧 tag / 旧 wheel），然后把
  §5.1 的备份完整恢复——数据库、项目配置、运行材料一起。
- **不可用旧版本直接打开迁移后的库**（§5.4 会拒绝）。
- **never hand-edit `schema_version`**：绝不通过手改
  `meta.schema_version` 来"降级"。版本号只是声明，改数字不会删除
  v9–v15 新增的表和列；旧代码会在错误的版本声明上继续读写，且旧
  版本再次打开时还会按它认识的版本重跑迁移。回退 = 恢复备份，
  没有捷径。
- 恢复前确认没有 orx 进程在写该项目；恢复时把迁移后的库连同可能
  残留的 WAL 伴生文件一起移除——恢复的主文件不能与迁移期残留的
  `-wal` 配对，否则 SQLite 可能重放出不属于该文件的帧（§5.1 的一致
  性备份本身是单文件快照，恢复后首次打开会按需重建 WAL）：

  ```sh
  # 本体已回退到旧版本后：
  rm -f .orx/state.db .orx/state.db-wal .orx/state.db-shm
  cp backup-0.3.x/state.db .orx/state.db
  cp backup-0.3.x/config.toml backup-0.3.x/profiles.toml .orx/ 2>/dev/null || true
  rm -rf .orx/runs && cp -R backup-0.3.x/runs .orx/runs
  ```

- 回退后旧版本打开的是它认识的库，一切照旧。备份时点到回退时点之间
  产生的写入随回退丢弃——回退窗口内的重要产出需另行导出保存。

## 6. 升级后验收

- `orx version` 报 **0.4.0**；`orx doctor` 的 `agent_def:*` 三项为 ok
  （或如实在 warn 提示缺失），skill 两项按需安装；
- 数据库迁移确认：`sqlite3 .orx/state.db "SELECT value FROM meta WHERE
  key='schema_version'"` 应报告 `15`（§5.2）；
- 仓库自检入口：`uv run python scripts/check_release.py`（构建 wheel、
  临时环境安装、包内资源与 preset/skills 检查、stand-in Goal 全流程，
  以及真实 v0.3.0 样例库的 v8→v15 升级/拒读/备份恢复核对；输出 JSON
  报告，发现问题退出非零。运行手册见
  [docs/package-acceptance.md](package-acceptance.md)）；
- 若装了 analytics：§4 第 6 步的 probe/collect 核对。

## 7. 与 0.3.1 升级指南的关系

- [docs/upgrade-0.3.1.md](upgrade-0.3.1.md) 是历史文档：其 §3（角色
  定义显式更新）、§4（技能更新）描述的流程未变、仍按其执行；其 §6
  的备份/迁移/回退方法论已被本文 §5 扩展到 v15；其 §7 的
  `replan --context-file` 输入要求在 0.4.0 **未变**，该节仍现行。
- 0.3.1 的发布事实见 [docs/release-0.3.1.md](release-0.3.1.md)；
  0.4.0 的发布说明见 [docs/release-0.4.0.md](release-0.4.0.md)。
