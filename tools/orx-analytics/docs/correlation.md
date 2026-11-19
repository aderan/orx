# 关联规则（冻结）

状态：Phase 1 冻结。ORX attempt 与原生工具会话的关联只有两类：

## 权威（authoritative）

ORX 自己写入的 `attempts.session_ref`（host 路径在 claim/complete/fail 时由
worker 传递；CLI 路径由适配器从工具输出捕获）。这是唯一可写成事实的关联。
读取时原样取出，不解析、不归一。

## 推断（inferred）

历史回填或补全用，必须逐条记录依据与置信度，绝不写成确定关系。可用证据
从强到弱：

| 置信度 | 依据（全部满足才给该级） |
|---|---|
| high (nonce) | 子会话首条用户 part 携带该 attempt 的一次性身份锚 `ORX_ASSIGNMENT=orx-assignment:<uuid>`（ORX v11 派发提示词首部、attempt 创建时铸造）且 `session.directory` == 项目根 且父会话链在项目内。token 一次一个 attempt、不可猜测、不会出现在别的提示词的证据血缘里——命中即无需时间窗/角色交叉校验；同一 token 出现在多个会话 = 同一提示词真实双发，降 ambiguous |
| high (marker) | 子会话首条用户 part 含该 attempt 的唯一标记（如 "attempt 88"）且 `session.directory` == 项目根 且父会话链在项目内（controller→subagent 结构成立）且**会话创建时间落在 attempt 跨度 ± 300s 松弛内**且提示词角色声明与 attempt 角色一致（修订 2026-10-06，见下）。仅适用于无 nonce 的 legacy attempt |
| medium | `session.directory` == 项目根 且会话时间窗覆盖 attempt 跨度（±松弛）且角色/标题语义与 attempt 一致，但无 attempt 唯一标记 |
| low | 仅时间邻近或仅标题相似——**默认不落库**，仅供人工线索输出 |

规则：
- 标题、时间邻近、短编号本身永远不构成确定关系；`(run, revision, task)`
  身份必须全链携带。
- 同一会话匹配多个 attempt（或反之）时降级为 ambiguous，列出候选与冲突
  证据，不择一。
- 推断结果可被后续权威值推翻：权威值出现后推断行标记 superseded，保留历史。
- 每条关联行包含：`basis`（命中规则原文）、`evidence`（命中的 part 片段/
  时间窗数字）、`confidence`、`computed_at`。

## 修订（2026-10-06，R006 复盘）

- **新增 nonce 身份锚档（high (nonce)）**：ORX v11 起派发提示词首部携带
  `ORX_ASSIGNMENT=orx-assignment:<uuid4>`（attempt 行 `nonce` 列同值）。
  token 命中直接 high——一次一 attempt、uuid 不可猜测，过期标记/证据血缘
  误链在 token 路径上结构性不存在。有 nonce 的 attempt 不再走 marker 路
  径（ORX 侧 discovery 同规则）。同一 token 多会话 = 提示词双发，ambiguous。
- **high 增加角色文本否决**：派发提示词首行自我声明角色（"You are an
  ORX worker/verifier…"）。声明存留时，与其他角色 attempt 的标记命中降为
  ambiguous。依据：R006 的 a108 验证会话提示词引用其核验的 worker attempt
  编号（"attempt 107" 证据血缘）且创建时间落在 worker 跨度 + 松弛内——时间
  窗拦不住，只有角色声明能区分。无声明则不否决（ORX 侧 nonce 落地前的
  best-effort 防线）。
- **high 增加时间窗交叉校验**：唯一标记命中之外，还要求会话创建时间落在
  该 attempt 跨度 ± 300s 松弛内（实现常量 `WINDOW_SLACK_MS`）。依据：R006
  实测一个 rev-2 worker 会话首提示携带 rev-1 证据血缘 "attempt 103"（a103
  早已关闭），仅凭标记会误链；而真实 worker 会话创建时间先于 claim 4–11 秒
  （R006 六个 host worker 样本）。时间只作否决证据（veto），绝不单独构成
  关联。
- **无跨度 attempt 的标记命中至多 ambiguous**：未 claim（无 started/ended）
  的 attempt 无法交叉校验创建时间，标记命中只保留为线索。
- 降级行的 `basis` 记录具体原因（跨度外/无跨度/多候选），`evidence` 保留
  命中片段、attempt 跨度、会话创建时间与窗口边界，可复核。

## 项目与会话的映射

- 注册项目以绝对路径为 `project_key`。zcode 会话按 `session.directory`
  精确相等映射；`proj_*` id 与 workspace 哈希仅作辅助校验，不单独决定归属。
- 未匹配任何注册目录的会话属于 `unassigned`，可随后注册项目后重新归属；
  不按标题猜测项目。
- Cursor composer 按 `workspaceId` → `workspaceStorage/<id>/workspace.json`
  的 folder URI 映射；无法解析的列为未归属。
- Codex 会话按 rollout `session_meta.cwd` / `turn_context.cwd` 映射。

## 同编号不并账

`T003` 在 rev1 与 rev3 是两个任务行；跨 revision 的对比只通过
`replan_*` 声明边（v9）进行，没有声明边就保持"未知"，不得用编号相似度
推断重复或浪费。

## controller 会话的发现与归属（G012 T005 冻结，2026-10-17）

controller（宿主）会话不是靠猜出来的——它必须是**锚定的子会话的根祖先**：

- 候选 = 本项目已关联（stored `session_ref` / ORX v11 nonce 推断 high）
  的子会话沿 `session.parent_id` 链向上走到根（任意层级深度；链条断或
  成环即无根，不推断）。自身已关联 attempt 的根会话是 attempt 账本，
  永不算 controller（嵌套 attempt 情形）。
- **目录只记录，不过滤**：`directory_matches_project` 记录符号链接两侧
  解析后是否与项目根一致；子代理继承 controller 窗口的目录，可以是别的
  项目（G010：从 A 窗口控制 B 项目时 18 个 nonce 命中全记在 A 目录下）。
  绝不按目录、标题、最近活动或宽泛时间窗猜测归属。
- **归属唯一证据**：该根祖先全部子会话所锚定的 attempt 同属一个 run 且
  run 均已知。跨多个 run、或任一子 attempt run 未知 → 未归属，给出原因
  （`usage_readmodel.native_session_preview` 的 `controller_for_run`）：
  `unattributed`（跨 run 共享）/ `host_session_unbound`（run 的 zcode
  attempt 均无会话绑定，沿用 ORX 缺量原因词汇）/ `session_not_imported`
  （session_ref 在、zcode 镜像无此会话）/ `no_controller_session` /
  `no_zcode_attempts`。未归属绝不摊分、绝不补零。
- 用量与关联依据同源发布：controller 自身用量为 `model_usage` 请求口径
  （见 metrics.md），`child_evidence` 逐条列出锚（子会话、attempt、
  authority/confidence、nonce），供复核；ORX 已记录口径不受任何影响。
