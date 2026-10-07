# 用小任务提前验收退避、切换与真实执行能力

## 判断

可以，而且应该在大型开发任务前验证。把测试拆成三层：配置检查、注入故障的完整调度回放、真实微任务。小任务要走拟投产的 profile、模型/effort、权限和交付路径；单独让模型回复 ok 只能证明一次模型请求成功，不能证明能读写、执行检查或正确切换。

这是分析和待实施方案。本次没有运行真实模型任务、改变权限、制造真实额度耗尽或修改生产代码。

## 本次实际核验

执行：

```bash
.venv/bin/python -m pytest -q tests/test_health.py tests/test_quota.py tests/test_routing.py tests/test_probes.py tests/test_host_progress_e2e.py
```

结果：**88 passed in 9.05s**。覆盖 cooldown 增长与封顶、额度 reset 前后路由、override 保护、CLI 限流后的下一档执行、以及宿主失联/迟到交付边界。测试使用临时状态、假 CLI 和模拟响应，不依赖真实付费模型。

现有 `agent probe` 主要检查 binary/version/help/auth/models。现有 `test_cursor_force_flag_from_profile` 检查参数组装。它们都不能证明真实 Cursor 内部能执行 uv/orx/pytest；这正是本次事故漏过的边界。

另一个已复现缺口：`dispatch._reroute_resource_failure` 选中下一档之后无条件走 `_execute_cli_task`。配置下一档为 ZCode host 时，实际结果是 `ORXError: no adapter for harness 'zcode'`，没有产生 host_required 指派。既有 fallback 用例主要使用 CLI/shell 替身，未覆盖这个 driver 边界。

可重复的诊断入口（当前 exit 1 是已知缺陷）：

```bash
.venv/bin/python docs/diagnostics/cursor-2026-10-07/replay_cli_to_host.py
.venv/bin/python docs/diagnostics/cursor-2026-10-07/replay.py
```

第一个验证 CLI→host 切换，第二个验证 exit 0 + blocked 的交付语义。两者调用真实 dispatch/状态机/存储/门禁，只用临时项目和确定性进程输出；不修改真实运行状态。应在修复前转成常规回归测试。本次小场景证明了“基础规则测试都绿”仍可能存在组合路径缺陷。

## 第一层：免费配置检查

核验 binary/版本、profile driver、模型解析、实际 argv、权限来源、命令 allow/deny、workspace 与模型支持。退避窗口的时区和来源必须明确；额度请求未知不能报告充足。

对于 Cursor，分别核验 trust、force/yolo、审批模式与 sandbox。记录最终有效配置，不能只检查 profile 字段；也不能通过 shell 在控制器侧执行成功来代替 Cursor 内部执行成功。

检查失败先给可操作诊断，不启动完整模型任务。纯配置检查证明“没有已知配置矛盾”，不宣称实际工具调用一定可行。

## 第二层：无模型故障注入

使用现有 pytest、临时 ORX 项目和 PATH 替身，重放真实 CLI 输出形状；推进虚拟时钟而非真的等待配额恢复。注入点限于进程/提供商响应，保留真实解析、健康学习、路由、状态机及交付门禁，避免把被测逻辑一起 mock 掉。

| 场景 | 注入内容 | 必须看到的行为 |
|---|---|---|
| 限流 | A 返回 rate limit | A 进入 cooldown，按配置切 B；单次切换有界 |
| 额度耗尽 | A 返回 usage limit + reset | reset 前不再选 A，过期后允许重新尝试，不直接宣布有足够额度 |
| 认证失效 | 登录失效响应 | 按现有路由跳过不可用 A 或报告无候选，不循环尝试登录 |
| 权限阻塞 | CLI exit 0，交付 blocked | 保留环境阻塞，绿门禁不能覆盖 blocked |
| 未解除的权限阻塞 | 同一 profile/权限条件再次出现同 blocker | 不反复派发；需要改变配置或选择有执行能力的 profile |
| 代码检查红 | worker 已能执行测试，但交付未绿 | 保留同会话修复机会；不能把代码红当资源耗尽 |
| 不同执行方式 | CLI→CLI、CLI→host、host→CLI、host→host | 每条都走正确启动/宿主指派路径；host 故障先经显式结束事实，不假装是 CLI 错误 |
| 所有候选失效 | 梯子上每个 profile 都不可用 | 返回有限、可解释的结果，不无穷循环或偷换模型 |
| 旧结果迟到 | A 已结束并切 B 后提交 A 的交付 | 拒绝旧 attempt，不能覆盖 B 的状态 |
| 未确认死亡 | 心跳过期，但 A 可能仍在写 | 只观察/核验，不创建第二个 writer |
| 手动 override | 候选被人工置 unavailable | 自动学习/探针不擅自解除 override；clear 与 available 的区别可见 |
| 共同故障域 | worker 与 controller 同时失去配额 | 独立观察者记录故障；有恢复者才继续调度，无恢复者则如实停留 |

GLM 1308 需重放中文原始消息及无时区 reset，不能只使用英语 Codex 限额消息。这是需要补充的验收场景，本次未宣称已经通过。

## 第三层：真实微任务

准备一个隔离的最小仓库，按项目现有工具链放好测试和一个错误函数。通过真实 ORX 指派让拟投产 worker 完成：

1. 读取指定文件，返回其中的随机 nonce，证明真实访问到了正确 workspace。
2. 按任务范围修正一行函数，例如把 add 的减法改为加法；预置测试已有明确红信号。
3. 在 agent 内部运行规定的测试命令和 git diff --check，保留工具实际结果。
4. 通过正式交付协议结束，再由控制器运行独立门禁，并核验实际改动、session/attempt 身份与用量。

每个 worker profile 的一个微任务即可同时检查 read/write/shell/检查/交付。host profiles 必须走真实原生子代理和回传路径；CLI profiles 必须走实际 adapter 启动。verifier 单独验证只读审查和两行 verdict；planner 如需认证，单独生成最小有效 Plan IR。一个角色通过不能给其余角色背书。

验收不能只看 agent 文本说 passed。至少要有：文件确实变化且在范围内、nonce 对应本次任务、检查真正执行且 exit 0、worker delivery 被解析、独立门禁通过、session/attempt 绑定正确。缺少某类证据应标记未验证。

建议每个真实微任务一次启动，外部墙钟上限 60–90 秒；第一次发现权限阻塞立即终止本轮，不尝试多个变体或派子代理绕行。这个时间是待校准目标，不能保证当前平台每次满足。先测试将用于正式任务的权限配置，不额外启用更宽权限来使探针通过；变更权限须单独记录。

## 退避与切换的组合验收

将第二层与第三层组合成一条小故事：A 的资源失败由故障注入触发，ORX 必须把真实微任务指派到 B；B 在实际权限边界完成任务并交付。这样既不烧光真实额度，也能证明切换后的执行者可工作。

先独立通过 A/B 的微任务，再做组合测试，能区分策略失败与 B 自身不可执行。正向恢复则推进模拟时钟跨过 reset，确认路由重新考虑 A；真实恢复探针只验证当前请求可成功，不能保证能完成数十分钟任务。

## 探针结果的适用范围与触发

结果绑定 CLI 版本、profile driver/model/effort、权限配置、workspace、测试工具链和观测时间。鉴权、CLI、模型或权限配置变化后失效；实际失败优先于旧成功。workspace 不同可能有不同项目权限和 hook，不能只按 profile 名共享结论。

在首次启用 profile、CLI 升级、权限/模型/工具链变化，或阻塞恢复后执行微任务；稳定配置用短期证据复用，不在每次任务前跑全部模型。复用期限应基于观测校准，不把固定 TTL 当容量保证。

“probe passed”与“额度充足”分开保存。一个 ok 或三行函数成功证明基本执行链路，不能证明复杂领域理解、长任务剩余额度、隐藏计费、下一次请求也成功、后台恢复者存在，或不同模型独立拥有额度。共享提供商/账号额度的 profiles 应关联故障域，不能把换模型名称等同于换了独立容量。

## 建议实施顺序

先把两个已知红回放转成回归并修复：CLI delivery 解释与 CLI→host dispatch。然后补全不同 driver 的故障注入矩阵，最后建立真实微任务认证入口。小任务耗时、输出和用量单独归账；不混入正式任务成功率，也不让一次轻量探针清除尚未解除的额度/权限 blocker。

验证目标是“指定故障引发了正确、有限的动作，接手者具备实际执行能力”，不只是“备用模型能够回复一句话”。
