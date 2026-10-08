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

## 第二层执行矩阵：逐行证据定位（G009 T008，2026-10-08 执行）

下表把上方 12 行矩阵逐行定位到确定性用例（文件::用例），并给出每行被断言的
有限动作。注入纪律：全部用例只伪造外部进程（PATH 脚本/捕获信封）、提供商
响应（fake fetch/quota snapshot）与时钟（store 内伪造 reset 值或冻结 ORX 时钟
缝隙）；被测的 dispatch/路由/健康学习/状态机/交付门禁从不 mock。标记
【新增】的用例为 T008 补齐的组合缺口；其余为 T001–T007 已落地用例。

| 场景 | 证据（tests/…） | 断言的有限动作 |
|---|---|---|
| 限流 | test_health.py::test_rate_limit_sets_bounded_cooldown；test_health.py::test_failing_worker_flips_health_and_routing_falls_back；test_health.py::test_rate_limited_cli_falls_back_to_host_parking；test_routing.py::test_reroute_to_host_rung_parks_host_assignment；test_routing.py::test_reroute_hop_is_bounded_once_per_slice | cooldown 窗口有界（2^n 封顶+jitter）且过期重开；同 slice 一次切换到下一 rung（CLI→CLI 执行、CLI→host 停泊 own nonce attempt）；每任务每 slice 至多一跳，双限流 rung 恰两 attempt 后停 FAILED |
| 额度耗尽 | test_quota.py::test_run_slice_falls_back_to_next_rung_on_quota_failure；test_quota.py::test_routing_rejects_exhausted_until_reset_passes；test_health.py::test_naive_reset_stays_gated_on_both_sides_of_virtual_clock；test_health.py::test_probe_success_never_lifts_active_quota_exhaustion | reset 从失败文本解析并记录；reset 前不选该 rung、过期后仅作为 unknown 候选重新考虑（无虚假 available）；探针成功不清额度门禁（不宣称额度充足） |
| 认证失效 | test_health.py::test_auth_failure_is_bounded_no_reroute_no_login_loop【新增】；test_health.py::test_auth_required_gates_routing；test_health.py::test_success_after_auth_required_recovers | 恰一次失败 attempt（无同 slice 换挡、无登录循环）；profile 门禁使后续路由带审计跳过；操作者解决后显式 retry 路由下一 rung 通过；成功恢复正常 |
| 权限阻塞 | test_delivery_gate_e2e.py::test_cli_blocked_delivery_survives_a_green_controller_gate；::test_cli_blocked_delivery_survives_a_red_controller_gate | exit 0 + blocked 交付以 attempt result=blocked 记录、task 失败且 reason 为环境阻塞前缀；绿门禁与红门禁都不能改写 blocked；usage/session 照常记录 |
| 未解除的权限阻塞 | test_routing.py::test_same_permission_blocker_is_not_relaunched_after_limit；::test_suppressed_rung_falls_through_to_next_profile；::test_suppression_limit_is_configurable；::test_host_worker_same_permission_blocker_is_suppressed_too【新增】；::test_suppression_is_task_scoped_other_tasks_still_get_the_profile【新增】 | 同指纹 blocker 达限后不再启动（第 4 轮零 attempt、零付费调用）；三处留痕（run 输出/路由审计/task event）带 class/count/remedy；抑制按 driver 无差别（host 经 task complete 的 blocked 同样受限）；抑制按任务域（其他任务仍获该 profile，独立 streak） |
| 代码检查红 | test_delivery_gate_e2e.py::test_scenario_b_gate_reject_then_same_attempt_fix_all_green；test_health.py::test_passed_cli_delivery_with_red_gate_keeps_profile_healthy；test_quota.py::test_run_slice_keeps_non_resource_failures_failed | 门禁拒绝保留同 attempt 修复机会（check→fix→check→complete 全程一个 attempt）；代码红不写资源状态（profile 保持 available）；非资源失败不触发换挡 fallback |
| 不同执行方式 | CLI→CLI：test_health.py::test_failing_worker_flips_health_and_routing_falls_back；CLI→host：test_routing.py::test_reroute_to_host_rung_parks_host_assignment、test_health.py::test_rate_limited_cli_falls_back_to_host_parking；CLI→external：test_routing.py::test_reroute_to_external_rung_parks_external_contract_only；host→CLI：test_routing.py::test_host_resource_failure_redispatches_to_cli_by_driver；host→host：test_routing.py::test_host_resource_failure_redispatches_to_next_host_rung；quota×driver【新增】：test_routing.py::test_reroute_on_chinese_glm1308_quota_parks_host_rung | 每条按目标 rung 自身 driver 分发（执行/停泊/正式外部契约）；host 故障先经显式结束事实（task fail）+操作者资源事实再经 run_slice 改派；身份绑定（新 attempt+新 nonce，失败档留历史）；中文 GLM 1308 额度类同样走 driver 正确的 host 停泊而非 adapter 崩溃 |
| 所有候选失效 | test_routing.py::test_all_non_routable_is_an_error；::test_all_candidates_unavailable_is_bounded_and_explained | 单条错误逐 rung 列原因（含 quota_domain_exhausted）；任务留 runnable、零启动、二次 slice 同样有界停止；无换模型偷换 |
| 旧结果迟到 | test_host_progress_e2e.py::test_scenario_b_confirmed_death_fail_retry_rejects_old_attempt；test_host_recovery_facts.py::test_recover_keeps_single_writer_and_rotates_the_nonce（T006）；【新增】test_host_progress_e2e.py::test_stale_cli_attempt_cannot_deliver_after_resource_reroute | 旧 attempt 迟到交付 stale 拒绝、迟到报告 closed/superseded 拒绝；fail/retry、recover、资源换挡三条路径下 B（新 attempt）状态均不被覆盖，最终只有新 attempt 的 completion 入库 |
| 未确认死亡 | test_host_progress_e2e.py::test_scenario_a_overdue_hint_then_original_attempt_late_delivery；test_host_recovery_facts.py::test_lost_contact_but_still_writing_never_becomes_a_verdict（T006） | overdue 仅 CHECK HINT（零状态移动、零新 attempt）；失联仍在写=观察事实（verdict None），恢复仍需操作者确认终止事实；原始 attempt 的有效迟到交付仍可受理 |
| 手动 override | test_health.py::test_override_protects_and_clear_reenables；::test_operator_override_never_auto_cleared_across_quota_paths；test_quota.py::test_refresh_never_touches_operator_overrides；::test_refresh_healthy_preflight_lifts_naive_reset_exhaustion；test_routing.py::test_operator_override_beats_the_domain_gate；【新增】::test_operator_override_outranks_learned_exhaustion_on_the_same_profile；test_probes.py::test_preflight_never_clears_quota_cooldown_or_override | 自动学习/预检/探针/额度刷新/域门五路均不解除 override；同 profile 上 override 直接压过已学习的 exhaustion（routing 保持 primary、行 override=1）；clear 与 available 区分可见（clear 后学习恢复写入）；操作者事实 > live dashboard |
| 共同故障域 | test_host_recovery_facts.py::test_shared_failure_records_the_domain_fact_and_parks_explainably（T006）；test_quota.py::test_shared_domain_reroute_skips_sibling_rung；test_routing.py::test_shared_quota_domain_is_not_independent_capacity；【新增】::test_shared_quota_domain_gates_the_controller_until_a_recoverer | 独立观察者（task note shared_failure）记录事实且零状态移动、无自动继续、迟到交付仍受理；sibling 以 quota_domain_exhausted 审计拒绝（不伪装容量）；【新增】控制器 profile 同域时 plan_route 同样 RoutingError 有界停止（重复调用同样停止=如实停留），直到操作者为控制器显式恢复（override）才重新调度 planning，worker 域成员不受牵连地保持自己的门禁 |

GLM 1308 补充验收（中文原始消息 + 无时区 reset）：test_quota.py::
test_provider_error_kind_truth_table（中文 quota/rate/auth 词汇 + 1308 代码形状）、
::test_provider_quota_reset_preserves_string_verbatim（naive/offset 逐字保留、
标记位置无关、裸时间戳不算）、::test_chinese_glm1308_end_to_end_naive_reset_
never_self_releases（真实 dispatch 栈端到端：诊断 quota_exhausted、逐字 naive
reset、跨 slice 永不自动放开、agent_status 标注 timezone unknown、operator 显式
恢复）；test_health.py::test_resource_error_kind_composes_english_then_provider、
::test_failure_class_taxonomy 的中文 resource 行。矩阵执行时点：以上全部为绿。

### 组合路径覆盖（T008 验收项）

- **driver × 权限**：host 经 `orx task complete` 的同条件权限阻塞同样受限
  （test_host_worker_same_permission_blocker_is_suppressed_too）；CLI 替身的
  既有矩阵见"未解除的权限阻塞"行。
- **driver × 配额**：中文 GLM 1308 额度失败 → host 停泊
  （test_reroute_on_chinese_glm1308_quota_parks_host_rung）；限流版本的
  CLI→host/external、host→CLI/host 见"不同执行方式"行。
- **配额 × override（同 profile）**：test_operator_override_outranks_learned_
  exhaustion_on_the_same_profile——学习到的 exhaustion 让位给同 profile 上的
  操作者 override，clear 恢复学习。
- **陈旧身份 × 换挡**：test_stale_cli_attempt_cannot_deliver_after_resource_
  reroute——资源换挡后死亡 rung 的迟到交付 stale 拒绝，新 owner 正常交付。
- **控制器同故障**：test_shared_quota_domain_gates_the_controller_until_a_
  recoverer——planner 与 worker 同 quota_domain，worker 的 naive-reset
  exhaustion 使 plan_route 有界停止；恢复者（操作者 override）出现前如实停留。
- **认证 × 边界**：test_auth_failure_is_bounded_no_reroute_no_login_loop——
  auth 不属资源换挡类：不换挡、不循环、单次尝试、门禁跳过、retry 后走下一档。

### 退避与切换组合验收（本节证据）

确定性半段：A 注入资源失败后 B 真实执行并交付（test_failing_worker_flips_
health_and_routing_falls_back、test_run_slice_falls_back_to_next_rung_on_
quota_failure——B 在同 slice 完成且任务通过）；正向恢复推进虚拟时钟跨 reset 后
路由重新考虑 A（test_routing_rejects_exhausted_until_reset_passes、
test_quota_domain_gate_releases_when_reset_passes）；真实恢复探针只证明当次
请求（test_probe_success_never_lifts_active_quota_exhaustion、
test_success_during_active_exhaustion_keeps_gate_recovers_after_reset）。
真实微任务半段（隔离仓库 + nonce + 真实权限边界的第三层）不属确定性矩阵，
按计划由 T009 的真实微任务认证入口验收，此处不宣称已通过。

### 探针适用范围（本节证据）

绑定与失效：test_probes.py::test_fingerprint_invalidates_on_every_bound_change
（CLI 版本/profile model/effort/workspace/工具链首 token/配置轴值变化各自换
指纹；同内容 touch 经 mtime tripwire 退役旧结果）；::test_preflight_axes_move_
independently；::test_help_output_and_probe_success_are_not_permission（探针/
help 成功不染指权限轴）；::test_refresh_failure_never_relabels_old_catalog_as_
fresh（实际失败优先于旧成功——目录域）。probe passed 与额度充足分存：
test_probe_success_never_lifts_active_quota_exhaustion、
test_preflight_never_clears_quota_cooldown_or_override；额度快照走 quota.py
独立 fetcher（test_quota.py 全部 fetcher 用例）。共享账号故障域关联：
quota_domain 解析与域门（test_routing.py::test_quota_domain_field_parses_and_
defaults_standalone 及域门三例）。

## 第三层：真实微任务

> **执行状态（G009 T009，2026-10-08）**：已执行。四次真实启动（每 profile 首轮
> 一次）：cursor-strong 54.3s 权限阻塞被正确诊断（预检先于付费启动预测）；
> codex-frontier 61.9s 完整 CLI 闭环；zcode-worker（真原生子代理）58s 完整
> host 闭环；zcode-verifier-flash 56s 独立验收。原始证据
> docs/diagnostics/g009-microtask-2026-10-08/evidence.json。

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
