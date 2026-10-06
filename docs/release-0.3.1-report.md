# ORX 0.3.1 发布报告（G007 / R007 / T005）

状态：发布已完成（2026-10-06，UTC）。本报告只记录实际执行的命令与
真实结果；未执行或未验证的行为一律不写。日志路径均在本仓库
`.orx/runs/R007/check/T005/` 下（运行材料不入库，由 `.orx/state.db`
引用）。

## 1. 发布事实

| 项 | 值 |
| --- | --- |
| 发布提交 | `4a450cf8fc16a3ec79a8e1b1a3a1a5754b0cbeb3`（`4a450cf`，`G007 T005: v0.3.1 发布 — …`） |
| 提交内容 | T001–T004 全部工作树成果 + T005 版本号/发布材料，共 22 个文件（+3404/−144） |
| tag | `v0.3.1`，轻量 tag（与 v0.3.0 一致，`git cat-file -t v0.3.1` = `commit`），指向 `4a450cf` |
| 远端 | `git push origin main`：`0f2629e..4a450cf`；`git push origin v0.3.1`：`[new tag]`；推送前 origin 无 `v0.3.1`（不覆盖任何既有 tag） |
| ls-remote 核验 | `refs/heads/main` 与 `refs/tags/v0.3.1` 均解析为 `4a450cf…`（日志 `12-0011-command.log`） |
| 版本单一源 | `src/orx/__init__.py` `__version__ = "0.3.1"`（pyproject 经 hatch 动态读取） |

## 2. 构建产物与元数据核验

- `uv build` exit 0：`dist/orx_agent-0.3.1-py3-none-any.whl`（223,275 B）、
  `dist/orx_agent-0.3.1.tar.gz`（599,570 B）（日志 `08-0007-command.log`）。
- wheel `METADATA` 与 sdist `PKG-INFO` 的 `Version:` 均为 `0.3.1`。
- wheel 内含 `orx/agents/{orx-worker,orx-verifier,orx-verifier-strong}.md`
  与 `orx/skills/{orx-agent,orx-controller,orx-pbv}`；sdist 同。
- `uv lock --check` 通过（orx-agent 条目为 editable 源、无静态版本，
  版本升级不需要也不产生 lock 变更）。

## 3. 受验检查（对最终发布源码、提交前执行）

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| `uv run pytest -q` | exit 0，**702 passed** in 54.73s | `07-0006-command.log` |
| `uv build` | exit 0（§2 两个产物） | `08-0007-command.log` |
| `uv run python scripts/check_release.py` | exit 0，JSON 报告 `problems: []`（含干净安装、包内资源、preset、技能、stand-in Goal 全流程、真实 v0.3.0 样例库 v8→v11 升级链与再生成核对 `matches_regeneration: true`） | `09-0008-command.log` |
| `git diff --check` | exit 0 | `10-0009-command.log` |
| `uv run orx version --json` | `{"ok": true, "version": "0.3.1"}` | `11-0010-command.log` |
| `git ls-remote origin refs/heads/main refs/tags/v0.3.1 'refs/tags/v0.3.1^{}'` | exit 0，双 ref 均 `4a450cf…` | `12-0011-command.log`（push 后执行） |

发布说明为 `docs/release-0.3.1.md`：围绕四项成果（交付前门禁、重规划
差异检查与成果引用、配额预检与退避、host 进展报告与会话身份修复）、
三个必须缺口修复、schema v8→v11、三个明确延后事项（状态页对应关系
呈现、后台自动恢复、并行 worktree——未实现也未宣传）与升级入口
（docs/upgrade-0.3.1.md）。README 的 v8/v9 观测表述已在发布提交中
修正为 v8/v9/v10/v11（gate 接受 8、9、10、11）。

`docs/m1.2-report.md` 的测量截止时间戳刷新随发布提交带走：cutoff
`2026-10-06T07:26:51…` → `2026-10-06T16:09:59…`（最终全量 pytest 的
再生结果；该文件由测试进程再生，未手工编辑——这是既有披露行为，
见 docs/host-progress-report.md 副作用披露）。

## 4. 本地安装与刷新（host 授权的收尾流程）

安装形态（未做 `uv tool upgrade`，未直接修改 `.orx/state.db`）：

- `which orx` = `/Users/flb/.local/bin/orx`（uv tool 环境控制台脚本）；
- `orx update --check`：`source: "editable"`、`version: "0.3.1"`、
  `can_upgrade: false`（editable 拒绝 `uv tool upgrade`，符合约束）；
  安装解释器 `orx version --json` 报 `0.3.1`（editable 跟随发布提交）。

技能刷新（真实本地命令 `orx skill update`，exit 0）：

- refreshed：`orx-controller`、`orx-agent`、`orx-pbv`；
- symlinked into：`~/.zcode`、`~/.cursor`、`~/.codex`（9 条符号链接
  逐一核验，均指向 `~/.agents/skills/<name>`）；
- canonical 副本与发布提交 `skills/` 逐字节一致（cmp 三项全同）；
  刷新后的 orx-controller 含 `ORX_ANALYTICS_BIN` 显式配置（命中 2 处），
  不再含本机路径。

角色定义（按 docs/upgrade-0.3.1.md §3 备份、比较、显式替换）：

| 文件 | 比较结论 | 处理 |
| --- | --- | --- |
| `orx-verifier.md` | 本地 == v0.3.0 打包版（无用户定制） | 备份 `*.bak-0.3.0` 后替换 |
| `orx-verifier-strong.md` | 本地 == v0.3.0 打包版（无用户定制） | 备份 `*.bak-0.3.0` 后替换 |
| `orx-worker.md` | 相对 v0.3.0 有一处用户措辞扩展（blocked-exit 句"—— name what you tried, what failed, and where; the Controller records it…"）；该句已被 0.3.1 打包定义**逐字收录** | 备份 `*.bak-0.3.0` 后替换；定制内容以被收录方式保留 |

- 替换路径：先尝试 `orx preset install zcode`，其**原子拒绝**
  （`profile(s) ['zcode-worker'] differ between the user layer and
  preset 'zcode'; refusing to install`——用户层 zcode-worker profile 与
  preset 不一致，属机器真实定制，保留不动，未强制对齐）；改用升级文档
  §3.3 认可的等价路径：从 `source_agents_dir()`
  （`/Users/flb/Sources/Products/ORX/agents`，即发布提交打包源）直接
  复制三个定义。替换后三个文件与发布提交打包副本逐字节一致。
- 升级后验收 `orx doctor`：0 failures / 0 warnings；三条
  `agent_def:*` 均 ok（model/thoughtLevel 与 profile 匹配）；
  `state_db schema_version 11`；两个默认技能 installed。

## 5. Goal G007 六项验收对照（当前证据）

1. **干净安装 wheel 后三个子代理定义随包分发、preset 可安装**：
   wheel 实测含三个 `orx/agents/*.md`（§2）；
   `scripts/check_release.py` 干净 venv 链路 preset 段无
   "install manually" 且与打包副本逐字节一致，本轮 exit 0
   `problems: []`（§3）；`tests/test_package_acceptance.py` 固化
   （702 全量绿的组成部分）。
2. **角色定义与交付协议一致**：`agents/orx-worker.md` TASK 段示范
   `{status, summary, checks[{command,exit_code,log}], artifacts}` 并
   声明旧形状被拒；`tests/test_delivery_gate_e2e.py` 从定义提取示例
   JSON 经 `load_delivery_evidence` 零错误接受并过门禁；preset 保留
   已有文件的更新路径成文（docs/upgrade-0.3.1.md §3）。
3. **移除本机路径依赖**：controller 技能只认 `ORX_ANALYTICS_BIN`
   （无默认位置、未配置即跳过），本轮 `orx skill update` 后安装副本
   已核验；`docs/observability-contract.md` 声明不固定 checkout 位置。
4. **升级与回退说明成文**：`docs/upgrade-0.3.1.md` §1–§7（备份时点、
   分渠道更新、角色文件处理、schema v8→v11 迁移与拒读、恢复式回退、
   `replan --context-file` 要求）；README 两个入口链接已在发布提交中。
5. **安装包级验收固化**：`scripts/check_release.py`（链路各段可导入
   函数、JSON 报告、非零退出）+ `tests/test_package_acceptance.py`
   （无 skip）+ `tests/fixtures/release-0.3.0`（真实 v0.3.0 代码生成的
   schema v8 样例库，再生成核对一致）；运行手册
   `docs/package-acceptance.md`。
6. **版本号与发布材料就绪并完成发布**：版本 0.3.1（§2/§3）；发布说明
   围绕四项成果、三延后、升级入口；README v8/v9 已修；push 到
   aderan/orx 并打 tag v0.3.1（§1）；本地 editable 执行了
   `orx skill update`（§4）。

## 6. 副作用与如实披露

- 全量 pytest 会再生 `docs/m1.2-report.md`（截止时间戳刷新）；发布提交
  带走的是最终检查轮的再生结果。交付门禁复跑 pytest 时会再次再生，
  工作树出现新的未提交时间戳刷新——既有披露行为，非本任务手工编辑。
- 本报告在发布 push 之后提交（发布报告只记录实际完成结果，故不能
  先于 push 写入发布提交）；tag `v0.3.1` 仍指向受验发布提交
  `4a450cf`，远端 main 包含该提交与本报告提交。
- 用户层 zcode-worker profile 与 preset 的差异被 preset 安装的冲突
  拒绝如实保留（§4），未做对齐，留用户处置。
- `dist/` 与 `.orx/runs/` 均在 .gitignore 内，构建产物与运行日志不入库。
