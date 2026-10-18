# G012 T002 — token 千位分隔：格式器修复与展示插值审计

日期：2026-10-18（用户实测反馈批次）。工作区：`.release-workspaces/orx-analytics`。

## 修复（单一入口）

`apps/macos/Sources/ORXAnalyticsCore/MetricsFormat.swift` 的 `format(_:)` 原设
`groupingSeparator = ""`（全站无分隔，用户反馈第 1 条）。改为**固定三位逗号
分组**，且分组按算术计算（不经过 ICU formatter / locale 查询）：`de_DE` 无法
把 `1,200` 变 `1.200`，`hi_IN` 无法重排为 `12,34,567`。该函数保持 private，
对外仍只有 `tokenText` / `countText` 两个入口（T005 controller token 展示将
复用同一入口）。

契约不变项（行为测试钉住）：未知 `—`、显式 `0`、两口径（ORX 已记录 / 原生
会话预览）并列不相加、进度仍为"已完成/当前计划任务数"分数而非百分比。

## 统一走格式器的展示口径

- `progressText`：分子分母同分组（`12,345/67,890`）。
- `coverageText`：`已观测 1,234/5,678 次尝试`。
- `missingText`：缺失行数为计数，`input_tokens 缺 2,000 行`。
- `consumptionText` / `UsageCalibers` / `DetailCalibers` / `HistoryRunRow`：
  原已走 `tokenText`，随格式器自动获得分组。

## 审计：原先绕过格式器的插值点（全部改为 `countText`）

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `QuickPanelView` | statusLine | 进行中数、控制层计数、需要关注数 |
| `QuickPanelView` | 三个 section 标题 | 需要关注/进行中/最近完成（N 个执行） |
| `ORXAnalyticsApp` | 菜单栏徽标 | 进行中 Run 数（语义不变，仅格式） |
| `RunDetailView` | 任务列表 | `revision.taskCount`（N 任务） |
| `RunDetailView` | 模型分布 | 尝试次数（N 次） |
| `RunDetailView` | 原生预览副标题 | 会话数（N 个） |
| `RunDetailView` | 准确度分布 | exact/estimated/unknown 计数 |
| `RunDetailView` | 标准化处理量 | `rowsMissing` 经 `partialSuffix`（部分和缺 N 行；nil 为"缺 — 行"） |
| `DiagnosticsView` | 引擎区 | 连续刷新失败次数、失败阈值 |
| `DiagnosticsView` | 来源区 | 健康来源计数（两处） |
| `HistoryModel` | `rangeText` | 分页窗口 `第 a–b 条 · 共 N 条` |
| `DetailPresentation` | `RunDimensions` | 当前计划失败任务数 |
| `NotificationPolicy` | 通知标题/正文 | 需要关注项数（含"…等 N 项"） |

## 审计：明确不改的数值插值（标识/版本/时间，非数量）

Run/Task/Session ID、`Revision n`、attempt `#id`、快照 `#id`、引擎协议
`v{version}`、schema 版本、退出码、时间戳与时长（`ageText` 的小时/分钟/秒）。
格式器只服务数量展示，避免把标识误改成 `1,024` 一类。

一处顺带的诚实性修复：`RunDetailView` 标准化处理量在 partial 且
`rowsMissing` 为 nil 时原为 `countText(rowsMissing ?? 0)`，把未知补零显示
"缺 0 行"。现把该后缀收敛为 `MetricsFormat.partialSuffix(_:)`（视图改为
调用它）：仅 partial 才追加"（部分和，缺 N 行）"，N 走 `countText`——nil
渲染"缺 — 行"、显式 0 保留"缺 0 行"、已知计数按千位分组（如
"缺 1,200 行"），与"未知保持未知"契约一致。

## 测试

- `MetricsFormatTests`：999 / 1,000 / 1,234,567 / 显式 0 / 未知 / 负样本
  （两口径不合并）；locale 翻转（de_DE、hi_IN、fr_FR、zh_CN，`setenv`+
  `setlocale` 尽力而为）下分组不变；进度与覆盖率分子分母分组；缺失行数分组；
  部分和后缀——nil →"缺 — 行"、1,200 →"缺 1,200 行"、显式 0 →"缺 0 行"、
  非部分和无后缀。
- `DetailModelsTests`：wire 夹具输入 1000 → `"1,000"`。
- `HistoryModelTests`：历史行 `已观测 token 1,200（全部版本）`。
- `RunDetailModelTests`：当前计划失败任务 1,234 → `当前计划失败任务 1,234`。
- `SmokeRunner`（`--smoke-test` 自检）：分组规则同步更新。

数值展示不改变底层值、标识或用量计算；ORX 已记录与原生预览仍分别显示。
