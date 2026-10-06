# 升级到 ORX 0.3.1

状态：角色定义与交付协议章节（§3–§5）由 G007 T001 起草；分渠道更新
步骤（§2）、数据库升级与回退（§6）、`replan --context-file` 要求（§7）
由 G007 T003 补齐，并从 README 链接。发布说明与版本号收口由 T005 完成。

本文面向从 0.3.0 升级到 0.3.1 的用户。0.3.1 的核心变化（与本文件相关的
三个）：

1. **安装包补齐原生角色定义**：wheel 现在携带 `orx/agents/`（orx-worker、
   orx-verifier、orx-verifier-strong）。0.3.0 的干净安装执行
   `orx preset install zcode` 时会报告
   `definition not packaged with this ORX (install manually)`；0.3.1 起
   三个角色随包分发、preset 可直接安装（由 `scripts/check_release.py`
   与 `tests/test_package_acceptance.py` 在干净临时环境实测验收）。
2. **角色定义与交付协议对齐**：`agents/orx-worker.md` 的 TASK 段不再示范
   旧版 `{summary, commands, artifacts}` evidence（该形状会被
   `orx task complete` 按名拒绝），改为现行结构化交付结果，并补齐
   START GATE / BLOCKED EXIT / 同会话 CHECK-FIX LOOP / DELIVERY GATE、
   claim 与 attempt 身份、heartbeat 进展报告规则。
3. **controller 看护接入去本机路径化**：analytics 看护改为显式配置、可选
   执行（G007 T002），不再把本机 analytics 目录写成固定位置。

数据库 schema 从 v8 升到 v11（§6，自动迁移），技能与角色定义的刷新路径
见 §3、§4。

## 1. 升级前确认

- 记录当前版本：`orx version`（0.3.x 均可按本文升级）。
- 确认安装形态：本地 editable（`uv pip install -e`，本仓库的常规形态）
  或 wheel 安装。两者的更新入口不同（§2）。`orx update --check` 会报告
  安装来源与升级路径。
- **暂停写作者**：升级开始前，让所有会写这个项目 `.orx/state.db` 的进程
  收尾或显式失败重试——正在运行的 ORX 会话（host worker / verifier
  子代理、controller 循环）、`orx run`、任何挂着该项目的终端。确认没有
  进程再持有该数据库的写连接后再动手。原因见 §6.1：新版本第一次打开
  旧库就会迁移并替换数据库文件，迁移之后写入的任何数据都不在备份里，
  迁移期间的并发写入也可能被替换操作丢掉。

## 2. 更新 ORX 本体（分渠道）

两个渠道共用同一顺序，差异只在第 3 步：

1. 暂停写作者（§1）。
2. 备份（§6.1）：数据库一致性备份 + 项目配置 + 运行材料。**必须发生在
   新版首次打开旧库之前。**
3. 把 ORX 本体切到 0.3.1（分渠道，见下）。
4. 用任意 orx 命令（如 `orx status`）首次打开旧库，触发自动迁移
   v8 → v11（§6.2）。
5. 技能刷新：`orx skill update`（§4）。
6. 角色定义显式更新：备份、比较、替换（§3）。
7. 升级后验收（§5）。

### 2.1 本地 editable 用户（现状推荐）

```sh
git fetch && git checkout v0.3.1   # 或对应发布提交
uv sync                            # 仅当依赖锁有变化时需要
```

- editable 安装直接跟踪源码 checkout，切到新 tag 后本体即是 0.3.1，
  无需重装。
- **不做 `uv tool upgrade`**：`orx update` 对 editable 安装会直接拒绝
  （"orx is an editable uv tool install; upgrade by pulling the source
  checkout and reinstalling, not via `uv tool upgrade`"）。本渠道没有
  `uv tool upgrade` 这一步。
- 技能只走 `orx skill update`（§4）；角色定义按 §3 显式更新。两者是
  不同的入口，`orx skill update` 不会触碰角色定义文件。

### 2.2 wheel 用户（uv tool / pip 安装）

- uv tool 安装：`orx update`（内部执行 `uv tool upgrade orx-agent`），
  或手动 `uv tool upgrade orx-agent`。先 `orx update --check` 可确认
  安装来源与将执行的命令。
- 其他 wheel 安装方式（pip / venv）：按你原来的安装命令安装 0.3.1 的
  wheel 覆盖旧版。
- 安装完成后的首次运行即触发 §6.2 的自动迁移——所以 §6.1 的备份必须
  在安装/首次运行之前完成。
- 技能同样只走 `orx skill update`；角色定义按 §3 显式更新。

## 3. 旧角色文件的备份、比较与显式替换（必读）

0.3.1 改动了三个原生角色定义，但 **preset 永远不会覆盖已存在的角色
文件**：`orx preset install zcode` 只安装缺失的定义，已有文件总是保留
（报告中标记 `preserved existing`），漂移由 `orx doctor` 的
`agent_def:*` 检查点名，绝不静默替换。因此升级到新版角色定义需要你
**显式选择**，步骤如下。

### 3.1 备份现有定义

```sh
cd ~/.zcode/agents
for f in orx-worker orx-verifier orx-verifier-strong; do
  [ -f "$f.md" ] && cp "$f.md" "$f.md.bak-0.3.0"
done
```

（若 `$ORX_ZCODE_AGENTS_DIR` 指向其他目录，以该目录为准。）

### 3.2 与新版比较

新版定义随包分发，先定位安装包内的副本：

```sh
NEW=$(python -c "from orx.presets import source_agents_dir; print(source_agents_dir())")
for f in orx-worker orx-verifier orx-verifier-strong; do
  echo "== $f =="; diff -u ~/.zcode/agents/$f.md "$NEW/$f.md"
done
```

（editable 用户把 `python` 换成 `uv run python`；也可以直接比较仓库文件
`agents/<name>.md`。）重点看：

- `orx-worker.md` TASK 段：新版示范的 evidence 格式与交付合同
  （未改动的自定义段落可保留）；
- frontmatter（`model:` / `thoughtLevel:`）：与 profile 请求不一致会被
  `orx doctor` 判 FAIL，合并时不要引入漂移。

### 3.3 显式替换（或手工合并后放回）

- **接受新版**：移走旧文件后重装 preset（preset 只装缺失项）：

  ```sh
  mv ~/.zcode/agents/orx-worker.md ~/.zcode/agents/orx-worker.md.bak-0.3.0
  orx preset install zcode     # 报告应显示 agent orx-worker: installed
  ```

  对 `orx-verifier` / `orx-verifier-strong` 同理。也可以直接把
  `$NEW/<name>.md` 复制过去（等价于显式替换）。

- **保留自定义**：以新版为底，把你的自定义内容合并进去后写回原路径。
  只要文件存在，preset 就不会动它——更新永远是你显式做的决定。

### 3.4 新版 evidence 示例（worker 角色现在教的格式）

```json
{
  "status": "passed",
  "summary": "what was delivered, or why it was not",
  "checks": [
    {
      "command": "uv run pytest -q",
      "exit_code": 0,
      "log": ".orx/runs/<run>/check/<task>/01-0000-command.log"
    }
  ],
  "artifacts": ["path/to/produced/file"]
}
```

`status` 取 `passed | failed | blocked`；`checks` 每项是
`{command, exit_code, log}`（无法运行时 `exit_code`/`log` 为 `null`）。
旧版 `{ "summary", "commands", "artifacts" }` 形状会被
`orx task complete` 按名拒绝并逐字段报缺。

## 4. 技能更新

技能只经 `orx skill update` 刷新（canonical 副本 + 各 symlink）；
该命令只刷新已安装且仍随包分发的技能，不会安装新技能，也不会触碰
角色定义文件。角色定义的更新是 §3 的独立流程——两个入口分开，谁也不
代劳谁。

## 5. 升级后验收

- `orx doctor`：`agent_def:*` 三项应为 ok（或如实在 warn 提示缺失），
  skill 两项按需安装；
- `orx preset install zcode`：三个 agent 均为 installed / preserved
  existing，不再出现 `MISSING (install manually)`；
- 数据库迁移确认：`sqlite3 .orx/state.db "SELECT value FROM meta WHERE
  key='schema_version'"` 应报告 `11`（§6.2）；
- 仓库自检入口：`uv run python scripts/check_release.py`（构建 wheel、
  临时环境安装、包内资源与 preset/skills 检查、stand-in Goal 全流程，
  以及真实 v0.3.0 样例库的 v8→v11 升级/拒读/备份恢复核对；输出 JSON
  报告，发现问题退出非零。运行手册见 docs/package-acceptance.md）。

## 6. 数据库 schema 升级与回退

### 6.1 升级前备份（在新版首次打开旧库之前）

**时点**：新版本第一次打开旧库就会自动迁移（§6.2），迁移把库文件改写
为 v11；而回退的唯一安全路径是恢复升级前备份（§6.4）。所以备份必须
发生在"新版第一次打开旧库"之前——对 editable 用户是切换 checkout 之后
的第一条 orx 命令之前，对 wheel 用户是安装新版后的首次运行之前。暂停
写作者（§1）之后、切换版本之前做备份最稳妥。

**数据库：用 SQLite 一致性备份，不要只 `cp` 主文件**。ORX 数据库以 WAL
模式运行，已提交的数据可能仍留在 `state.db-wal` 里，直接复制主文件会
漏掉这些数据。`VACUUM INTO` 与 `.backup` 都经由 SQLite 的事务一致快照
产出**单文件**备份（无 `-wal`/`-shm` 伴生文件），WAL 中已提交的帧全部
包含在内：

```sh
mkdir -p backup-0.3.0
sqlite3 .orx/state.db "VACUUM INTO 'backup-0.3.0/state.db'"
# 或等价：sqlite3 .orx/state.db ".backup 'backup-0.3.0/state.db'"
```

（`VACUUM INTO` 要求目标文件不存在；每次备份换一个新名字或先清空目录。）

**同时保存项目配置与必要运行材料**——它们不在 `state.db` 里，回退时
同样要恢复：

```sh
cp .orx/config.toml .orx/profiles.toml backup-0.3.0/ 2>/dev/null || true
cp -R .orx/runs backup-0.3.0/runs
```

`.orx/runs/` 下是每个 run 的 assignment prompts、exec / verification
日志、evidence 文件与 replan delivery snapshot 等运行材料，是独立
verifier 与时间线的复核依据。

### 6.2 自动迁移：0.3.0 schema v8 → v11

- 新版第一次打开旧库时**自动执行**，没有也不需要专用迁移命令；任何
  orx 命令（`orx status` 即可）都会触发。
- 迁移链路 v8 → v9（replan 对应关系六张表）→ v10（`attempt_progress`
  进展报告表）→ v11（`attempts.nonce` 身份锚列）。三步全部纯增量：
  不改既有表的行、不回填历史——升级自 v8 的库 replan 表为空、进展与
  nonce 为 NULL，读取为 unknown，与新建库的可观测行为一致。
- 迁移策略是**复制-替换**（`src/orx/state.py` 的 `_migrate_copy`）：
  先做 WAL checkpoint，把主文件连同 `-wal` 伴生文件复制到同目录临时
  副本，在副本上依次执行迁移，全部成功、WAL 折回后才原子替换原文件；
  任一步失败时临时副本被删除，原库保持原样、仍可被 0.3.0 打开。
- 迁移是升级动作里唯一改写数据库的步骤；它不改 `.orx/config.toml`、
  `.orx/profiles.toml` 与 `.orx/runs/`。

### 6.3 旧版本拒读新版库

0.3.0 代码打开 v11 库会立即报错退出——
`schema version 11 is newer than supported version 8; upgrade orx`——
且不会改动该文件。这是硬性拒绝，不是可绕过的提示：v11 库只能被支持
v11 的 ORX 读写。

### 6.4 回退：必须恢复升级前备份

- **唯一安全的回退路径**：本体回到 0.3.0（旧 tag / 旧 wheel），
  然后把 §6.1 的备份完整恢复——数据库、项目配置、运行材料一起。
- **不可用旧版本直接打开迁移后的库**（§6.3 会拒绝）。
- **never hand-edit `schema_version`**：绝不通过手改
  `meta.schema_version` 来"降级"。版本号只是声明，改数字不会删除
  v9/v10/v11 新增的表和列；旧代码会在错误的版本声明上继续读写，且旧
  版本再次打开时还会按 v8 重跑一遍 v9–v11 迁移。回退 = 恢复备份，
  没有捷径。
- 恢复前确认没有 orx 进程在写该项目；恢复时把迁移后的库连同可能残留
  的 WAL 伴生文件一起移除——恢复的主文件不能与迁移期残留的 `-wal`
  配对，否则 SQLite 可能重放出不属于该文件的帧（§6.1 的一致性备份
  本身是单文件快照，恢复后首次打开会按需重建 WAL）：

  ```sh
  # 本体已回退到 0.3.0 后：
  rm -f .orx/state.db .orx/state.db-wal .orx/state.db-shm
  cp backup-0.3.0/state.db .orx/state.db
  cp backup-0.3.0/config.toml backup-0.3.0/profiles.toml .orx/ 2>/dev/null || true
  rm -rf .orx/runs && cp -R backup-0.3.0/runs .orx/runs
  ```

- 回退后 0.3.0 打开的是它认识的 v8 库，一切照旧。备份时点到回退时点
  之间产生的写入随回退丢弃——回退窗口内的重要产出需另行导出保存。

## 7. `replan --context-file` 输入要求

中途重规划的命令是 `orx replan --context-file <file>`。要求分两层：
**代码允许什么**（CLI 参数层）与 **controller 流程要求什么**（执行
协议层），两层不要混。

### 7.1 文件本身的硬校验（代码层，`src/orx/dispatch.py`）

校验发生在任何分发之前——坏文件在 planner 被调用前就失败，不会留下
半成品 assignment：

- **可读**：路径相对当前目录解析；读不了（不存在 / 无权限）报
  `cannot read replan context file ...`。
- **非空**：全空白文件视同空文件，报
  `... is empty; provide this round's reason, intent, and supporting
  material (or omit --context-file)`。
- **上限 64 KiB**（`REPLAN_CONTEXT_MAX_BYTES = 65536`，按 UTF-8 字节
  数计），超出报 `... exceeds 65536 bytes; keep the intent file
  focused`。

### 7.2 内容要求

文件承载本轮的三件事：

- **原因（reason）**：为什么要重规划——哪个假设或依赖图错了；
- **意图（intent）**：这轮要改什么、什么不能动；
- **支撑材料（supporting material）**：路径、引用、摘录等可查证据。

```text
Reason for this replan: <为什么>
Intent for this round: <改什么、什么不能动>
Supporting material: <路径/引用/摘录>
```

文件文本作为 Controller 补充材料**逐字**进入 planner prompt；ORX 另外
附上 Goal 原文（verbatim，永不被 context file 改写）与确定性执行事实
快照（revisions、任务状态、失败原因、evidence 与 verification 引用）
——planner 同时看到三者。planner 的精确输入会被归档（prompt file）；
重复执行 `orx replan` 会用最新的 facts 与 intent 刷新仍在等待的
planning assignment，不会另开一个。

### 7.3 代码允许省略，controller 中途重规划不可省略

- **代码层**：`--context-file` 是可选 CLI 参数，省略它执行 replan 是
  合法输入（空文件的报错信息本身就写着 `or omit --context-file`）。
  不带 context file 的 replan 会得到只有 Goal 与事实快照的 prompt。
- **controller 流程层**：controller 技能（`skills/orx-controller`
  循环第 9 步）要求 Goal 进行中的 replan **先写 context 文件**再执行
  `orx replan --context-file <file>`——原因、意图、材料是流程对一次
  合格中途重规划的要求。省略参数是代码层的合法输入，不是流程层的
  合格执行。
