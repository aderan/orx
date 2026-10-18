# G012 联合验收报告（T007 收口）

日期：2026-10-18。本报告是 Analytics 0.4.3 冻结候选的联合验收记录
（T007）；权威顺序见 [joint-acceptance-runbook.md](joint-acceptance-runbook.md)，
基线见 [baseline.json](baseline.json)。发布（阶段 5，T008）不在本报告范围。

## 1. 冻结与绑定（freeze --goal-id g012，passed）

| 项 | 值 |
|---|---|
| Analytics 冻结提交 | `d0867103a447e94ca1fd01731a60be62aeb2823a`（0.4.3；版本自提交树读出） |
| ORX 产品输入 | 发布标签 `v0.4.0` → `f1cc7757c953d3fee5bff5630613a6d2fb380a74`（pin 复核在 freeze/candidate/verify 三处；移动即 `orx-pin-moved`） |
| 验收驱动提交 | `043eecbdeed6f88108fd9ed126fbd40991c1f872`（binding.acceptance_driver，仅溯源非输入；含驱动 G012 扩展、电池联合布局修正、测试与 g012 手册/基线/决定文件） |
| 渠道决定 | `docs/diagnostics/g012/distribution.json`（sha256 `a94df4c5…` 按候选绑定）：internal-preview 允许（ad-hoc dev zip）；official 受阻（凭据缺口 2026-10-17 实测仍在） |
| 样本溯源 | store-v2 样本再生比对一致；当前 analytics 升级链（v2→v3→v4 增量、行保留、源样本字节不变、未来版本 9 拒绝）绿；旧代码拒读更新 ORX 绿 |
| 冻结时未提交输入 | ORX 侧仅受保护用户报告 `docs/m1.2-report.md`（处置见 baseline.json；发布物取提交态）；Analytics 克隆仅未跟踪生成目录 `apps/macos/.build/`（g012 豁免、构建一律物化提交树） |
| 历史标签 | 全部原样（analytics v0.4.2→2461a260；ORX v0.1.0–v0.4.0；候选 `historical_tags_unchanged: true`） |

## 2. 电池（battery，passed——五项全 0）

- `orx_pytest`＋`orx_check_release`：自 **v0.4.0 pin** 一次性干净克隆。
- `analytics_pytest`＋`analytics_swift_test`＋`analytics_check_install`：自
  **0.4.3 冻结提交**独立干净克隆（`battery/analytics`）。
- **联合布局修正（本轮验收发现并修复的驱动缺口）**：v0.4.0 套件自带的
  joint 测试钉住其自身发布对环境（analytics 0.4.2/store v3）；首轮 battery
  把 0.4.3 冻结提交放进联合布局导致 `upgraded store_version '4' != '3'`
  失败。修正为：pin 克隆旁联合布局携带**已发布 analytics v0.4.2 对**
  （tag 移动即 `analytics-v042-pin-moved` 失败关闭，绝不重定位——与
  `orx-pin-moved` 同纪律），0.4.3 冻结提交的三项自有检查在其独立干净
  克隆执行。**任何检查不跳过、不替代、不放宽**；g011 路径零变化
  （tests/test_joint_release.py 29 项全绿，含新增
  `test_g012_battery_joint_layout_pins_the_v042_release_pair`）。该修正
  作为独立驱动提交 `043eecb` 落库后**重跑 freeze→全链**，绑定记录最终
  驱动提交。

## 3. 构建、候选与只读复验（build --build-app / candidate / verify --phase candidate）

- 最终 App：冻结提交经 build_app.sh `dev`（内部预览渠道门：决定校验、
  固定运行时 sha256、自测先行禁写码、签名前封条基线、归档前封条比对、
  全门禁后记哈希）。
- 产物（`published: false`，目标标记 **G012/g012**，无任何 G011 表述）：

| 资产 | SHA256 |
|---|---|
| `ORXAnalytics-0.4.3-internal-preview-dev-macos13-arm64.zip` | `461a2f037f9e94125d559ad57def4166383ba6f06a50d55c7014813909d63824` |
| `orx_analytics-0.4.3-py3-none-any.whl` | `7f7c6692a1559e5c736d732a3db780f5588500f505245b2b0dd2bf01aba73ef9` |
| `orx_analytics-0.4.3.tar.gz` | `b25327a8f8d456eaba912282d83043ee858d24822d1cc76b3be60161ed989a2b` |
| orx_agent-0.4.0 wheel（pin 溯源重建，非发布动作） | `f71c19aebd2dcd1a09524602be50ad730b194d743f52925ea59fd75c2d2a212f`（与已发布 v0.4.0 Release 资产一致） |

- 版本一致性：wheel/sdist 元数据、App `CFBundleShortVersionString`、包内
  引擎版本、发布说明全部 **0.4.3**；ORX 产品版本 **0.4.0**（仓冻结源码
  即已发布 v0.4.0，0.4.3 电池自 pin 克隆验证）。ad-hoc 签名验签 exit 0。
- 候选只读复验（规定检查 1）绿：摘要/身份/电池证据/包与 App 元数据/
  签名/决定 sha/历史标签/样本溯源/pin 复核/目标标记一致性。

## 4. 隔离演练（exercise，passed）

冻结 ORX wheel 一次性 venv 驱动隔离项目全生命周期（暂停期间完成不冒充
done、回执重放零变更、cancelling→cancelled、恢复与未确认执行者、历史
可查、控制/执行分层），最终 App 内置引擎 socket 两次采集只读断言
（bundled_analytics_version 0.4.3、read_only_source true、
no_real_session_mirrors true、stand_in_app_delivery **false**——无替身
冒充最终 App）。

## 5. 安装副本与首启/重启封条（runbook §5，先于 verify）

- 归档哈希复核==候选后 **ditto** 安装到 `dist/g012/installed/
  ORXAnalytics.app`；Info.plist：channel=internal-preview、
  version=0.4.3、source_commit=d0867103…、内置 cpython-3.12.8+20250115。
- `check_runtime_seal.py`（规定检查 2）对**该安装副本**两轮真实启动：
  first-launch 与 engine-restart 均取得真实启动证据（引擎=App 子进程经
  生产 makeChild、命令行指向包内运行时、socket 连接、启动日志落盘）；
  前后 bundle 2000 条目 added/removed/changed **全空**（零新增
  `__pycache__`）；`codesign --verify --deep --strict`（规定检查 3）
  前后均 exit 0；对抗环境 `PYTHONDONTWRITEBYTECODE=0` 下封条完好只可能
  来自 App 自身强制。verdict **pass**。v0.4.2 同路径 73 pyc/验签 0→1 的
  已披露缺陷就此闭环。
- 升级链：0.4.2 数据升级在演练与封条链中以私有副本验证（备份、镜像补
  齐、重复采集幂等——freeze 升级探针＋exercise 全生命周期＋电池
  analytics_pytest 279 项）；真实 ORX/zcode 数据零污染（演练/验收全部
  一次性目录；源库只读打开）。

## 6. 用户五项反馈对安装后实际 App 的验收

全部五项的引擎级事实 + App 级自检（安装后二进制 `--smoke-test` PASS，
含千位分组规则）+ 冻结电池（swift 全量钉住展示层）；UI 级观察与边界
见 `dist/g012/gui-evidence.json` 与
`dist/g012/gui-acceptance/`（徽标实时读数、AXPress 打开真实面板的
窗口服务器证据、95 秒刷新节律实测、引擎五项事实 JSON）。CGSession
探针判定会话解锁（onConsole=1/loginDone=1，操作者自有窗口同屏渲染），
但像素截屏受 Screen Recording 权限拒绝（全黑帧实测）且 SwiftUI 状态
面板不暴露 AX 内容（两代实例同证）——像素级五项复核按 rev2 条件式
边界记录＋解锁/授权后人工复核移交（清单在
`gui-acceptance/screencapture-boundary.txt`，G011 §9.1 同式）。

1. 千位分隔：引擎实测大数（R012 input 82,573,883；R011 184,766,456；
   controller 109,266,766）经同一 `MetricsFormat` 渲染；安装后二进制
   自检钉死 `1,200`/`1,234,567`；未知 —/显式 0/两口径并列不变。
2. 点击项目窗口前置：`DetailWindowRouting` 链＋宿主双保险在冻结树，
   battery 测试钉住（单窗复用、选中跟随最后点击）；像素级在移交清单。
3. done 普通文字：引擎最近完成区终态 done 原文（R011/R010/R009/R005/
   R003）→ `controlPlainText` 渲染；非终态徽标保留、互斥完备由
   PanelSectionsTests 钉死；各控制状态与未知值语义不变。
4. 刷新节律：面板打开 95 秒实测引擎状态轮 **~30.3 秒/轮**（3 轮；
   旧 5 秒缺省同窗口约 19 轮）；缺省 30 秒与设置项语义由
   AppSettingsTests 钉死（显式选择保留）。
5. controller 归属：R005/R003 attributed（own_usage：281 请求 input
   66,837,410；59 请求 8,403,221；processed=input+output、错误零占位
   显式）；R012/R011 unattributed 附真实原因（子会话分属两 run）＋
   会话累计带不摊分强制标注；双口径隔离不变（controller 数字仅在
   native_session_preview 口径内）。

## 7. 规定检查（`orx task check T007`，全绿）

1. `joint_release.py verify --manifest dist/g012/candidate.json --phase candidate` — ok
2. `check_runtime_seal.py --app dist/g012/installed/ORXAnalytics.app --evidence-dir dist/g012/runtime-seal` — ok（两轮规定复跑均 pass）
3. `codesign --verify --deep --strict dist/g012/installed/ORXAnalytics.app` — ok
4. `joint_release.py verify --work-dir dist/g012` — ok（全链：产物哈希重算、身份、全部阶段证据、App/GUI/签名证据，无 skip 后通过）
5. `git diff --check` — ok
6. `git -C .release-workspaces/orx-analytics diff --check` — ok

## 8. 冻结纪律与后续

- 冻结后仅追加证据文档（本报告、clone 侧
  `reports/release-v0.4.3-acceptance.md`、IMPLEMENTATION_PLAN 阶段记录），
  **零产品源码混入**；若后续验收发现产品问题，须重新提交、冻结并重跑
  受影响完整门禁。
- 阶段 5（T008）：从冻结候选字节发布——analytics 冻结提交打轻量标签
  `v0.4.3` 推送、GitHub prerelease 上传冻结资产、发布后下载核对与
  `verify-remote` 双绿、发布记录 `release-record.md`。ORX v0.4.0 Release/
  标签零动作。
