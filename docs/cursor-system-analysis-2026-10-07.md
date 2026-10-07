# Cursor 阻塞事故与 sess_7c667fa0 用量审计

取证日期：2026-10-07。代码基准：本次调查开始时的工作树。范围：分析与实施规划；没有修改生产代码、真实 ORX 状态、Cursor 权限或用户记忆。

## 结论

这是系统性问题，首要缺陷是 **CLI 执行结果没有进入 ORX 的结构化交付协议**。Cursor 明确报告检查命令被拒绝，ORX 却把进程 exit 0 当成 worker completed，随后把控制器侧的红测试重新归类成代码失败。重试提示因此持续指向错误原因，同一环境阻塞被连续重跑。

现有交付门禁确实阻止了本次三个红结果通过，但这不足以证明整个交付协议正确：本次通过真实 dispatch 的最小回放证明，同样的 blocked 输出在控制器检查全绿时可以被判 passed。

前一份分析中“a130 出生即死、a131 写测试后轻率交卷、a132 不理解领域语义、Cursor 无本地留痕”等结论被原始证据否定。不得据此调整模型排名。

这次会话跨 G007/R007 发布收尾和 G008/R008 ModelSpec 实现，共 10 个任务、26 个 ORX attempts。ZCode 根会话和子会话记录约 1.450 亿累计处理 token；加入两次 Codex 规划及三次 Cursor 执行，按各适配器口径归一化的处理量约 **1.558 亿**。这不是 1.558 亿未缓存计费 token，也不是一个上下文窗口的长度。

## 证据与复现

原始数据只读查询：

- 项目 `.orx/state.db`：attempts、task_events、verifications、usage_observations。
- `~/.zcode/cli/db/db.sqlite`：session.parent_id、model_usage、turn_usage、message、part、tool_usage。
- `.orx/runs/R008/exec/T003-{131,132,133}.log`：真实 argv 和 Cursor JSON result。
- `.orx/runs/R008/check/T003/`：逐轮 pytest 日志。
- `~/.cursor/projects/Users-flb-Sources-Products-ORX/agent-transcripts/<session_id>/<session_id>.jsonl`：三个 Cursor 会话均存在；包含用户输入、assistant 文本和 tool_use，不能视为完整工具结果或逐请求账单。

派生证据：[evidence.json](diagnostics/cursor-2026-10-07/evidence.json)。可重取：

```bash
.venv/bin/python docs/diagnostics/cursor-2026-10-07/audit.py
```

真实代码路径最小回放：[replay.py](diagnostics/cursor-2026-10-07/replay.py)：

```bash
.venv/bin/python docs/diagnostics/cursor-2026-10-07/replay.py
```

已执行，约 0.15 秒，exit 1，输出：

| worker 明确交付 | CLI 进程 | 控制器检查 | 实际 attempt | 实际 task |
|---|---|---|---|---|
| blocked | exit 0 | 红 | completed，failure_reason=NULL | failed，原因变成 delivery gate red |
| blocked | exit 0 | 绿 | completed，failure_reason=NULL | passed |

回放使用临时项目、现有测试辅助工具和缩减的真实输出形状；只替换模型进程输出和启动探针，不替换 dispatch、门禁、状态机、结果存储或 Cursor 输出解释器。两个场景保持 blocked 输出不变，仅改变控制器检查结果，不调用真实模型。这是诊断用红信号，尚未修复；没有把预期失败用例放进常规测试套件。

已有相关检查：`tests/test_delivery_gate_e2e.py`、`tests/test_verify_check.py`、`tests/test_adapters.py`、`tests/test_model_catalog.py`，**100 passed in 19.12s**。既有测试通过与最小回放失败并存，说明覆盖缺口在 CLI 结果进入交付流程的边界。修复阶段应把这个回放转成常规回归测试。

本次只做诊断和规划，diagnosing-bugs 的生产修复及修复后绿灯阶段留给实施阶段；不宣称已解决缺陷。

## 真实时间线：全部北京时间 UTC+8

前一份分析把数据库 UTC 时刻当成了本地时刻，并把关闭 attempt 的时间当成故障发生时间。

| 时间 | 事实 | 证据 |
|---|---|---|
| 10-06 22:46:40 | 根会话开始，执行 G007 | session、turn_usage |
| 10-07 00:24:50 | 建立 G008/R008 | runs |
| 01:05:18–02:05:00 | a130 实际运行约一小时，114 次模型请求，35 次 Edit、3 次 Write；修改实现，写出 test_model_resolution.py | model_usage、tool_usage、part |
| 01:19–02:01 | a130 多轮排查测试卡顿；后定位参数化语法解析的二次方正则回溯并修复 | Bash/TaskOutput/Edit 记录 |
| 02:04:07 | a130 写出 37,123 字符测试文件；随后已执行测试并看到红测试 | Write、Bash 记录 |
| 02:05:00 | 子会话和控制器都遭遇 GLM 1308 配额错误 | model_usage.error_message |
| 08:28:10 | 用户输入“继续”，控制器恢复 | 根会话用户消息 |
| 08:30:49 | a130 才被记录为 failed；原因误写成 died at spawn / never claimed / no workspace changes | attempts、task_events |
| 08:31–08:32 | a131 报告所有 shell 被拒绝、无代码修改；stdout 内还有 status=blocked JSON；CLI exit 0 | exec log、Cursor transcript |
| 08:34–08:45 | a132 报告 ls 可运行，但 uv/pytest/orx 等被拒绝、无代码修改；CLI exit 0 | exec log、Cursor transcript |
| 08:47–09:07 | a133 在无法自检的情况下仍修改代码和测试，最后报告 shell 被拒绝；控制器看到 IndentationError | exec log、transcript、check log |
| 09:08–09:15 | a134 接手已有实现和测试，修复后出现 117 全绿；后续又有门禁和独立检查 | verifications、ZCode 子会话 |
| 09:21:43 | a135 独立验证通过 | verifications |
| 10:13:42 | R008 done；根会话 10:15:22 收尾 | runs、model_usage |

**a130 的状态污染非常严重**：ORX 已有 claim 事件，真实子会话也有约一小时的实现过程；失败说明仍声称未 claim、未改工作区。开始时一次 file not found 不能代表后续一小时都没有测试文件。

02:05 的真实错误到 08:30 的 ORX 关闭相隔约 **6 小时 26 分钟**。这个延迟来自控制器和 worker 受同一额度影响，以及没有完成恢复的外部执行者；它不是 a130 连续计算了 7 小时 25 分钟。宿主 attempt 的墙钟跨度不能直接代表模型运行时长。

原始提供商错误里的重置时刻是无时区的 `2026-10-07 04:24:23`；后续失败说明自行加了 `Z`。没有证据支持这一 UTC 标注，不能据此宣称“额度在重置前部分释放”。08:47 trivial 探针通过仅证明那个时刻、那个请求可用，不能承诺剩余额度足够执行完整任务。

## 系统缺陷：确认项与待验证项

### P0：CLI 运输成功覆盖了任务交付失败（已复现）

`runtime.CommandResult.ok` 只看 exit_code 和 timed_out。`dispatch._execute_cli_task` 在门禁前依据它写 completed；Cursor 的 result 文本没有被当作任务交付解析。`Capture` 只承载 usage/session/missing reason，没有 delivery。

所以 Cursor 的 `{type:result, subtype:success, is_error:false}` 只是一次响应正常结束，里面的任务结果仍可能 blocked。不能要求自然语言指令让模型“退出非零”来弥补：模型控制的是回复和工具，CLI 进程退出语义由 harness 决定。当前 `[BLOCKED EXIT: exit non-zero]` 指令不是可靠机器协议。

同一缺陷也影响健康学习：`dispatch._record_attempt_health` 在外部门禁前使用 run_result.ok，会把权限阻塞的 exit 0 响应作为成功处理，清除失败 streak / cooldown 等健康字段（手动 override 行除外）。路由由此拿不到本次环境阻塞。

修复必须分清：进程结果、harness 响应结果、worker 交付结果、控制器命令门禁、独立 verifier 判断。这些是不同事实，后者不能覆盖前者。

### P1：控制器 preflight 不能证明 Cursor 内部权限（强证据；历史配置未快照）

原始 argv 有 `--trust`，没有 `--force`。本次检查到的 Cursor 配置是 approvalMode=allowlist，shell allowlist 只有 `Shell(ls)`，sandbox disabled。与 a132“ls 能运行、uv/orx/pytest 被拒绝”的实际症状高度一致。

当前配置不是事故时配置快照，所以“事故时每项权限精确取值”仍不能断言。但三轮终稿和 transcript 已证实执行权限阻塞，不需要借模型能力解释。

`--trust` 只解决工作区信任，不保证 prescribed checks 获得执行授权；sandbox disabled 也不等于审批策略 unrestricted。应按具体任务验证允许执行的命令与写路径。不要把所有 worker 默认改成 --force 作为唯一修复。

官方资料也将 CLI 权限、审批模式及工作区配置分别建模：[Configuration](https://cursor.com/docs/cli/reference/configuration)、[Permissions](https://cursor.com/docs/cli/reference/permissions)、[Output format](https://cursor.com/docs/cli/reference/output-format)。本次以原始日志和本机 CLI 帮助为主，旧网页中关于 print-mode 写入的描述存在版本差异。

### P1：重复失败不改变恢复决策（已由事件序列确认）

三次相同 profile 被重新启动，没有证据显示 uv/orx/pytest 的权限条件改变。第一轮 blocked 原因没有进交付字段；重试提示反而要求修红测试。a131/a132 红测试完全相同，恰好符合“工作区未改”，不能证明同一模型修了两轮仍不收敛。

缺少按 task/profile/阻塞原因/环境版本识别“条件未改变”的恢复门槛。应让权限阻塞在恢复必要条件满足后再试；代码失败的迭代则保留同 session 的修复机会。CLI 与 host 的门禁红处理不同：host 会保留 attempt 让它修，CLI 会关闭后把 task 判失败。

a133 的行为仍有问题：知道无法自检后继续写测试，并留下收集错误；不能用系统缺陷免除这个违约。但本次数据不能区分模型能力、权限失败和错误重试反馈的各自影响。

### P1：host 故障没有自动形成可调度结果（已确认事实；恢复能力需独立设计）

控制器与 worker 使用同一提供商配额，同一时刻失效，失败通知和重路由因此依赖用户回来。当前可选 watchdog 仅观察、不恢复。它即使发现停滞，也不等于存在可执行的恢复者。

应先固化故障发现和准确状态写入，才决定由哪个具有不同故障域的宿主负责继续调度。worker 超时或失联只能触发核验，不能直接启动第二个写入者；需确认原会话结束后才能 fail/retry。不要以自动恢复之名绕过既有单写入者边界。

### P1：错误结论已写入持久记忆（直接读取确认）

`~/.zcode/cli/memories/projects/orx-f6adf3259bf71590/memory/orx-quota-reroute-lessons.md` 已存储“启动即死”“每轮写完测试矩阵”“不理解验收语义”“只有 ORX 一条观测腿”等误归因。后续分析可能把它重新当证据，形成错误强化。

需要用原始执行记录附加勘误并替换这些结论；历史数据库事件应保留原值，通过明确的审计/纠正事实关联原记录，不能静默改掉原始历史。本次没有更改真实数据库或用户记忆。

### P2：长上下文与卡顿诊断放大成本（量化确认；具体注入来源尚未完全确认）

11 个 host worker 的首个模型输入中位数 **72,245**；10 个 host verifier 首个输入中位数 **6,242.5**，差约 **11.6 倍**。worker 使用广泛工具面排除少量工具，verifier 使用 Read/Bash 白名单且关闭 AGENTS.md 注入。独立子会话不自动意味着轻上下文。

以上支持“worker 固定上下文较重”，但没有保留完整历史请求体，不能把全部差额归因于工具定义或父会话历史。现有子代理能力实测反而说明父会话内容不可见。应做受控工具面/指令注入对照，避免把上下文体量误称成每轮重复读仓库。

a130 累计 2,573.9 万 token。内部约 42 分钟陷于卡顿排查，包括反复启动/中止同一测试、长等待，以及尝试不存在的 timeout/pytest 参数；到 02:01 才修正参数化 parser 的二次方回溯。该 parser 修复已在当前代码中，不能把它列成尚待修复的现存 bug；需补性能回归及诊断耗时边界。

根会话从首轮约 8.3 万输入增长到最后最高 28.8 万；恢复轮没有把 G007 的控制历史从 G008 调度上下文中移开。Controller 本应主要持有决策、状态和证据引用，这里却贡献总处理量约 19.4%。

## Token 审计与“是否符合预期”

### 不重复计数的归属

ZCode 以 session.parent_id 递归找后代，逐 session 聚合 model_usage；没有再把 turn_usage、message.tokens 加一遍。933 条请求记录包含 3 条 error、1 次标题生成；error 未报告 token 的零值不代表失败请求必然免费。没有发现 model_usage 里的重试行重复（根会话与 a130 的 logical_request_id 均唯一、attempt_index=0）。

22 个子会话中，21 个对应 ORX host attempts，另一个是可用性 trivial 探针。两次 Codex planner 和三个 Cursor worker 从 ORX CLI observations 单独取数，未包含在 ZCode 子树中。

| 范围 | 请求记录 | input_tokens | output_tokens | cache read | 处理量口径 |
|---|---:|---:|---:|---:|---:|
| ZCode 根会话 | 164 | 30,194,466 | 96,560 | 29,751,488 | input + output = 30,291,026 |
| ZCode 22 子会话 | 769 | 114,101,763 | 632,259 | 112,354,112 | input + output = 114,734,022 |
| 两次 Codex 规划 | 未在此统计逐请求数 | 703,765 | 17,734 | 557,440 | input + output = 721,499 |
| 三次 Cursor 执行 | 原始 JSON 是执行累计 | 1,065,150 | 109,108 | 8,915,328 | input + cache read + output = 10,089,586 |

ZCode 的 cache read 是 input 的子集，raw_usage 和 computed_total 可交叉验证。Codex 使用相同包含缓存的输入口径。Cursor CLI 按原样保留独立输入与缓存桶（本次 cacheWriteTokens 全为 0）；其 cache read 远大于 input，不能按 ZCode 方式扣减，也不能直接与 ZCode input 列相加比较。

Cursor 官方支持对 CLI 桶语义的解释见 [CLI usage 与账单口径说明](https://forum.cursor.com/t/discrepancies-between-cursor-cli-usage-and-billing/164471/7)。该示例针对 Anthropic；本次模型是 grok，跨提供商总量应视为按记录字段归一化的处理量估算，最终计费仍需账单对账。

归一化总处理量约 **155,836,133**。其中缓存读取约 **151,578,368**，其余输入约 **3,402,104**，输出约 **855,661**。不得把缓存读取当作免费，也不能把剩余输入加输出当作账单；不同模型、缓存、套餐窗口与服务商计费权重未在本次确定。reasoning_tokens=0 只是报告字段，不能解释成模型没有思考或 max 无额外消耗。

### Cursor 三轮

| attempt | 外部进程时长 | input bucket | output | cache read | 归一化处理量 | 交付事实 |
|---|---:|---:|---:|---:|---:|---|
| 131 | 81.5 秒 | 46,730 | 2,341 | 166,272 | 215,343 | blocked，未修改代码 |
| 132 | 699.7 秒 | 419,462 | 33,804 | 2,770,176 | 3,223,442 | blocked，未修改代码 |
| 133 | 1,206.1 秒 | 598,958 | 72,963 | 5,978,880 | 6,650,801 | blocked，修改但无法自检 |

三轮合计 **33.1 分钟、约 1,009 万处理 token**，约占整条会话处理量的 **6.5%**。a131/a132 的无代码交付累计约 344 万处理 token，第二轮阻塞前探索明显过长。a133 有代码改动，不能把整轮 token 都算成“零产出浪费”。

cache read 增长首先反映各轮累计模型调用与历史前缀重用，不能直接证明“每轮全量重建缓存”或读取了更多不同仓库文件。fresh session 确实存在，但它的额外冷启动成本与总 cache read 是不同指标。

T003 的 a130–a135 处理量约 **4,401 万**（未分摊控制器开销），其中初次 worker a130 约 2,574 万、修复 worker a134 约 742 万、Cursor 三轮约 1,009 万、verifier 约 77 万。只关注 Cursor 会漏掉更大的初始诊断与控制上下文成本。

### 预期判断

- **工作体量**：两个 Goal、十个任务，不能按“一次 resolver 修复”评价全会话，也不能用 a134 的 7 分钟抹掉 a130 已完成的大量实现。产物从发布收尾走到 887 测试的 ModelSpec 实现，真实工作量存在。
- **相对本机历史**：该根会话平均请求输入约 18.4 万，并非异常尖峰。其他 ORX 根会话也出现平均 13–26 万、最高 30–39 万的记录。这支持“既有运行方式存在系统性上下文膨胀”，而非本会话独有异常。历史样本任务不同，不能据此计算严格浪费比例。
- **相对设计目标**：不符合薄 controller、环境阻塞快速退出、同 session 修复、条件改变后再重试的预期。耗时和重复执行有明确、可避免的结构性成本。缺少按任务类别建立的 token 预算，不能诚实给出“超预算 X 倍”。
- **角色分配**：host worker 总处理量约 1.125 亿；host verifier 约 222 万。廉价 verifier 分层工作正常，优化重点应是 worker 上下文、controller 历史和恢复机制，而不是取消独立验收。
- **ORX 自身用量覆盖**：26 个 attempts 只有 5 个有 observations，覆盖率 **19.2%**；21 个 host attempts 既没有 usage observation，也没有 missing reason，属于 unassessed。根控制器和探针成本也不在 ORX attempt 汇总中。ORX 的 usage 页面单独不足以评价真实总成本。
- **时间**：会话墙钟跨度约 11 小时 29 分钟，约 6 小时 23 分钟是故障后等待用户恢复的间隔；不能把这些小时都乘成模型持续调用费用。已记录的 token 总和没有时间外推。

## 实施规划：先修状态正确性，再修恢复，再压成本

以下为待实施计划，不代表已经启动开发。实施时将当前阶段放入根 IMPLEMENTATION_PLAN.md，完成后移除本次临时计划；现有历史内容不删除。

## Stage 1: 统一 CLI 任务交付解释（P0）
**Goal**: 保留进程、harness、worker delivery、gate、verifier 的独立事实，让 CLI/host 使用同一交付验证语义。
**Success Criteria**: blocked/failed 不被 exit 0 覆盖；检查全绿也不能使 blocked 成功；缺失或歧义 delivery 显式 protocol_error；usage/session 仍照常记录；agent verifier 仍独立。
**Tests**: 将 replay 的绿/红两个场景转成真正回归测试；添加显式 passed、failed、blocked、缺交付、畸形交付、非零退出、CLI envelope error、日志截断而完整捕获仍在等矩阵；比对 host 等价输出与 retry 分类。
**Status**: Not Started

建议 seam：adapter 输出解释提供运输事实；worker 提示词给出统一、可机器读取的终结交付；dispatch 调用现有 delivery validator，不以自由文字包含 blocked 作为成功协议。a131 的 fenced JSON 可做兼容输入；a132/a133 只有自然语言 blocker 时应给明确协议诊断，并保留原文供 controller 判定，不能偷偷判 passed。历史 model/effort 自报 marker 与交付状态分开处理。

## Stage 2: 执行权限预检与有限恢复（P1）
**Goal**: 在该 worker 的实际权限边界内确认 prescribed checks 可执行，把权限阻塞和代码失败分开恢复。
**Success Criteria**: controller PATH/denylist 通过不被宣传为 worker 权限通过；权限配置来源和版本可追溯；同一未解除 blocker 不反复派发；host 与 CLI 的同 session 修复语义明确一致。
**Tests**: print + allowlist(ls) 对 uv/orx/git 的阻塞场景；明确允许时通过；trust 与审批模式分别覆盖；两次相同权限阻塞禁止第三次原样启动，权限版本变化后允许重试；代码修复在同 attempt/session 内收敛；能力不足时不强制升级权限。
**Status**: Not Started

沿用项目 profiles、静态 capabilities 与动态执行事实的边界。先做确定性授权可行性检查；仅当 harness 没有可靠本地接口时才选最小模型工具探针，不为每个任务额外烧一轮完整推理。记录 workspace 改动与 check 执行事实，阻塞后不推进计划为 Done。CLI resume 需验证身份、原进程结束、预算和单写入者，再利用已有 --resume；不能无条件恢复旧会话。

## Stage 3: 宿主故障事实、恢复与勘误（P1）
**Goal**: 及时观察 host 子会话的结束/错误，准确关联 attempt，让控制器自身失效也能被独立观察；修正本事故的错误知识。
**Success Criteria**: a130 类事故准确记录“运行中断且有未验收改动”；错误发现与 attempt 关闭有独立时间；原始错误代码/无时区 reset 都保留；没有重复 writer；勘误不覆盖原始事件。
**Tests**: 启动前失败/运行一小时后失败分别回放；worker 与 controller 同时配额耗尽；旧会话失联但未确认死亡时不 reroute；恢复者关闭原 attempt 后只派一个 writer；无时区 reset 不任意附 Z；watchdog 仅观察与有授权恢复者两种模式分清；记忆勘误必须引用原始记录。
**Status**: Not Started

先实现“故障被准确看见和记录”，再增加不同故障域的恢复执行者。是否推进后台恢复是新的实现范围选择，不能把现有 watchdog 宣传成已具备该能力。持久记忆中的模型能力结论应撤回，改为权限阻塞和交付解析缺口的实证。

## Stage 4: 成本计量、上下文减负与预算校准（P2）
**Goal**: 按 Goal/run/task/attempt/session/role 展示真实覆盖率与统一 token 口径；控制长上下文、诊断卡顿和无进展重试。
**Success Criteria**: 本次 26 attempts 的 host 记录都可关联观测或明确 missing；根控制器和探针另列，不混入 task；缓存口径显式、无 double count；在同一代表性任务上减少上下文而验收不退化。
**Tests**: ZCode cache 包含于 input、Cursor cache 独立桶、部分字段/失败零值/重复 observation/子树与 attempt 重叠；worker 工具白名单与现定义受控 A/B；controller 使用短状态摘要与完整历史 A/B；parser 长无括号输入线性性能回归；无进展诊断耗时/请求数边界触发升级而非重复等待。
**Status**: Not Started

先只读接入并复用观测能力，再依据多个代表性任务的 P50/P90 设置预算。预算应分墙钟等待、模型请求数、累计处理量、未缓存输入、输出和阶段成果；达到软阈值要求解释/缩小范围，硬阈值保留交付与恢复事实后停止新请求。不要把预算简单实现成“三次 check 就杀进程”。验收门禁重复执行也应按 start/self-check/delivery/verifier 标记用途，再依据产物版本判断是否可复用，不能为节省 token 取消独立验证。

## 开发顺序与完成定义

顺序是 Stage 1 → Stage 2 → Stage 3 → Stage 4。首个可验收切片只修 CLI delivery seam 与健康学习输入，暂不同时改路由算法、后台恢复和模型档位。每阶段先写行为回归，再实现、跑相关及全量检查，按阶段增量提交；连续三次修复失败即停下记录并重评方案。

最重要的完成信号不是“测试又全绿了”，而是：blocked 能成为准确的交付事实；同一未解除 blocker 不再烧第二、第三轮；宿主死亡及时可见且不重复派 writer；成本统计能解释所有会话归属；模型能力判断有控制了权限和上下文的对照证据。
