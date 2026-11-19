# ORX — macOS 菜单栏应用（apps/macos）

Swift Package（Xcode-free 构建链）承载设计稿 §7/§9 的原生外壳：
AppKit `NSStatusItem` 承载 SwiftUI 快速面板及进行中 Run 数徽标，
`LSUIElement` 保持菜单栏应用形态。状态项由系统控制可见性，Finder
可随时打开唯一主窗口。引擎 Unix socket 客户端供应快速面板和执行详情
（设计 §3/§4/§5）。

## 界面和设计系统

[DESIGN.md](DESIGN.md) 记录设计系统、复用组件及数据展示规则。各界面共用
`Sources/ORXAnalyticsApp/DesignSystem/` 的 tokens，主题设置同时作用于
快捷面板、主窗口、设置和诊断。

## 构建 / 测试 / 冒烟（不打开 Xcode）

```sh
swift build                 # 或 ./scripts/build_macos_app.sh
swift test                  # 协议客户端、缓存、缓存优先、口径规则、详情模型单测
./scripts/smoke_macos_app.sh   # 构建后以 --smoke-test 自检并退出
./scripts/build_app.sh dev     # 可安装 Dev 版 .app + zip（内置 Python 运行时）
./scripts/build_app.sh dist    # 正式分发 .app + DMG
```

最低 macOS 13（`Package.swift` platforms）；Apple Silicon。可安装包的
构建、内置运行时固定版本与校验和、dev/dist 区分、安装升级回退说明见
[docs/app-installation.md](../../tools/orx-analytics/docs/app-installation.md)（Stage 5 / T012）。

## 引擎由 App 启动管理（设计 §9）

App 是引擎的生命周期管理者：socket 无应答时用包内固定版 Python 运行时
启动 `Resources/orx_engine_serve.py`（单实例锁 → 用户私有 socket → 有界
重启的调度器），就绪后重试连接；协议不兼容只报告不接管。启动前若检测到
analytics.db 需要跨版本打开，先备份再放行（升级保护）。引擎进程不随 App
退出而被杀——CLI watcher 的需求必须存活（§7）。`swift run` 无内置运行时，
开发替代见 app-installation.md「开发运行」。

## 结构

| 位置 | 职责 |
|---|---|
| `Sources/ORXAnalyticsCore` | 引擎 socket 客户端（overview / runs.get / tasks.get / refresh.request、版本协商、不兼容明确报告）、overview 与详情负载模型（对齐 Python 线格式）、缓存优先存储、AppModel、RunDetailModel、面板三区合并、口径与维度展示规则、引擎进程管理（内置运行时解析、升级备份守卫、有界拉起） |
| `Sources/ORXAnalyticsApp` | AppKit 菜单栏和窗口生命周期；SwiftUI 快速面板、执行详情、模型与路由、用量和设置；`--smoke-test` 自检 |
| `Resources/Info.plist` | `LSUIElement` 等应用元数据；SwiftPM 构建时嵌入 `__TEXT,__info_plist`，Stage 5 打 `.app` 包时复用 |
| `Resources/orx_engine_serve.py` | 引擎服务入口（App 拉起）：acquire → start_server → EngineSupervisor；包内分析代码由构建脚本放入 Resources |
| `Tests/ORXAnalyticsCoreTests` | 合成来源单测（EngineSimulator，不连真实引擎、不读真实会话） |

## 契约要点（实现必须遵守）

- 徽标数字固定为进行中 Run 数；关注标记附带文字说明（设计 §3）。
- 快速面板三区：同一 Run 只出现一次（带关注事项的 Run 只出现在“需要关注”）、
  异常优先排序、卡片点击带着 Project+Run 上下文打开执行详情（设计 §3）。
- 进度只表达“已完成任务数 / 当前计划任务数”，与全部版本的已观测消耗分开
  呈现；Revision 默认收起，历史计划消耗单独列示（设计 §3）。
- 详情四区数据全部来自引擎查询接口（runs.get / tasks.get）；Swift 层不写
  业务 SQL、不另实现 token 算法（设计 §7）。模型分布按尝试计数展示，
  不在展示层重算 token 归属。
- 执行状态 / 关注事项 / 数据健康三维度分立呈现，未知值显示"—"
  而非 0（设计 §4）。
- 协议版本不兼容：明确报告 App/引擎两侧版本并停止查询，不强行接管（设计 §7）。
- 快速面板卡片点击（含通知深链与“查看全部执行”）打开执行详情时激活应用并把
  run-detail 单一窗口前置取键（`NSApp.activate` → `openWindow` →
  `makeKeyAndOrderFront`），不产生重复详情窗口；连续点击不同项目时选中跟随
  最后一次点击（G012 用户反馈 2）。
- 控制状态维度分层不变：Goal 控制状态读原文，终态 `done` 在面板卡片中显示
  为次要文字「目标 已完成」；非终态
  `已暂停/取消中/已取消` 保留橙色控制徽标；状态行汇总计数（含已完成）
  不受影响（G012 用户反馈 3）。
- 打开面板先渲染缓存，再异步刷新；详情窗口异步加载、失败保留上次成功数据
  并附原因；刷新不阻塞主线程（设计 §3/§8）。默认刷新节律整体下调
  （G012 用户反馈 4）：面板/详情默认 30 秒（设置项保留 5/10/15/30/60，
  显式选择过的既有偏好保留），引擎分级 30/60/300 秒，派生 lane 随之缩放；
  数据新鲜时打开面板不再立即补刷一轮，详情窗口加载是纯读查询不产生采集需求。
- token 数字沿用冻结口径：未知显示 "—"，只有明确记录为零才显示 0；
  ORX 已记录用量与原生会话预览两口径独立展示、不相加（设计 §5，docs/metrics.md）。
  展示统一千位分隔（固定三位逗号，不随 locale 变化，仅影响显示不改存储值，
  G012 用户反馈 1）；原生会话预览口径内含 controller 自身请求用量与归属判定
  （attributed / unattributed 附原因等，跨 run 共享时显示会话累计并强制标注
  不摊分；见 docs/metrics.md「controller 自身用量」，G012 用户反馈 5）。
- 引擎 socket 默认 `<data-dir>/engine.sock`，data-dir 沿用 CLI 约定
  （`ORX_ANALYTICS_HOME` 覆盖，默认 `~/.orx-analytics`）。
- 关闭详情窗口应用继续驻留菜单栏，只有“退出”结束；清理显示缓存是设置里
  单独、明确的操作；退出/卸载不删除 ORX 项目数据（设计 §9）。

引擎协议契约见 `tools/orx-analytics/src/orxanalytics/engine.py` 与 `tools/orx-analytics/src/orxanalytics/query_model.py`
（本仓 Python 侧，Stage 2 交付）。

## ORX 执行与配置

菜单栏的「查看全部执行」打开一个 ORX 窗口。执行和用量沿用 Analytics
引擎的历史、Run、Task 查询。从任务用量打开「模型与路由」时，页首选择器
定位到该 Run 的完整项目路径。路由页也可选择用户默认或其他已登记项目。

配置页优先在常见用户工具目录中查找 `orx`，并允许指定可执行文件绝对路径。
页首显示实际使用的安装版本、目标层和文件路径。执行与用量仍由本地引擎
提供，适用于只安装 Analytics 的环境。

编辑规划深度或候选顺序后，点击「预览变更」核对目标层和新生效值，再点击
「保存」。并发编辑导致预览失效时，重新载入后重做变更。ORX 按作用域
校验 profile，并一次替换目标配置文件。后续派发读取新配置。已有尝试
继续展示历史请求及可观测的实际模型、推理和已关联的路由记录。

机器接口见 [桌面路由配置契约](../../docs/desktop-routing.md)。
