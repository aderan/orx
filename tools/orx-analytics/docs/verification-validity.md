# 验收有效性投影（tasks.get 返回字段）

`tasks.get` 保留任务全部历史验收原始行（`passed` 原值、行身份不变），并新增两类字段区分 **历史验收** 与 **当前有效验收**。实现见 `src/orxanalytics/verification_validity.py`，规则与 ORX `Store.verifications_current`（ORX 仓 `src/orx/state.py`）一致：同一 snapshot、revision、task 内取最新 Worker attempt 为边界，取最新 `verification_context` 任务的 `verification_after` 为水位；绑定旧 Worker 的行、不高于水位的行、现代交付下的无绑定（`attempt_id` NULL）旧行均为历史。

## 快照口径（重要）

一切判断基于**最近一次采集到的 ORX 快照**（`verification_window.basis == "snapshot"`，附 `snapshot_id` / `taken_at`）。Analytics 不重新读取实时文件、不重算指纹、不修改 ORX 库，也不承诺采集之后文件未变。文件是否变化以 ORX 本体（`orx verify` / `status --json`）为准。

## 逐行字段（`verifications[]` 每行新增）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `current` | `true` / `false` / `null` | 该行是否属于当前有效窗口；`null` 表示依据不足（未知），不是 false |
| `validity_reason` | `string \| null` | 非 current 时的原因；`current: true` 时为 `null` |

`validity_reason` 取值（Swift 端按前缀/关键词展示，不应逐字匹配整句）：

- 历史（`current: false`）
  - `history: bound to an older worker attempt` — Worker 重试后前一轮的行
  - `history: at or below the verification-context watermark (verification_after=N)` — 产物/依赖变化后水位推进，旧行退出窗口但保留
  - `history: unbound legacy row a modern delivery cannot inherit` — 无绑定旧行不被现代交付继承
- 未知（`current: null`，依据不足，不得显示为已确认有效）
  - `unknown: verification context missing`
  - `unknown: latest verification context invalid` — 最新 context 损坏（JSON/版本/字段非法），**不回退**到更早的好 context
  - `unknown: context does not match the current worker`
  - `unknown: context inputs are not reusable`

## 任务窗口摘要（`verification_window`）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `basis` | `"snapshot"` | 固定为快照口径 |
| `snapshot_id` / `taken_at` | int / ISO string | 依据的快照身份与采集时间 |
| `note` | string | “基于已采集 ORX 快照投影，非实时文件保证”的说明 |
| `context_state` | string | `valid` \| `missing` \| `invalid` \| `worker_mismatch` \| `not_reusable` |
| `context_event_id` | int \| null | 最新 verification_context 任务事件行 id |
| `worker_attempt_id` | int \| null | 当前窗口的 Worker attempt 边界 |
| `verification_after` | int \| null | 当前水位；仅 `context_state == "valid"` 时非 null |
| `counts` | object | `current` / `current_passed` / `historical` / `unknown` |

## 展示约定

- 历史 first-pass 等既有指标维持历史口径，**不得**用历史 `passed` 计数代替 `counts.current`。
- `current: null` 显示为未知（与既有未知口径一致），不得显示为通过或失败。
- 隔离：窗口按（项目、Run、revision、task）计算，同号任务、跨项目、跨修订互不串读。
- 旧引擎读取新响应：新增字段可直接忽略；解码失败时按未知处理。

## 回归覆盖

`tests/test_verification_validity.py`：同交付补验保持通过、水位推进后旧行保留但退出窗口、Worker 重试排除前轮、无绑定旧行、缺 context / 损坏 context（含最新坏不回退）、Worker 不匹配、不可复用、跨项目/修订隔离、历史计数口径，以及用真实 ORX Store 生成的临时库交叉核对窗口（`verifications_current` 逐段一致、历史 append-only、源库只读 SHA-256 不变）。
