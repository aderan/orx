# Stage 4 验收：日志分类、产品修复与主控续接

2026-11-19。当前 Codex 会话发起隔离项目中的 G001/R001，真实 ZCode 会话接手同一个 Run，最终两项任务均 passed，R001 为 done。逐次执行、原始用量、文件内容与恢复证明见 [acceptance.json](acceptance.json)。

## 分类修复

提交 `a2bb804` 移除裸数字 `429` 的匹配。JSONL 只从错误封装提取供应商错误；时间戳、事件 ID、token 数、正常生成内容和工具结果不会触发资源限流。纯文本需要 HTTP/status 429 或明确的限流消息。运行时 `timed_out` 优先记为 `timeout`，不因此冷却 profile 或自动派给备用候选。

先写回归，初次运行有 9 项失败；修复后新增 25 项回归通过，相关测试 211 项通过，全量 1249 项通过。Stage 3 保留的真实超时日志重放为 `timeout`，其中流式内容单独分类为 `process_failure`。旧日志已截断，结论仅基于保留内容；没有改写旧 attempt。

## 真实任务与受控故障

目标是构建本地日志诊断工具。T001 实现 `log_report.py`，T002 依赖它实现 `diagnose_cli.py`。后者的 preread 为空，前序模块从 ORX 的 dependency_artifacts 交接，携带 T001 / revision 1 / attempt 2 / 文件存在信息。Verifier 同时获得 Worker 交付、命令检查和依赖产物证据。

| Attempt | 执行角色 | 配置 | 结果 | 秒 |
| --- | --- | --- | --- | --- |
| 1 | 规划 | Claude Code sonnet / T1 / low | completed | 95.033 |
| 2 | T001 实现 | ZCode GLM-5.3 / T2 / low | completed，命令检查通过 | 38.561 |
| 3 | T002 实现 | Cursor Grok 4.7 / T2 / medium | completed，命令检查通过 | 126.111 |
| 4 | 第一条语义检查 | Codex CLI Sol / T1 / low | fail，发现注入的产品缺陷 | 18.250 |
| 5 | T002 修复 | Cursor Grok 4.7 / T2 / medium | completed | 149.478 |
| 6 | 第一条语义检查 | Codex CLI Sol / T1 / low | pass | 18.046 |
| 7 | 第二条语义检查 | 受控替身，未调用模型 | protocol_error，裁决为空 | 0.144 |
| 8 | 第二条语义检查 | Codex CLI Sol / T1 / low | pass，由新 ZCode 主控推进 | 17.308 |

产品故障由验收操作者在 attempt 3 结束后故意注入：T002 复制分类逻辑而不导入 T001 模块，命令输出仍正确。真实独立审查给出合法 fail，ORX 保持 T001 passed、将 T002 标为 failed。通过 `orx task retry T002` 启动 Cursor，失败证据进入新 Worker 输入；实际修复由 Cursor 完成。

协议故障由临时 PATH 替身只在 attempt 7 注入畸形裁决 `ORX_VERDICT=pass extra text`。这一次没有真实模型请求；其他 Codex 调用原样转发。ORX 返回 `protocol_error`、`verdict: null`、`retryable: true` 和下一步 `orx verify --json`，T002 保持 verifying。

随后停止由当前 Codex 主控发派工作，启动新的真实 ZCode GLM-5.3-Flash / low / yolo 会话。它加载 controller skill、查看配置与状态、调用一次 verify，完成 attempt 8。切换前 18 条 verification 行保持完全一致，只增加第二条语义检查的 pass 行 19；第一条 pass 行 18、命令检查、三个 Worker attempt 和计划修订均未变化。没有重建 Goal 或 Run。

ZCode 的自然语言总结误称命令检查“重跑”；逐行比对证明它们被复用。验收以机器记录为准。

## 项目默认配置

当前 ADE 会话继续担任主控，未增加 ORX 代启动终端主控功能。项目显式定义 CLI profile，保留 user 层的原生候选：

- light / standard 规划：Claude Code sonnet / low，Codex CLI 备用。
- deep 规划：Claude Code sonnet / high，Codex CLI 备用；由 Controller 选深度。
- Worker：ZCode GLM-5.3 / low → Cursor Grok 4.7 / medium → 兼容宿主的原生 ZCode 候选。
- Verifier：Codex CLI Sol / low → 兼容宿主的原生 ZCode Flash → 原有 Cursor 候选。

Codex 和 ZCode 两种主控环境的配置预览均保存于 JSON。原生候选按当前宿主和可启动 agent 筛选，CLI 按项目顺序。Stage 4 实际执行的是 coding 链路；deep/high、原生代理和视觉验收没有在这次 Run 中再次实测。

## 成本与边界

本次 7 次真实模型执行及 1 次协议替身均未出现权限阻塞或真实 CLI 超时。隔离项目每次 CLI 上限为 180 秒；仓库日常配置仍为 1800 秒。初始两次 Worker 的命令检查都通过。注入缺陷后首次正式验收通过 1/2，修复后 2/2；这个样本不能用于估计自然首轮通过率。3 次 Worker / 2 个完成任务，用户在发起后介入 0 次。Run 墙钟约 646 秒，另有规划 95 秒。

7 次真实模型执行都有原生用量记录，协议替身没有模型用量。ZCode 续接主控另报告自身 input 179995、output 1046、cache read 126848，缓存包含在输入中。当前 Codex ADE 主控用量未知，Claude 用量尚无完整归一映射，也没有金额观测，因此不计算完整总成本或每个验证成功任务的金额成本。故障注入产生的修复与重验开销保留在记录中。

提供了超时、断连、错误协议等自动化回归；这次真实 Run 注入的是协议错误，没有故意制造网络断连。验收不覆盖浏览器、视觉或 S 专有工具。隔离项目保留在 JSON 记录的临时路径中；当前 ORX 项目的已有运行状态和其他未提交修改保持原状。
