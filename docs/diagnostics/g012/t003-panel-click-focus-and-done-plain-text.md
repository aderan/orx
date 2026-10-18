# G012 T003 — 点击项目窗口前置聚焦 + done 控制态降为普通文字

日期：2026-10-18（用户实测反馈批次，第 2、3 条）。工作区：`.release-workspaces/orx-analytics`。

## 用户反馈与根因

1. 反馈 2「点击项目卡片后 run-detail 窗口未到最前方」：
   `QuickPanelView` 的卡片点击只调 `openWindow(id: "run-detail")`。单一
   `Window` scene 会复用既有窗口（不会重复开窗），但不会激活本应用
   （menu-bar LSUIElement 应用常在后台），也不会可靠地把被其他应用遮挡的
   窗口重新前置取键——通知深链路径 (`RuntimeCoordinator`) 早已有
   `NSApp.activate(ignoringOtherApps: true)`，面板路径漏掉了同样的处理。
2. 反馈 3「目标已完成 的控制徽标过于显著」：`PanelRunCard.controlBadgeText`
   对所有已知非 active 控制态一律给橙色徽标，终态 done 与 需要干预的
   已暂停/取消中 同等强调，噪音过高。

## 修复

### 窗口前置（复用现有路由，不新增窗口）

新文件 `apps/macos/Sources/ORXAnalyticsApp/DetailWindowRouting.swift`：

- `openRunDetailFrontmost(_:)` = `NSApp.activate(ignoringOtherApps: true)`
  → `openWindow(id: "run-detail")`（单一 Window scene，复用即无重复窗口）
  → 下一 runloop tick 做 `makeKeyAndOrderFront` 前置遍历（按
  identifier 含 `run-detail` 或标题含 `执行详情` 匹配）。
- 接线：面板卡片点击、底部「查看全部执行…」、以及 `OpenWindowDonation`
  捐赠给通知深链的闭包（原只 activate+openWindow，现同样取键前置）。
- `RunDetailHostView` 侧双保险：捕获宿主 `NSWindow`（NSViewRepresentable
  accessor），`onAppear`（首次打开）与 `onChange(of: model.selectedRun)`
  （连续点击不同项目 / 通知深链改选中）都前置取键；窗口引用未就绪时回退
  到遍历匹配。
- 覆盖矩阵：首次打开（openWindow 新建 + 前置）；已开但被遮挡后再点
  同一项目（选中不变 → 点击路径的异步前置遍历兜底）；连续点击不同项目
  （AppModel 选中跟随最后一次点击，单一窗口 + host onChange 前置）。

选中项路由本身未改：`AppModel.openRunDetail/openAllRuns` 维持"先设
selectedRun 再开窗"的既有次序（新增测试钉住：连续不同项目只剩最后一次
选中；查看全部执行回到入口列表）。

### done 控制态降为普通文字（G011 分层不变）

`apps/macos/Sources/ORXAnalyticsCore/PanelSections.swift`：

- `controlBadgeText`：已知非 active 且非 done 才出徽标
  （已暂停/取消中/已取消 保留橙色徽标）。
- 新增 `controlPlainText`：仅终态 `done` 返回「目标 已完成」——
  在 `QuickPanelView` 卡片头以 `caption2 + secondary` 普通文字呈现，
  无橙色背景、无强调样式，但文字仍可见、tooltip 维度说明保留。
- 维度分立不回退：`controlStatus`、状态行汇总 `activeControlCounts`
  （含 已完成 计数）不动；执行状态 (`run.status`) 与未知状态诚实展示
  （secondary 色 captions）原样保留。

## 测试

- `PanelSectionsTests`（TDD，先红后绿）：
  `testDoneControlRendersAsPlainTextNonTerminalStatesKeepBadge`（done 出
  plainText 不出徽标；已暂停/取消中/已取消 出徽标；active/缺省/未知 两者
  皆无；执行状态不被改写）、
  `testControlPresentationIsMutuallyExclusiveAcrossStates`（徽标/普通文字
  互斥完备；done 在最近完成里仍可数）。
- `AppModelTests.testDetailSelectionRoutesCardClicksAndKeepsLastSelection`：
  首次点击选中、连续不同项目跟随最后点击、查看全部执行清空选中。
- 命令检查：filtered swift test（32 通过）、swift build、git diff --check
  全绿。真实窗口前置/键盘焦点/单窗口由独立 verifier 的 vision 检查在构建后
  的 App 上验收。

## 重试与验证记录（attempts 236–240）

- attempt 236：原始交付，3 项命令检查全绿。
- attempt 237：agent[vision] 观察未能执行——操作者桌面会话处于锁定状态，
  截屏只捕获到锁屏画面（lock-screen 证据在案），真实 UI 观察物理上不可
  进行。实现本身未受影响（attempt 236 交付的 3 项命令检查全绿）。
- attempt 238（rev1 重试）：实现零改动，三项命令检查重跑全绿（filtered
  swift test 32/32、swift build、git diff --check）。
- attempt 240：rev1 独立验收——agent[vision] 条目再次如实 fail：会话仍
  锁定（13:46 黑屏探针与锁屏证据在档），真机视觉验证持续物理不可行。
  两次 fail 均为验证环境边界，非实现缺陷。

## 修订 2 重派收口（attempt 247）

- 重派原因（replan 裁定）：rev1 的 T003 实现与全部确定性检查已在
  attempts 236/238 通过；唯一 fail 来自 agent[vision] 条目，验证者两次
  如实留档（237/240）。本修订将该条目改为条件式——解锁则真机验证；
  锁定则边界记录 + 解锁后人工复核移交——实现零改动，命令门禁复跑收口。
- attempt 247（本次）实现零改动复验，关键证据均在位：
  `DetailWindowRouting` 激活链（NSApp.activate → openWindow →
  makeKeyAndOrderFront 前置遍历）；`RunDetailHostView` 的 `onAppear` /
  `onChange(of: model.selectedRun)` 前置与窗口引用回退查找；
  `PanelSections` done→`controlPlainText`（终态）与非终态
  （已暂停/取消中/已取消）徽标互斥渲染；`AppModelTests` 选中路由测试。
  三项命令检查复跑全绿：filtered swift test 32/32（含 PanelSectionsTests
  11 项 0 失败）、swift build、git diff --check。
- 会话锁探测（14:01–14:02 CST 复测）：screencapture 探针返回整屏纯黑
  （无锁屏 UI、无桌面内容可见），与 13:46 黑屏探针及锁屏证据一致——
  操作者会话仍处锁定状态，真机视觉验证持续物理不可行；不以模拟器替代。
- 解锁后人工复核移交（G011 §9.1 同式，操作者解锁会话后执行）：
  1. 点击快速面板项目卡片 → run-detail 窗口前置并取得键盘焦点，
     展示所点项目/run，不产生重复详情窗口；
  2. 窗口被其他应用遮挡后再次点击（同一或不同项目）→ 窗口再次前置
     取键，连续切换项目时选中跟随最后一次点击；
  3. done 项目显示普通文字「目标 已完成」（无橙色徽标背景/强调样式）；
     已暂停/取消中/已取消 项目继续显示橙色控制徽标。
