# G012 联合验收运行手册（T001）

状态：G012（Analytics 0.4.3 单独发布）的权威运行手册。实现全部在
`scripts/joint_release.py`（G011 驱动的最小目标化扩展），行为固化在
`tests/test_joint_release.py`（G011 组保持原样，新增 G012 组）。G011 的
联合层手册见 `docs/diagnostics/g011/joint-acceptance-runbook.md`——本手册
只写 G012 的差异与顺序，共同机制（一次性检出、样本溯源、失败关闭、
渠道隔离、封条纪律）不重复，全部沿用。

## 1. 范围：Analytics 单独发布，ORX 固定不重发

- **G012 发布物**：orx-analytics 0.4.3（wheel＋sdist＋App 内部预览 zip），
  来自 Analytics 冻结提交（阶段 4 记录，当前基线 f8c81b5，见
  `docs/diagnostics/g012/baseline.json`）。
- **ORX 产品输入固定**：已发布 `v0.4.0` 标签 → `f1cc7757…`（发布物不动）。
  ORX 不从 HEAD 重建发布、不打新标签、不重传 Release——**不要求重复发布
  ORX**。驱动从 pin 解析 ORX 提交构建依赖链 wheel（演练/依赖闭包用，
  属溯源重建，不构成任何 ORX 发布动作；已发布 v0.4.0 Release 保持权威）。
- **选择方式**：`--goal-id g012`（显式；默认仍 g011，G011 复现命令不变）。
  g012 默认工作目录 `dist/g012`、默认决定文件
  `docs/diagnostics/g012/distribution.json`；既有工作目录上重跑 `freeze`
  不带 `--goal-id` 时从 manifest 沿用目标标识（goal 连续性）。
- **门禁不放宽**：五项 battery 检查（含 ORX 全量 pytest＋check_release，
  从 **pin 的 v0.4.0 干净克隆**跑）、样本溯源（旧样本再生比对）、全部
  失败关闭码、渠道隔离、签名证据、GUI 证据、受保护报告排除——与 G011
  逐字共享。新增失败码仅一个：`orx-pin-moved`（v0.4.0 标签移动即停，
  绝不重定位 pin）。唯一目标化放宽：Analytics 克隆内未跟踪的**生成目录**
  `apps/macos/.build/`（SwiftPM 缓存）在 g012 下豁免未提交输入门禁——
  构建一律物化提交树（git archive/clone），该目录永远进不了构建；其余
  任何未提交内容（含同目录下的 tracked 修改、任何其他未跟踪路径）照旧
  拒绝，g011 门禁分毫未动（测试逐分支钉死）。

## 2. 阶段与命令（g012 全集）

| 阶段 | 命令 | 说明 |
| --- | --- | --- |
| 冻结 | `uv run python scripts/joint_release.py freeze --goal-id g012 --work-dir dist/g012 [--channel internal-preview]` | ORX 产品输入解析自发布标签 v0.4.0（移动即 `orx-pin-moved` 失败关闭），Analytics 取克隆 HEAD；版本从提交读出；样本溯源校验；binding 记录 `goal_id/goal_label`、`orx.pinned_tag`、`acceptance_driver.commit`（G012 验收驱动提交，仅溯源非输入）与 Analytics 冻结提交 |
| 电池 | `uv run python scripts/joint_release.py battery --work-dir dist/g012` | 五项必需检查从冻结提交一次性干净克隆跑：ORX 全量 pytest＋check_release（克隆在 **v0.4.0 pin**，非 HEAD）＋ Analytics 全量 pytest＋swift test＋check_install；任一失败即阶段失败 |
| 构建 | `uv run python scripts/joint_release.py build --build-app --work-dir dist/g012` | ORX wheel 自 pin 的一次性检出构建；Analytics sdist/wheel 与最终 App（内部预览 dev zip，ad-hoc）自 Analytics 冻结提交的 build_app.sh 构建，尊重 g012 决定门；缺 App 归档即 fail-closed |
| 候选 | `uv run python scripts/joint_release.py candidate --work-dir dist/g012` | 产出 `dist/g012/candidate.json`：**目标标记 G012**（`goal: "G012"`、`goal_id: "g012"`，不携带任何 G011 任务号或表述；发布说明文字亦按目标生成）；决定按 sha256 绑定 g012 决定文件；历史标签快照（含 v0.4.0 pin 与 v0.4.2）与 battery 证据随档冻结；`published: false` |
| 候选只读复验 | `uv run python scripts/joint_release.py verify --manifest dist/g012/candidate.json --phase candidate` | 冻结候选只读复验（摘要/身份/电池证据/包与 App 元数据/签名/决定 sha/历史标签/样本溯源/pin 复核 + 目标标记与 binding 一致性）；不判 GUI 与渠道根（后续步骤的事） |
| 演练 | `uv run python scripts/joint_release.py exercise --work-dir dist/g012` | 冻结 ORX wheel（自 pin 构建）装入一次性 venv 驱动隔离项目全生命周期＋最终 App 内置引擎 socket 连接两次采集与只读断言 |
| 验证 | `uv run python scripts/joint_release.py verify --work-dir dist/g012` | 全链验收：重算全部产物哈希、核对身份、要求全部阶段证据与日志、App/GUI/签名证据；缺一即败，绝无 skip 后通过 |
| 远端核对 | `uv run python scripts/joint_release.py verify-remote --work-dir dist/g012 --channel-root dist/g012/channels/internal-preview` | 消费者侧：核对已发布渠道根产物逐字节一致、绑定一致、渠道隔离（内部预览不带 DMG）；渠道根不存在即 fail-closed |

（`self-test`／`generate-samples` 与 G011 相同，默认 g011；g012 复现自检
可加 `--goal-id g012 --work-dir dist/g012/harness-selftest`。）

## 3. 顺序（收口顺序，缺一不可、不可倒置）

1. **冻结**：`freeze --goal-id g012 --work-dir dist/g012`（阶段 2/3 的
   修复提交全部落入 Analytics 克隆后执行；ORX 侧驱动/手册/基线提交亦已
   落库——binding 的 `acceptance_driver.commit` 即该提交）。
2. **电池 → 构建 → 候选**：`battery` → `build --build-app` → `candidate`
   （顺序前置检查强制）；随后 `verify --manifest dist/g012/candidate.json
   --phase candidate` 只读复验冻结候选。
3. **演练**：`exercise`。
4. **GUI 证据**（操作者，先于 verify）：写 `dist/g012/gui-evidence.json`
   （字段 operator/artifacts/observed_at，截图等放
   `dist/g012/gui-acceptance/`）。G012 必须覆盖用户实测反馈四项的可见
   结果——token 千位分隔、点击项目卡片 run-detail 窗口前置聚焦、目标
   已完成（终态 done）降为普通文字（非终态徽标保留）、数据刷新节律
   默认值下调（含设置项如有）——以及 controller token 归属显示（宿主
   会话用量按 run 会话归属只读关联后可见）。
5. **真实首启封条检查**（v0.4.2 已披露缺陷的落地验收，先于 verify）：
   从冻结归档安装（ditto 解压到隔离目录），`codesign --verify --strict`
   首启前 exit 0；执行**真实首次引擎启动**（安装后的 App 拉起内置引擎，
   即 `EngineProcessManager.makeChild` 路径），随后断言包内**零新增**
   `__pycache__`（`find <App>.app/Contents/Resources -name __pycache__`
   与首启前基线一致、无新 `.pyc`）且 `codesign --verify --strict` 仍
   exit 0；结果（含前后封条基线）记入 GUI/验收证据。失败即整个验收
   失败——该修复是 G012 的发布理由之一，不允许带缺陷过验。
6. **验证**：`verify --work-dir dist/g012`（上两步证据齐后才可能绿）。
7. **同字节发布**（阶段 5，仅 Analytics）：从冻结候选字节发布——
   Analytics 冻结提交打轻量标签 `v0.4.3` 并推送（无强推；v0.4.2 及全部
   历史标签原样）；GitHub Release（prerelease）上传冻结候选原始字节：
   App zip＋analytics wheel＋sdist＋SHA256SUMS＋manifest.json（＋兼容
   组合说明，沿用 ORX v0.4.0＋Analytics 0.4.3 表述）。**发布阶段不重建、
   不重签、不修改任何字节；ORX 的 v0.4.0 Release/标签零动作**。
8. **发布后下载核对**：从 Release 下载全部资产重算哈希与
   candidate.json/manifest 逐项比对；用已发布资产填充
   `dist/g012/channels/internal-preview/`；跑规定检查 `verify --work-dir
   dist/g012` ＋ `verify-remote --work-dir dist/g012 --channel-root
   dist/g012/channels/internal-preview` 双绿收口；发布记录落
   `docs/diagnostics/g012/release-record.md`（对账口径沿用 G011）。

## 4. 与 G011 的差异一览（其余全同）

| 维度 | G011（默认，不变） | G012 |
| --- | --- | --- |
| 目标标识 | g011／G011 | `--goal-id g012`／G012（候选记录标记 G012） |
| ORX 产品输入 | 仓 HEAD（联合组合发布） | 发布标签 v0.4.0 固定（移动即失败关闭） |
| ORX 发布 | v0.4.0 标签＋Release | **零动作**（不重发） |
| 工作目录／决定文件 | dist/g011／g011 decision | dist/g012／g012 decision |
| 驱动提交记录 | 无此字段 | binding.acceptance_driver（仅溯源） |
| 未提交输入豁免 | 仅受保护用户报告 | ＋Analytics 克隆生成目录 `apps/macos/.build/`（仅 g012、仅未跟踪、仅该前缀） |
| 失败码 | 见 G011 手册 §6 | ＋`orx-pin-moved` |

## 5. 运行材料与保护

- 运行材料 `dist/g012/`（本机 gitignore 的 `dist/` 规则排除，不入库）；
  `EVIDENCE-INDEX.md` 为脱敏证据索引（标题按 binding 目标标记 G012）。
- `docs/diagnostics/g011/**` 与 `dist/g011/**` 为已发布 G011 证据，只读；
  G012 材料一律入 `docs/diagnostics/g012/**` 与 `dist/g012/**`。
- 受保护用户报告 `docs/m1.2-report.md` 保持未暂存未提交（处置见
  baseline.json protected_report；发布物取提交态）。
