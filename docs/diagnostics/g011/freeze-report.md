# G011 联合发布冻结报告（T006）

状态：T006 交付物。本报告记录 0.4.x 收口的**冻结事实**：两仓发布冻结
提交、干净检出全量与包级检查、绑定清单的最终分发物（ORX wheel、
Analytics sdist/wheel、内部预览 App 归档）及其在渠道最终门禁后冻结的
SHA256。本任务**不创建任何已发布结论**：不推送、不打标签、不发布渠道
根（T007 记录操作者 GUI 验收、T008 发布并核对远端对应关系）。

机器可读的冻结清单是 `dist/g011/candidate.json`（schema 1，phase
`candidate`，`published: false`）；脱敏证据索引
`dist/g011/EVIDENCE-INDEX.md`；逐阶段日志 `dist/g011/logs/`。本报告引用
它们，不复述全部字段。

## 1. 冻结提交（两仓完整 sha）

| 仓库 | 版本 | 冻结提交（完整 sha） | 说明 |
| --- | --- | --- | --- |
| ORX（本仓库） | v0.4.0 | `f1cc7757c953d3fee5bff5630613a6d2fb380a74` | 18 文件（+5644/−46）：版本 0.4.0、兼容/升级/回退/发布文档、联合验收驱动与测试、g011 诊断文件、两仓基线与分发决定 |
| orx-analytics（`.release-workspaces/orx-analytics` 隔离克隆） | v0.4.2 | `2461a260387caf0e6acb63446d4be3b8d1a43b46` | 30 文件（+2715/−108）：生命周期分层展示、Swift 徽标与通知钉死、渠道身份与发布门禁、构建封条修复、安装文档 |

- 两仓**不同号**是既定兼容组合（ORX v0.4.0 ＋ Analytics v0.4.2，
  baseline `compatible_release_combo`）。
- 版本一律**从冻结提交读出**（`git show <commit>:src/.../__init__.py`），
  绝不读工作区；freeze/verify 每次复验。
- **历史标签保持原样**：ORX `v0.1.0, v0.2.0, v0.2.1, v0.3.0, v0.3.1`
  快照与基线一致，`v0.3.0` 仍指向 `0f2629e6…`；Analytics 仓本无标签，
  保持无标签。本任务未创建任何标签（T008 从冻结提交打标）。
- 原始 Analytics checkout（`/Users/flb/Sources/Tools/orx-analytics`）
  全程只读，未执行任何变更性 git 操作；发布工作全部在隔离克隆内。

## 2. 受保护用户报告的处置（发布排除，实测复核）

`docs/m1.2-report.md`（用户文件，G010 处置态）**不进入发布**：

- 冻结时 ORX 工作区脏文件**恰且仅**为 ` M docs/m1.2-report.md`
  （candidate.json `binding.orx.worktree` 如实记录，逐字节保留 porcelain
  前导空格）；Analytics 克隆冻结时工作区为空（entries `[]`）。
  candidate 阶段的未提交输入门（`uncommitted-inputs-at-freeze`）逐一
  核对：除基线保护的用户报告外，任何未提交内容存在即拒绝产出候选。
- 一切构建均取**冻结提交**（`git archive`/`git clone --detach`），工作区
  改动在语义上进不了任何产物；发布物中该文件取提交态。
- 该文件自始至终未暂存、未提交、未改写。交付时实测：
  sha256 `5289e6e713483ae1930513a4443967b90906d5a3433f7493770e5a52b89d0fa5`
  （与 baseline `protected_report.sha256` 逐字节一致）、
  `git status --porcelain` 为 ` M docs/m1.2-report.md`（未暂存）。
- 冻结提交链路上的报告类改动是否入发布的结论：**入**＝两仓兼容/安装/
  发布文档与诊断文件（已随冻结提交提交）；**不入**＝用户报告的工作区
  改动与一切运行材料（`dist/`、`.release-workspaces/` 均不进版本库）。

## 3. 干净检出全量与包级检查（一次性检出，全部真实通过）

`battery` 阶段对两仓冻结提交各做一次性**干净 git 检出**
（`git clone --no-hardlinks` ＋ `git checkout --detach <冻结提交>`——真
检出带历史与标签，包级链的 `git archive v0.3.0` 才可用；未提交内容进不
了 clone）。Analytics 检出置于 ORX 检出内
`.release-workspaces/orx-analytics`，还原发布工作区布局，使 ORX 全量
（含联合验收测试）可整体运行。五项必需检查全部 exit 0
（`dist/g011/logs/02-battery.log`，candidate 复验逐项核对日志存在）：

| 检查 | 命令（检出内） | 结果 |
| --- | --- | --- |
| `orx_pytest` | `uv run pytest -q` | exit 0，**1162 passed**（0 失败） |
| `orx_check_release` | `uv run python scripts/check_release.py` | exit 0，`problems: []`（v8→v15 升级链、旧版拒读、fixture 再生全过） |
| `analytics_pytest` | `uv run --extra dev python -m pytest -q` | exit 0（256 项，100% 无失败标记；同树在克隆工作树实测 256 passed） |
| `analytics_swift_test` | `swift test`（apps/macos） | exit 0，**140 项，0 失败**（5 skipped＝未设 `ORX_WIRE_FIXTURES` 的 wire 契约项，属既有跳过语义） |
| `analytics_check_install` | `uv run --extra dev python scripts/check_install.py` | exit 0，sdist→wheel→干净离线安装→双 CLI 入口/注册表/隔离采集/数据目录优先级全过 |

环境预置披露（环境准备，绝不改冻结源码；均记录在 battery 阶段与日志）：
干净 Analytics 检出内按 T001 程序建 `.venv`（钉 harness 解释器
3.12.9、可编辑安装）；干净 ORX 检出内以 sqlite backup API 生成
`.orx/state.db` 自包含快照（m12 验收实测的 dogfood 库为 gitignore 的
运行态数据，源库只读不动）；App 构建的固定运行时 tar 从发布克隆
var/runtime-cache 预种子（build_app.sh 每次使用仍验 sha256）。

## 4. 冻结分发物（绑定清单；SHA256 在渠道最终门禁后冻结）

全部产物构建自冻结提交（`built_from` ＝完整 sha，candidate 复验逐一
核对提交身份、文件名版本、渠道风味）：

| 产物 | 路径（`dist/g011/` 内） | SHA256 |
| --- | --- | --- |
| ORX wheel | `dist/orx_agent-0.4.0-py3-none-any.whl` | `f71c19aebd2dcd1a09524602be50ad730b194d743f52925ea59fd75c2d2a212f` |
| Analytics wheel | `dist/analytics/orx_analytics-0.4.2-py3-none-any.whl` | `284d07741306347630f70f52b9be4a5322efbe2c84e41f05ccffff4cc956584d` |
| Analytics sdist | `dist/analytics/orx_analytics-0.4.2.tar.gz` | `751a1011ca6e00c351c662c43d6af9a8c49481419b2b946dbaacecf3df4ef924` |
| App 归档（内部预览 zip） | `dist/ORXAnalytics-0.4.2-internal-preview-dev-macos13-arm64.zip` | `5187029c8ab41a41a8d6100c502061035a322c7d24ad015cfbe2fa76303d7396` |
| App 逐归档构建记录 | `dist/ORXAnalytics-…-arm64.zip.json` | `60b38c0cb6d291960995be56d0f9df628d22589d60f2a0446e2d7219cdfdce97` |

一致性事实（candidate 生成时实测、`verify --phase candidate` 每次复验）：

- **包元数据**：`orx-agent 0.4.0`、`orx-analytics 0.4.2`（wheel METADATA
  与 sdist PKG-INFO 一致）；冻结 wheel 装入一次性 venv 的依赖闭包
  `typer 0.27.3 / pydantic 2.14.1`（manifest `runtime.orx_dependencies`）。
- **App 版本/来源提交**：Info.plist `CFBundleShortVersionString=0.4.2`、
  `ORXAnalyticsSourceCommit=2461a260…`（完整 sha）、
  `CFBundleVersion=0.4.2+dev+2461a26`、包内 `Resources/distribution.json`
  与 `orxanalytics-version.txt` 同值。
- **内置运行时**：`ORXAnalyticsBundledPython=
  cpython-3.12.8+20250115-aarch64-apple-darwin-install_only`（与冻结提交
  build_app.sh 的 pin 一致）；捆绑解释器实测运行报告 `3.12.8`。
- **签名渠道一致**：内部预览＝ad-hoc（`codesign --verify --strict`
  exit 0，authorities 为空、如实非 Developer ID、非公证——渠道的诚实属
  性）；构建记录 gates 如实 `not-performed` 公证/装订/spctl，无冒充。
- **决定门**：构建经 `--decision docs/diagnostics/g011/distribution.json`
  （sha256 `ca245052…fc324d` 随候选冻结；官方渠道 decision `blocked`、
  fail_closed，凭据缺失，未降级、未伪造）。

**失效规则**：候选归档 SHA256 已在渠道最终门禁（内部预览＝可验证的
ad-hoc 签名与封条守卫）后冻结；此后任何源码、签名或字节变化使候选失
效，必须重新构建并重新联合验收（candidate.json `invalidation`）。

## 5. 冻结验收中发现并修复的构建封条缺陷（如实记录）

首轮构建的最终归档 `codesign --verify --strict` 失败（sealed resource
invalid）。根因是 Analytics 侧 `build_app.sh` 的顺序缺陷：引擎自测在
ad-hoc 签名**之后**运行，捆绑解释器向 `.app` 内写入 `__pycache__/*.pyc`
（stdlib 与 orxanalytics 包），破坏密封；且 ditto 归档的 AppleDouble 条
目用 `unzip` 解压会落成 `._*` 杂散文件二次破坏封条。修复全部进入**冻结
提交**（`2461a260`）后才产出候选：自测移至签名前且禁写字节码
（`PYTHONDONTWRITEBYTECODE=1`＋`-B`）；签名前记录封条基线（固定运行时
自带的少量 stdlib 预编译缓存属密封内容）、归档前比对，签名后包内字节
码/缓存集合任何增删即 `bundle-mutated-after-signing` 拒绝；安装文档明
确预览 zip 用 Finder/ditto 解压。ORX 侧配套：驱动统一 `ditto -x -k` 解
压、一切 harness 解释器调用禁写字节码、freeze 换绑定时重置下游阶段记
录（旧绑定证据不可沿用）。修复后的最终归档严格验签通过（第 4 节）。

## 6. 边界与后续

- **本报告不构成发布**：无推送、无标签、无渠道根、`published: false`。
  T007 以冻结归档完成操作者 GUI/生命周期联合验收（`exercise`＋
  `verify`，含 GUI/签名证据），T008 推送冻结提交与准确标签
  （ORX v0.4.0、Analytics v0.4.2）、上传同字节产物与校验清单并核对远端。
- 复现：`uv run python scripts/joint_release.py freeze|battery|build|candidate --work-dir dist/g011`；
  候选只读复验（T006 规定检查）：
  `uv run python scripts/joint_release.py verify --manifest dist/g011/candidate.json --phase candidate`。
- 本机事实：ORX_ANALYTICS_BIN 未设置（外部看护不可用，不影响本循环）；
  正式渠道两项凭据缺失（distribution.json `credential_facts_probed_live`）。
