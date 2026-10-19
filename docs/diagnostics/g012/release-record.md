# G012/T008 发布记录：Analytics v0.4.3 内部预览发布收口

性质：**独立证据提交**（冻结受验提交之后追加的文档，零源码变更；受验
源码自冻结提交起未再变化）。本记录逐项对账 Goal 验收要求的发布事实：
受验源码提交、证据提交、标签、下载链接、资产哈希、渠道结论。联合验收
权威记录：`joint-acceptance-report.md`（T007）、`baseline.json`／
`distribution.json`（T001）、`joint-acceptance-runbook.md`（T004）。
发布时间：2026-10-19 10:21 UTC。ORX v0.4.0 产品、标签与既有 Release
**零动作**（不重发、不重传、不打标）。

## 1. 受验源码提交与标签（远端 `git ls-remote` 核实）

| 仓 | 受验冻结提交（candidate.json 绑定） | 标签（轻量） | 远端核实 |
|---|---|---|---|
| `aderan/orx-analytics` | `d0867103a447e94ca1fd01731a60be62aeb2823a`（0.4.3，store v4） | `v0.4.3` → 该提交（本地创建后推送，对象为 commit——无 `^{}` 解引用，轻量确认） | 一致；历史标签 `v0.4.2` → `2461a260…` 原样，无同名标签冲突，未覆盖未强推 |
| `aderan/orx`（不动） | `f1cc7757c953d3fee5bff5630613a6d2fb380a74`（v0.4.0，G011 已发布） | `v0.4.0` 原样 | 一致；v0.1.0/v0.2.0/v0.2.1（annotated，tag 对象＋peeled commit 映射不变）、v0.3.0 → `0f2629e6…`、v0.3.1 → `4a450cf8…`、v0.4.0 → `f1cc7757…` 全部原样 |

推送前核对（T008 实测）：Analytics 远端 main `f8c81b5`、ORX 远端
main `bd6cb46` 均为本地严格祖先（`merge-base --is-ancestor` 双绿）、零
分叉；Analytics 远端标签仅 `v0.4.2`、ORX 远端无 `v0.4.3`——不存在
同名标签冲突。推送：Analytics main `f8c81b5..253dd93` ＋ 新标签
`v0.4.3`（普通 push，非强推）；ORX main `bd6cb46..ee8571a`（G012
验收驱动/文档提交：`7cec66c`／`043eecb`／`0eea576`／`ee8571a`——
driver/docs-only，无产品源码变更，使验收驱动公开）；随后两侧证据
提交（§2）。全程无强推、无任何历史标签移动。git-lfs 的 locks-verify
代理探测超时曾阻断首次 push，以 `-c lfs.locksverify=false` 重试成功
（本仓无 LFS 对象，仅跳过锁探测，不改变推送语义）。

## 2. 证据提交（冻结后追加；均不含源码变更、均不移动受验标签）

| 仓 | 提交 | 内容 |
|---|---|---|
| orx | `7cec66c`（T001/T005 驱动） | joint_release.py G012 扩展、store v4 升级链对齐、g012 基线/手册/决定/任务记录（binding.acceptance_driver 记录，仅溯源非输入） |
| orx | `0eea576`（T007 后） | 联合验收报告与阶段记录 |
| orx | `ee8571a`（T007 后） | T007 结构化交付结果存档 |
| orx | 本提交（T008 收口） | 本发布记录 ＋ IMPLEMENTATION_PLAN.md G012 五阶段移除（归档状态替换；历史保留于轮次记录）＋ T008 轮次记录 |
| orx-analytics | `d086710`（冻结提交，受验源码） | 0.4.3 本体（五项反馈修复＋seal 修复＋store v4）——**证据链上属受验源码，与证据提交清楚区分** |
| orx-analytics | `253dd93`（T007 后） | `reports/release-v0.4.3-acceptance.md` 冻结候选验收对账记录 |
| orx-analytics | 发布后证据提交（T008） | `reports/release-v0.4.3-record.md` 发布对账记录（来源标明 ORX 仓权威记录） |

受保护用户报告 `docs/m1.2-report.md` 全程未暂存未提交（排除决定见
`baseline.json` protected_report 并随 Release 说明披露）；发布物一律取
其提交态。公开证据不含运行数据库、私钥或真实会话原始数据
（`dist/g012/` 整体不入库，仅发布产物＋哈希＋说明随 Release 公开）。

## 3. 发布资产、下载链接与哈希（发布阶段未重建、未重签、未修改任何字节）

Release：[aderan/orx-analytics `v0.4.3`]
(https://github.com/aderan/orx-analytics/releases/tag/v0.4.3)
（published 2026-10-19T10:21:27Z，**标记 prerelease**——App 归档为
内部预览渠道；wheel/sdist 为 0.4.3 分发物）。ORX 侧唯一 Release
`v0.4.0`（正式，Latest）原样未动。

| 资产 | 下载链接 | SHA256（＝candidate.json 冻结值） |
|---|---|---|
| App 归档（内部预览） | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/ORXAnalytics-0.4.3-internal-preview-dev-macos13-arm64.zip | `461a2f037f9e94125d559ad57def4166383ba6f06a50d55c7014813909d63824` |
| Analytics wheel | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/orx_analytics-0.4.3-py3-none-any.whl | `7f7c6692a1559e5c736d732a3db780f5588500f505245b2b0dd2bf01aba73ef9` |
| Analytics sdist | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/orx_analytics-0.4.3.tar.gz | `b25327a8f8d456eaba912282d83043ee858d24822d1cc76b3be60161ed989a2b` |
| ORX wheel（不重传） | https://github.com/aderan/orx/releases/download/v0.4.0/orx_agent-0.4.0-py3-none-any.whl | `f71c19aebd2dcd1a09524602be50ad730b194d743f52925ea59fd75c2d2a212f`（与已发布 v0.4.0 资产一致） |
| SHA256 清单 | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/SHA256SUMS.txt | 上述四项逐行清单（下载复算逐行吻合） |
| 冻结绑定清单 | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/manifest.json | manifest.json（binding/artifacts/stages，发布时点副本） |
| 兼容组合说明 | https://github.com/aderan/orx-analytics/releases/download/v0.4.3/compatibility-orx-0.4.0-analytics-0.4.3.md | 兼容组合/渠道/seal 修复/节律/归属口径/移交/排除披露 |

Release 说明正文包含（controller 要求的七要素齐备）：(1) 五项反馈
修复摘要；(2) controller 数据口径与不可关联边界（逐 run 判定、共享
会话绝不摊分、仅 native_session_preview 口径、双口径隔离）；(3) 新
默认节律（面板/详情 30s、引擎 30/60/300s）与设置项指引（选项保留、
显式选择保留、「（默认）」移至 30 秒）；(4) 首启 seal 缺陷**本归档
已修复**（两轮真实启动证据、codesign 0→0，明确取代 0.4.2 已知缺陷
披露，不复用旧结论）；(5) 内部预览渠道标注（ad-hoc、prerelease、
受众、Gatekeeper）与安装方式（Finder/ditto，禁 unzip）；(6) 解锁后
约 10 秒像素复核移交（五项清单，引用 joint-acceptance-report.md §6
边界与移交）；(7) 兼容组合 ORX v0.4.0（schema 8–15）＋ store v4
升级链。

## 4. 消费者侧核对（verify-remote，规定检查绿）

发布后从新 Release **下载全部资产**（另自 ORX v0.4.0 Release 下载
ORX wheel）重算哈希：四个产物与 candidate.json/manifest 冻结值逐字节
一致；`SHA256SUMS.txt` 逐行与复算值吻合；`manifest.json`、兼容组合
说明与上传副本字节相同。`uv run python scripts/joint_release.py
verify-remote --work-dir dist/g012 --channel-root
dist/g012/channels/internal-preview` 绿：internal-preview 渠道根
（`dist/g012/channels/internal-preview/`，由已发布 Release 下载资产
填充）绑定一致、产物逐字节一致、无 DMG 混入（渠道隔离）。manifest
七阶段（freeze/battery/build/candidate/exercise/verify/verify-remote）
全 passed。远端新标签提交与全部历史标签对象映射逐一核对通过
（§1 表；含 annotated tag 对象与 peeled commit）。首次下载尝试遇
代理瞬时 SSL EOF，重试后完整下载——不影响发布物本身。

## 5. 渠道结论

- **内部预览（internal-preview）**：已发布（本 Release，prerelease）。
  App 归档 ad-hoc 签名、未公证，受众仅内部测试，Gatekeeper 拦截首次
  启动需手动确认（安装文档在案；Finder/ditto 解压）；渠道身份在标题/
  正文/Info.plist/包内 distribution.json 四维一致。
- **正式分发（official）**：保持受阻，未发布。无 Developer ID
  Application 证书、无 notarytool 存储凭据（2026-10-17 实测，
  `distribution.json`）；真实签名/公证/装订/安装评估证据齐全前不产出
  DMG、不使用正式分发表述。
- Analytics Python 分发物（wheel/sdist）：随 v0.4.3 Release 正常分发
  （构建自冻结提交、check_install 全过）。
- ORX v0.4.0：产品、标签、Release 资产全部原样（§1、§3）。

## 6. 遗留移交（如实）

- 解锁后约 10 秒像素级人工复核（验收报告 §6 边界与移交，五项清单）
  随 Release 说明移交，结果无论通过与否需如实记入后续发布说明。
- 本机 `ORX_ANALYTICS_BIN` 未设置、外部看护不可用——不影响本循环。
