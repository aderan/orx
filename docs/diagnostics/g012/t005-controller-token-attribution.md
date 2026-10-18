# G012 T005 — controller token 归属补齐（只读核对记录）

日期：2026-10-18。任务：端到端补齐 controller token（session_ref/nonce 及
会话父子关系复用、只读镜像 zcode `model_usage` 请求用量、按唯一证据归属、
Swift 原生预览显示）。本文件记录**脱敏聚合**的只读核对结果；原始会话 id
不全文出现（前缀截断），不包含任何 prompt 内容。

## 方法（全部只读）

- 分析 store 建在临时目录 `/tmp/orx-analytics-g012-verify`（先删除重建；
  未触碰 `~/.orx-analytics`、未触碰任何源库）。
- zcode 源 `~/.zcode/cli/db/db.sqlite` 经 `open_ro`（`mode=ro` +
  `query_only`）读取；ORX 源为本仓 `.orx/state.db`（同样只读快照）。
  采集前后源文件摘要不变，无 `-wal`/`-shm` 残留（合成源测试
  `test_zcode_import_idempotent_and_readonly` 已固化该断言）。
- 采集量：zcode 1,759 sessions / 2,021 turns / 60,371 model_usage 请求 /
  70,544 tool 行（约 8 秒）；ORX 251 attempts；关联结果 stored=151、
  inferred_high=13、ambiguous=2、none=85。

## 每个真实 run 的 controller 判定（脱敏聚合）

| run | controller_for_run | 说明（聚合） |
|-----|--------------------|--------------|
| R001 | host_session_unbound | zcode attempt 均无会话绑定（沿用 ORX 缺量原因词汇） |
| R002 | session_not_imported | 有 session_ref 但 zcode 镜像无此会话 |
| R003 | attributed | 1 个 controller 会话；59 请求；input 8,403,221；processed 8,467,972 |
| R004 | attributed | 1 个；97 请求；input 12,971,997；processed 13,024,411 |
| R005 | attributed | 1 个；281 请求；input 66,837,410；processed 67,112,053 |
| R006 | host_session_unbound | 同 R001 类原因 |
| R007 | unattributed | 与 R008 共享同一 controller 会话（164 请求；input 30,194,466；processed 30,291,026） |
| R008 | unattributed | 同上（同一会话跨两个 run） |
| R009 | no_controller_session | 有关联会话但其根祖先自身已关联 attempt |
| R010 | attributed | 1 个；168 请求；input 30,849,476；processed 30,916,124 |
| R011 | unattributed | 与 R012 共享同一 controller 会话 |
| R012 | unattributed | 当前 G012 run：controller 会话 `sess_8ee5e…`（前缀截断）与 R011 共享——同一 zcode 主会话先后驱动 G011 与 G012 |

R012 的 controller 会话聚合数字（会话自身累计，非单一 run 归属额）：
345 请求；input 105,424,695；output 200,306；cache_read 102,574,144
（input 子集）；processed = input＋output = 105,625,001；settling 0；
错误零占位 0；子会话锚 69 个（69/69 为 nonce 锚），角色 worker+verifier；
目录与项目根一致（符号链接解析后）。归属判定为 unattributed 的原因：
子会话 attempt 分属 ['R011','R012'] 两个 run——按冻结契约不拆分、不摊分；
详情页显示该会话累计量并强制标注"跨多个 run 共享，未归属到单一 run，
绝不按 run 摊分"。

## 结论

- 用户反馈 5（"controller 的 token 没有检测到"）成立且已补齐：controller
  会话在真实库中可发现、其自身请求用量可只读镜像并在详情显示；唯一
  归属证据的 run（R003/R004/R005/R010）直接显示；跨 run 共享（R011/R012、
  R007/R008）如实给出不可唯一归属原因并显示带共享标注的会话累计；
  host_session_unbound / session_not_imported / no_controller_session
  各按其真实原因显示，不补零。
- 全部判定路径（attributed / unattributed / host_session_unbound /
  session_not_imported / no_controller_session）在真实数据上各至少出现
  一次；合成回归覆盖同一路径集 + 缺列/缺字段/去重/迁移。
- 双口径隔离未变：controller 数字只存在于 `native_session_preview`
  口径内（具名会话条目），ORX 已记录口径与读数不受影响，无合并总数。
