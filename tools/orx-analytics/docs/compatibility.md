# 版本兼容与来源能力

软件版本、ORX 数据 schema、analytics 存储 schema 独立演进。
orx-analytics 0.4.3 支持以下明确的数据版本，不按“大于等于”推测兼容。
**经验证的兼容组合：ORX v0.4.0 + orx-analytics v0.4.3**（ORX 软件
版本 0.4.0、ORX schema v15、analytics 0.4.3、store v4——四条版本轴
各自独立，两个仓库的软件版本不必同号）。

| ORX schema | 本工具读取能力 |
|------|------|
| 8 | 核心执行事实、用量、验证、会话引用 |
| 9 | 另含重规划对应关系 |
| 10 | 另含显式 attempt progress |
| 11 | 另含指派 nonce 身份锚 |
| 12 | 保留 v11 形状；接受用量来源 `zcode_db` |
| 13 | 保留 v12 形状；`goals.status` 新增 `paused`（非终态，读原文） |
| 14 | 保留 v13 形状；`goals.status` 新增 `cancelling`（非终态，读原文） |
| 15 | 保留 v14 形状；新增 `completion_receipts` 回执表 |
| 其他、缺失或非法值 | 拒绝，不迁移、不补列、不改版本号 |

analytics 自己的 store 版本为 4，可以将 v1/v2/v3 数据原地进行增量迁移；
无法读取未知未来版本。v3 → v4 为**纯增列**：`zc_requests` 镜像新增
zcode `model_usage` 的逐请求 token 列，并把 zcode 采集游标
（`model_usage_rowid`）重置，使下一次采集重读已镜像行并按天然主键
`(session_id, logical_request_id, attempt_index)` 幂等补齐新字段——重复
采集不重复计数（导入计数中新行为零）。zcode 来源缺这些列时（旧库）
镜像为 NULL、缺口在导入计数的 `model_usage_columns_missing` 与 `probe`
中列名，controller 请求口径相应字段保持未知，绝不补零。ORX 的读取语义
由
[ORX observability contract](https://github.com/aderan/orx/blob/main/docs/observability-contract.md)
定义。本仓的指标与关联分别以 [metrics.md](metrics.md) 和
[correlation.md](correlation.md) 为准。

### v13/v14/v15 的读取边界（G010 生命周期管理）

- Goal 状态始终读 `goals.status` 原文，不从 run 聚合推导；
  `paused`（展示“已暂停”）与 `cancelling`（展示“取消中”）均为
  非终态，占用唯一的未完成 Goal 槽位；Goal 控制状态与 Run/attempt
  执行事实分层呈现，互不改写。
- v15 的 `completion_receipts` 只镜像回执**事实**（digest、身份、
  交付状态、时间），不镜像 `receipt_json` 重放负载——回执是给补交
  方的重放面，本工具只读不重放。本工具不因此获得任何调度、恢复或
  迁移权限（源库永远 `mode=ro` + `query_only`）。

## 来源和平台

| 来源 | 默认位置 | 当前能力与限制 |
|------|------|------|
| ORX | 已登记项目的 `.orx/state.db` | 按显式版本和列名读取；每轮追加快照；可用 `collect --skip-global` 单独采集 |
| ZCode | `~/.zcode/cli/db/db.sqlite` | 会话树、turn 用量、请求/重试、逐请求 token（store v4 起，含 controller 请求口径）、工具调用；`session` 和 `bundle session` 使用此来源；缺 token 列的旧库按缺口列名 |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | 线程、模型上下文、用量及事件；按字节游标增量读取，不补全缺失日志 |
| Cursor | macOS `~/Library/Application Support/Cursor/user/globalStorage/state.vscdb` | composer 与 workspace 映射；来源没有逐消息用量时保持缺口 |

来源路径目前使用实现内的默认位置；`ORX_ANALYTICS_HOME` 只改变分析数据
目录，不改变来源位置。不要把它当成 `CODEX_HOME` 或 agent 来源配置。
并非每个来源都具有相同的报告与关联能力：完整原生会话树报告当前是
ZCode 功能，采集 Codex/Cursor 不意味着 `session` 能接受其 id。

watch 使用 POSIX 进程与信号，面向 macOS/Linux；新发现通知通过 macOS
`osascript` 发送，其他平台仍可查 findings。Cursor 的默认读取路径针对
macOS，Windows 来源和 watcher 尚未纳入验收。

`probe` 反映当前机器和来源是否可读。它以 GAP 报告缺口，退出 0 只代表
探测命令完成；`collect` 在任一已尝试来源读取失败时退出 1，其他来源仍
可采集；`watch` 记录 `last_error` 并在后续轮次继续尝试。
报告中的 NULL/unknown 与未覆盖分母是数据含义，不是安装故障。

## 契约更新与验收

扩大版本支持前应检查 ORX 契约修正，更新 probe/importer 与对应样本测试。
若规范观测来源或指标定义变化，同时检查报告、异常规则和证据包复算，
避免同一份数据出现不同结论。0.4.3 的规范来源优先级为
`native_cli > zcode_db > host_report > output_estimate`，同级取最小观测 id。

CI 验证确定性样本、源码包和独立 wheel 安装；不需要真实来源、不调用模型。
[source-probes.md](source-probes.md) 记录历史机器上的探测事实，不能替代
当前支持声明，也不承诺未来 agent 内部格式永远不变。
