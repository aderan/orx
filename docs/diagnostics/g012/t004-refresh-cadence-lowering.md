# G012 T004 — 统一降低面板/详情/引擎刷新与采集频率

用户反馈 4)：数据更新太过频繁。本页记录改动、受控时钟证据与语义保持。

## 改动总览（orx-analytics 0.4.3）

| 位置 | 旧 | 新 |
|---|---|---|
| 引擎分级 `TIER_INTERVALS`（engine.py） | ui 5s / active 15s / idle 60s | ui 30s / active 60s / idle 300s |
| 需求租约 `DEMAND_LEASE_SECONDS`（engine.py） | ui 30s / background 90s（=6×间隔） | ui 180s / background 360s（=6×新间隔；≥2×对应 tier 间隔，单次漏续不降级） |
| CLI 持有进程续约 `HOLDER_RENEW_SEC`（watch.py） | 30s | 60s（=引擎 active tier；每次续约同时合并提交一个后台刷新任务，不得快于 tier） |
| 面板默认刷新（AppSettings.defaultPanelRefreshInterval） | 5s | 30s |
| 详情自动刷新（RunDetailModel.startAutoRefresh） | 独立硬编码默认 5s | 无默认值：由调用方传入共享偏好（RunDetailView 传 `app.settings.panelRefreshInterval`，默认 30s） |
| 派生 lane 间隔（随 tier 自动缩放） | usage max(20, 4×tier)=20/60/240；anomaly 2×tier=10/30/120 | usage 120/240/1200；anomaly 60/120/600 |

`WAKE_SLACK`、`FULL_RECHECK_INTERVAL`、`HISTORY_AUDIT_INTERVAL` 不变；唤醒判定按当前 tier 间隔自适应（idle 300s 下 gap>330s 才视为睡眠）。

## 受控时钟证据（旧 vs 新，同一 300s 窗口，FakeClock 步进 1s，无长真实 sleep）

`tests/test_engine.py::test_lower_default_cadence_controlled_clock_comparison`：旧节律用引擎可注入 `intervals={"ui":5,"active":15,"idle":60}` + 旧租约 {30,90} 模拟，新节律即模块默认。ui tier 下逐秒 tick，客户端每轮续约：

| lane | 旧（runs/300s） | 新（runs/300s） |
|---|---|---|
| state | 61（每 5s） | 11（每 30s） |
| usage | 16（每 20s） | 3（每 120s） |
| anomalies | 31（每 10s） | 6（每 60s） |

每个 lane 均严格 fewer；精确值在测试中逐一断言。App 侧 `AppModelTests::testAutoRefreshRoundCountScalesWithIntervalInSameWindow` 用同一 2.6s 观察窗口对比 0.4s（旧默认替身）与 1.2s（新默认替身）节律的自动刷新轮数（快≥4、慢≤3 且 旧>新）；默认值 30s 由 AppSettingsTests/AppModelTests 直接断言，`startAutoRefresh(interval:)` 缺省取 `settings.panelRefreshInterval`。

## 重复立即刷新与多窗口重复采集的消除

- 打开面板原来立即刷两轮：`bootstrap()` 的后台刷新 + `startAutoRefresh()` 循环首轮再刷。现循环改为 sleep-first（先等一个完整间隔），立即轮只剩 bootstrap 一条路径（`testPanelOpenDoesNotFireDuplicateImmediateRefreshRounds`）。
- 重复打开面板：`bootstrap()` 增加新鲜度门槛——距上次成功刷新不足当前间隔时跳过立即刷新，交给循环按节奏刷（`testBootstrapSkipsImmediateRoundWhenDataIsFresh`）。手动刷新与唤醒同步不受门槛影响（`testManualRefreshIsImmediateEvenWhenDataIsFresh`、既有 wake 测试）。
- 面板+详情同时打开：详情窗口加载是纯读查询（runs.get/tasks.get），从不发送 `refresh.request`；采集需求仅由面板循环的 panel-open 持有（`testDetailWindowLoadsAddNoCollectionRequests`）。引擎侧单实例锁 + 单 pending 槽合并 + lane 调度保证多客户端/多窗口不可能双采集（既有 `test_second_engine_cannot_start_or_collect`、`test_refresh_requests_coalesce_bounded` 继续通过）。

## 需求租约与新节律一致

`test_demand_leases_aligned_with_lower_cadence`：租约 ≥2× 对应 tier 间隔（漏一整轮不降级），未续约最终按租约过期释放；`test_watch_engine.py::test_holder_renewal_paces_background_jobs_to_the_new_cadence`：`HOLDER_RENEW_SEC(60) ≥ TIER_INTERVALS["active"](60)` 且 `2×60 ≤ background 租约 360`。

## 升级迁移（已保存偏好保留，未显式选择采用新默认）

`UserDefaultsAppSettingsStore` 新增 `panel.refresh.interval.chosen` 标记（仅设置页显式选择时置位）。读取规则：标记+有效值 → 保留；无标记但值 ≠ 旧默认 5 → 保留（0.4.2 只有显式选择才可能存出非 5 值）；其余（无值或恰为旧默认 5 且无标记）→ 新默认 30。设置页选项 5/10/15/30/60 全保留，"（默认）" 标记移到 30 秒，与实际缺省一致（`testUpgraderWithIncidentalLegacyDefaultAdoptsNewDefault`、`testUpgraderExplicitNonDefaultChoiceIsKept`、`testExplicitFastChoiceIsKeptViaTheMarker`、`testIncidentalResaveKeepsExplicitRefreshChoice`）。

## 语义保持（既有测试全部继续通过）

手动刷新立即生效、并发刷新合并为一轮（`testConcurrentRefreshCallsCoalesceIntoOneRound`、`test_refresh_requests_coalesce_bounded`）；缓存优先首帧（bootstrap 同步发布缓存）；唤醒重新同步（`testWakeIssuesResyncRoundAndClearsFlagAfterSuccess`、`test_sleep_wake_no_stall_verdict_and_resync`）；失败保留旧数据（`testFailedRefreshKeepsLastGoodCacheDisplayed`、`test_per_project_failure_isolation_keeps_last_success`）；后台运行语义（App/CLI 各自需求、退出互不影响）。完整套件：Python 123 passed（工作区根目录全量），Swift 159 executed / 0 failures（5 项与本次无关的既有跳过）。

## 用户实机不受影响

改动仅在 release-workspace 克隆内；用户 /Applications 预览与 ~/.orx-analytics 未触碰。
