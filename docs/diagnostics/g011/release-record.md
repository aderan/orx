# G011/T008 发布记录：ORX v0.4.0 ＋ Analytics v0.4.2 兼容组合发布收口

性质：**独立证据提交**（冻结受验提交之后追加的文档，零源码变更；受验
源码自冻结提交起未再变化）。本记录逐项对账 Goal 验收要求的发布事实：
受验源码提交、证据提交、标签、下载链接、资产哈希、各渠道结论。
联合验收权威记录：`joint-acceptance-report.md`（T007）、
`freeze-report.md`（T006）、`baseline.json`／`distribution.json`（T001/T005）、
`joint-acceptance-runbook.md`（T004）。发布时间：2026-10-17（UTC）。

## 1. 受验源码提交与标签（远端 `git ls-remote` 核实）

| 仓 | 受验冻结提交（candidate.json 绑定） | 标签（轻量，与 v0.3.x 一致） | 远端核实 |
|---|---|---|---|
| `aderan/orx` | `f1cc7757c953d3fee5bff5630613a6d2fb380a74`（v0.4.0，schema v15） | `v0.4.0` → 该提交 | 一致；历史标签 v0.1.0–v0.3.1 原样（v0.3.0 仍指 `0f2629e6…`、v0.3.1 仍指 `4a450cf8…`），无同名标签冲突，未覆盖未强推 |
| `aderan/orx-analytics` | `2461a260387caf0e6acb63446d4be3b8d1a43b46`（v0.4.2，store v3） | `v0.4.2` → 该提交 | 一致；该仓历史无标签保持无标签 |

推送前核对（T008 实测）：两仓远端 main（`9638d26`／`bd06d70`）均为
本地严格祖先、零分叉；ORX 远端无 `v0.4.*` 标签、Analytics 远端无任何
标签——不存在同名标签不一致的阻塞。推送：ORX main `9638d26..56af38c`
＋新标签 `v0.4.0`；Analytics main `bd06d70..2461a26` ＋新标签
`v0.4.2`，随后 `2461a26..f8c81b5`（证据提交）。全程无强推。

## 2. 证据提交（冻结后追加；均不含源码变更、均不移动受验标签）

| 仓 | 提交 | 内容 |
|---|---|---|
| orx | `5c4247b`（T006 后） | freeze-report.md ＋ 计划阶段 5 状态 |
| orx | `56af38c`（T007 后） | joint-acceptance-report.md ＋ screenshots/README.boundary.md ＋ 计划 T007 轮次记录 |
| orx | 本提交（T008 收口） | 本发布记录 ＋ IMPLEMENTATION_PLAN.md 移除 G011 五阶段（Goal 验收要求）＋ T008 轮次记录 |
| orx-analytics | `f8c81b5`（发布后） | `reports/release-v0.4.2-record.md` 发布对账记录（来源标明 ORX 仓权威记录） |

受保护用户报告 `docs/m1.2-report.md` 全程未暂存未提交（排除决定见
`baseline.json` protected_report 并随 Release 说明披露）；发布物一律取
其提交态。公开证据不含运行数据库、私钥或真实会话原始数据
（`dist/g011/` 整体不入库，仅发布产物＋哈希＋说明随 Release 公开）。

## 3. 发布资产、下载链接与哈希（发布阶段未重建、未重签、未修改任何字节）

Release：[aderan/orx `v0.4.0`](https://github.com/aderan/orx/releases/tag/v0.4.0)
（2026-10-17T18:02:51Z，正式）／
[aderan/orx-analytics `v0.4.2`](https://github.com/aderan/orx-analytics/releases/tag/v0.4.2)
（2026-10-17T18:03:29Z，**标记 prerelease**——App 归档为内部预览渠道；wheel/sdist 为 0.4.2 分发物）。

| 资产 | 下载链接 | SHA256（＝candidate.json 冻结值） |
|---|---|---|
| ORX wheel | https://github.com/aderan/orx/releases/download/v0.4.0/orx_agent-0.4.0-py3-none-any.whl | `f71c19aebd2dcd1a09524602be50ad730b194d743f52925ea59fd75c2d2a212f` |
| App 归档（内部预览） | https://github.com/aderan/orx-analytics/releases/download/v0.4.2/ORXAnalytics-0.4.2-internal-preview-dev-macos13-arm64.zip | `5187029c8ab41a41a8d6100c502061035a322c7d24ad015cfbe2fa76303d7396` |
| Analytics wheel | https://github.com/aderan/orx-analytics/releases/download/v0.4.2/orx_analytics-0.4.2-py3-none-any.whl | `284d07741306347630f70f52b9be4a5322efbe2c84e41f05ccffff4cc956584d` |
| Analytics sdist | https://github.com/aderan/orx-analytics/releases/download/v0.4.2/orx_analytics-0.4.2.tar.gz | `751a1011ca6e00c351c662c43d6af9a8c49481419b2b946dbaacecf3df4ef924` |
| SHA256 清单 | 两 Release 同字节：…/releases/download/v0.4.0/SHA256SUMS.txt ＝ …/releases/download/v0.4.2/SHA256SUMS.txt | 上述四项逐行清单 |
| 冻结绑定清单 | …/releases/download/v0.4.0/manifest.json ＝ …/v0.4.2/manifest.json | manifest.json（binding/artifacts/stages） |
| 兼容组合说明 | …/releases/download/v0.4.0/compatibility-orx-0.4.0-analytics-0.4.2.md ＝ …/v0.4.2/…（同文件名） | 兼容组合/渠道/已知缺陷/复核移交/排除披露 |

Release 说明正文（两处）均包含：兼容组合表（ORX 0.4.0／schema v15／
Analytics 0.4.2／store v3）、内部预览渠道标注（ad-hoc、受众、
Gatekeeper 限制）、已知缺陷披露（首次引擎启动 73 个 `__pycache__`
使 `codesign --verify` 0→1；修复＝`EngineProcessManager.makeChild`
设 `PYTHONDONTWRITEBYTECODE=1`，落下一 Analytics 发布；归档字节不受
影响）、解锁后约 10 秒人工复核移交（§9.1 三项）、正式渠道凭据受阻
声明。

## 4. 消费者侧核对（verify-remote，规定检查绿）

发布后从两 Release **下载全部资产**重算哈希：四个产物与
candidate.json/manifest 冻结值逐字节一致；`SHA256SUMS.txt`、
`manifest.json`、兼容组合说明在两 Release 字节相同。
`uv run python scripts/joint_release.py verify-remote --work-dir dist/g011`
绿：internal-preview 渠道根（`dist/g011/channels/internal-preview/`，
由已发布 Release 资产填充）绑定一致、产物逐字节一致、无 DMG 混入
（渠道隔离）。manifest 七阶段（freeze/battery/build/candidate/
exercise/verify/verify-remote）全 passed。

## 5. 渠道结论

- **内部预览（internal-preview）**：已发布。App 归档 ad-hoc 签名、
  未公证，受众仅内部测试，Gatekeeper 拦截首次启动需手动确认（安装
  文档在案）；Release 标记 prerelease，渠道身份在标题/正文/Info.plist/
  包内 distribution.json 四维一致。
- **正式分发（official）**：保持受阻，未发布。无 Developer ID
  Application 证书、无 notarytool 存储凭据（2026-10-17 实测，
  `distribution.json`）；真实签名/公证/装订/安装评估证据齐全前不产出
  DMG、不使用正式分发表述。解除路径见 `distribution.json`
  `user_steps_to_unblock`。
- Analytics Python 分发物（wheel/sdist）：随 v0.4.2 Release 正常分发
  （构建自冻结提交、check_install 全过）。

## 6. 遗留移交（如实）

- 运行时封条缺陷（已知，见 §3 披露）修复落下一个 Analytics 发布。
- 解锁后约 10 秒像素级人工复核（验收报告 §9.1）随 Release 说明移交，
  结果无论通过与否需如实记入后续发布说明。
- 本机 `ORX_ANALYTICS_BIN` 未设置、外部看护不可用——不影响本循环。
