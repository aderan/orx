# Quota preflight & exhaustion backoff — research report (2026-10-05)

背景：Codex Plus 5x 额度耗尽。两个问题：(1) 耗尽错误能否识别并退避到下一个 agent；(2) 能否提前知道 codex / cursor / zcode 三家的用量。取数机制参考 [orca](https://github.com/stablyai/orca)（`src/main/rate-limits/`），全部在本机实测验证过。

## TL;DR

| 问题 | 答案 | 状态 |
|---|---|---|
| 耗尽错误可识别？ | 是。codex 退出码 1 + 消息含 "usage limit"，现有 `classify_failure` 模式直接命中 → `quota_exhausted` | 已实测 |
| 识别后退避？ | 半成品。health 已把 profile 置 `exhausted`，下一次路由自动跳到下一档；但失败的任务当次不重试，且 `exhausted` 永不自动恢复（需手动 `orx resource clear`） | 缺口明确 |
| 提前知道用量？ | 三家全部可以，各有已验证的本地凭据 + HTTP 端点 | 已实测 |

## 一、错误驱动退避（探索 1）

### 一手证据（2026-10-05 实测，Codex Plus 5h 窗口耗尽）

`codex exec --json` 的行为：

- **退出码 = 1**（非 0，`CommandResult.ok` 为 False）
- stdout JSONL 末尾两条事件都带同样消息：

```json
{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro (…), visit …/usage to purchase more credits or try again at Oct 6th, 2026 2:16 AM."}
{"type":"turn.failed","error":{"message":"同上"}}
```

### ORX 现有链路（全部已存在）

1. `adapters/base.py:230` — `("usage limit", "quota_exhausted")` 模式命中（消息小写后包含 "usage limit"；消息里的 ’ 是 Unicode 撇号，不影响子串匹配）。
2. `dispatch.py:1620-1622`（worker）/ `1028-1030`（planner）/ `3145-3147`（verifier）— `record_attempt_outcome(ok=False, error_kind=quota_exhausted)`。
3. `health.py:63-66` — profile 状态翻为 `exhausted`（可带 `quota_reset_at`，但见下）。
4. `routing.py:122-123` — `NON_ROUTABLE_RESOURCE_STATUSES` 含 `exhausted`，下一次 `route()` 跳过该 profile，选下一档并标 `fallback_used=true`。

即：**跨重试的退避已经工作**。worker ladder（`~/.config/orx/config.toml`：`zcode-worker → cursor-strong`；planner 档位 codex 在前）中 codex 挂掉后，下次 `task retry` / 下一任务自然落到 cursor。

### 三个缺口

1. **无当次退避**：`dispatch.py:1632-1637` quota 类失败直接 FAILED；planner 路径 `1039-1042` 直接 raise。可在失败分支按 `error_kind in (quota_exhausted, rate_limited)` 重入路由换下一档（worker 循环在 `run_slice` `1366-1400`）。
2. **`exhausted` 永不自动恢复**：不像 `cooldown` 有 `cooldown_active` 时间门（`routing.py:124-130`），`exhausted` 只能手动 `orx resource clear`。重置时间其实就在错误消息里（"try again at Oct 6th, 2026 2:16 AM"），且预检端点给的是精确 epoch（见下）。修法：解析重置时间 → 喂 `quota_reset_at` → routing 加一个过期门（镜像 cooldown）。
3. **`quota_reset_at` 管道存在但从未被喂**：`record_attempt_outcome` 接受该参数，三个调用点都没传；`classify_failure` 只返回 kind 不返回时间。

## 二、用量预检（探索 2）— 三家实测通道

机制均来自 orca `src/main/rate-limits/`，已在本机用真实账号逐一验证。共同注意：均为非官方接口，凭据来自本地文件/钥匙串，读即可、**绝不能写日志**。

### codex（实测 ✅）

```
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <~/.codex/auth.json 的 tokens.access_token>
chatgpt-account-id: <同文件 tokens.account_id>   # 有则带
User-Agent: codex-cli
```

响应（当日实测，关键字段）：

```json
{"plan_type": "plus",
 "rate_limit": {"allowed": false, "limit_reached": true,
   "primary_window":  {"used_percent": 100, "limit_window_seconds": 18000,  "reset_at": 1791224196},
   "secondary_window":{"used_percent": 44,  "limit_window_seconds": 604800, "reset_at": 1791609764}},
 "model_usage": {"gpt-6-astra": {"available": false, "available_at": "…"}},
 "credits": {"has_credits": false, "balance": "0"}}
```

门控判据：`rate_limit.allowed == false || limit_reached == true` → 视为 exhausted，`primary_window.reset_at` 即 `quota_reset_at`；还白送逐模型可用性。备选通道：`codex app-server` JSON-RPC（`initialize` → `account/rateLimits/read`，orca `codex-rpc-rate-limit-probe.ts`），无需碰 auth.json 但要拉起子进程，HTTP 路更简单。

### cursor（实测 ✅）

1. 令牌：macOS 钥匙串 `security find-generic-password -s cursor-access-token -a cursor-user -w`（cursor-agent 2026.06+ 不再落 `~/.cursor/auth.json`）。拿到的是裸 JWT。
2. Cookie：解码 JWT payload 取 `sub` claim（形如 `github|user_…`），拼
   `Cookie: WorkosCursorSessionToken=urlencode(sub)%3A%3Aurlencode(jwt)`，并带 `Origin/Referer: https://cursor.com(/dashboard)`（CSRF 要求，缺了必 403/401）。
3. `GET https://cursor.com/api/usage-summary`（旧版 `/api/usage`）。响应（当日实测）：

```json
{"membershipType": "pro", "isUnlimited": false,
 "billingCycleStart": "2026-09-20…", "billingCycleEnd": "2026-10-20…",
 "individualUsage": {"plan": {"used": 2000, "limit": 2000, "remaining": 0,
    "breakdown": {"included": 2000, "bonus": 5459, "total": 7459},
    "totalPercentUsed": 15.07, "autoPercentUsed": 15.66, "apiPercentUsed": 9.2},
   "onDemand": {"enabled": false}}}
```

门控判据：`individualUsage.plan` 的 `remaining`/`breakdown.total`（included 用尽后吃 bonus，本机当前 included 2000/2000 已用完、总占比 15%）；注意 `used==limit` 不代表断粮，要看 breakdown 与百分比。

### zcode / GLM coding plan（实测 ✅）

```
凭据：~/.zcode/v2/config.json → provider["builtin:bigmodel-coding-plan"].options.{apiKey, baseURL}
GET {baseURL origin}/api/monitor/usage/quota/limit
Authorization: <apiKey>          # 裸 key，无 Bearer 前缀
```

响应（当日实测）：

```json
{"code":200,"success":true,"data":{
  "limits":[
    {"type":"TOKENS_LIMIT","unit":3,"number":5,"percentage":20,"nextResetTime":1791224117032},
    {"type":"TOKENS_LIMIT","unit":6,"number":1,"percentage":67,"nextResetTime":1791629874999},
    {"type":"TIME_LIMIT","unit":5,"number":1,"usage":4000,"currentValue":420,"remaining":3580,
     "percentage":10,"nextResetTime":1791425186998,
     "usageDetails":[{"modelCode":"search-prime","usage":413},…]}],
  "level":"max"}}
```

窗口语义（orca `zcode-usage-fetcher.ts` 的映射，`unit` 是枚举）：`unit=3,number=5` → 5 小时窗口；`unit=6,number=1` → 周窗口（10080 分钟）；`TIME_LIMIT unit=5` → 月度资源池（search/web-reader 等，4000 配额）。`nextResetTime` 为毫秒 epoch。**注意 zcode 是 host harness，`record_attempt_outcome` 只在 CLI 路径被调 —— GLM 配额对 ORX 完全不可见的现状，靠这条端点补上。**

`~/.zcode/v2/coding-plan-cache.json` 只有权益状态（`bigmodel-coding-plan: available`），无数值，不能当预检源。

## 三、落地记录（G005，2026-10-06 实施完成）

P1 + P2 全部落地，617 测试绿（新增 tests/test_quota.py 20 例）：

- **重置时间解析**：`adapters/base.py` 新增 `parse_quota_reset` / `quota_reset_from`，识别 "try again at / resets at …"（含 codex 的序数词日期 "Oct 6th, 2026 2:16 AM"，本地时区锚定）；`dispatch._record_attempt_health` 成为三个 CLI 启动点（planner/worker/verifier）共用的 health 记录缝，quota 失败时把解析出的重置点喂进 `quota_reset_at`（管道原本就存在但从未被喂）。
- **过期自动恢复**：`health.quota_exhaustion_active` + routing 门（镜像 cooldown）：已知重置点已过 → 候选按 `unknown`（"应可用、未验证"）参与路由，下一个 outcome 重新学习真相；未知重置点保持手动 `orx resource clear` 语义。成功执行会清掉 `quota_reset_at`。
- **当次换档**：`run_slice` 对 `quota_exhausted` / `rate_limited` 失败立即重入路由（`dispatch._reroute_resource_failure`，单跳：仅当选中不同 profile），FAILED→RUNNABLE 复用 `task retry` 的状态机事件；输出带 `fallback_from`。非资源类失败（process_failure 等）维持 FAILED，不换档。
- **预检模块**：`src/orx/quota.py` 三 fetcher（codex wham/usage、cursor usage-summary+钥匙串 JWT Cookie、GLM quota/limit），60s TTL 缓存、传输可注入、任何失败降级为 `unknown`（对 state 零影响），凭据只用于请求、绝不进日志/快照。
- **接线**：`orx run` / `orx plan` / `orx verify` 入口跑 `quota.refresh(project)`——provider 报 limit_reached 时把对应 CLI profile 写成 exhausted+reset（走 `resource_learn`，operator override 永远赢），恢复健康时把自动学习的 exhausted 行翻回 available；新命令 `orx quota [--force]` 展示实时快照与 profile 映射。`ORX_QUOTA_PREFLIGHT=0` 全关（测试默认关，conftest 统一设置）。
- **实测**（2026-10-06 上午）：`orx quota` 一次拉三家成功；codex 5h 窗口已重置（31%）、GLM 周窗口 82%。

已知边界（有意为之）：
- planner / verifier 的 quota 失败不做当次换档（任务/裁决的语义不同于 worker，交给下一次路由自然换档）。
- cursor 的 `used >= breakdown.total` 判耗尽是启发式；GLM 无显式 reached 标志，用 ≥99.5% 阈值。
- 预检端点均为非官方接口，形状变了会安静降级为 unknown，不会误门控。

## 当日余量快照（2026-10-05 晚）

- codex：5h 窗口 100%（02:16 重置），周窗口 44%，credits 0。
- cursor：included 2000/2000 用尽，bonus 5459 可用，总占比 15%，账期至 10-20。
- GLM（zcode）：5h 窗口 20%，**周窗口 67%**，月度资源 10%。
