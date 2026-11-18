# ZCode CLI 接口与 ORX 接入边界（2026-11-18）

`edit` 允许 ZCode 直接使用工作区文件编辑工具。它对 Bash 沿用 `build` 的判定，所以能改文件、能执行 `cat`，却可能没有权限执行 Python 测试。对于当前的非交互 CLI，工具需要审批时会立即被拒绝；这次是模型在拒绝后继续尝试，最终耗尽了 ORX 的时限。

## 本机验证了什么

- 安装版本为 `0.17.1`。第二阶段用同一 `edit` 模式验证了两种行为：最小测试中 `Write` 写入工作区，`Bash(cat)` 被标为只读并成功；真实 R001 的 T001 需要执行 Python 检查，捕获到 7 次 `Bash` 审批请求（理由 `High risk tools require explicit approval`），对应 7 次 `deny`（`No permission client configured for Bash`）。ORX 在约 240.25 秒结束该次进程，后用 Codex CLI 续做。同一 Run 的 ZCode Flash `plan` 验证阶段成功。
- 该次失败另有一个 ORX 误报：通用分类器会在整段文本里找 `429`，流中事件 ID、时间戳或序号也能带这个数字。这不足以认定供应商限流。应该分别保留“进程超时”“Bash 无审批客户端”，并仅从结构化错误、单独的诊断或明确的错误文句识别限流。
- 本机 `zcode app-server` 的 stdio `runtime/capabilities` 查询成功。先前尝试创建会话时，服务还会反向请求运行偏好；回应后，未初始化的 provider registry 仍找不到指定模型。已验证协议可达，尚未验证可完成整次任务或工具审批。

## 从源码确认的接口

下述链接固定在官方仓库提交 [`fec9759`](https://github.com/zai-org/ZCode/commit/fec975905a2abd4c07e35c40d51fa097a2258ec9)（源码快照 `v3.18.2`）。它与本机 CLI 的版本号体系不同；只有上面列出的行为和上一阶段探测过的调用算本机实测。源码更新后仍应按已安装 CLI 做一次探测。例如，本机 `--help` 未列出 `--output-format`，实际接受了 `stream-json`。

| 入口 / 能力 | 接口与使用边界 |
| --- | --- |
| 非交互执行 | `--prompt` + `--cwd` + 显式 `--mode`，适合 ORX 现有进程 adapter。`--output-format stream-json` 为逐行 JSON，正常结束有 `type: result`；stderr 单独留存。退出码、结果帧、工具结果和 ORX 交付都要检查。`--resume <sessionId>` 显式恢复指定会话；并发时避免依赖 `--continue` 的最近会话选择。[CLI 参数及输出](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/02-cli-reference.md#L15-L59) · [会话语义](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/06-sessions.md#L3-L34) |
| 模型和 effort | ORX 给 ZCode 传独立的个人模型配置路径 `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`，写入 provider/model 以及该模型支持的 reasoning 选项；`--model`、`--settings` 不是这个 CLI 的公开参数。从事件记录请求值，只有供应商实际报告才记录生效值。[配置入口及规则](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/03-config.md#L3-L59) |
| 双向宿主 | 终端会话可交互审批；`app-server` 则让客户端自己接入。快照中的 stdio 是 NDJSON，覆盖反向 `interaction/requestPermission` 及 V4 `resolveInteraction`。需要按版本实现会话、模型初始化、审批回调和收口，有了实际审批宿主才能验证需要审批的 Worker。[stdio 协议](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/design/v2/zcode-protocol-agent-server.md#L25-L98) · [审批桥接](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/bootstrap/src/zcode-protocol/interaction-response-race.ts#L5-L23) · [V4 命令](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/packages/shared/src/zcode-protocol-v4/command.ts#L312-L349) |

| 权限模式 | 实际边界 |
| --- | --- |
| `plan` | 限制普通写入与非只读命令，保留部分 MCP 和会话控制。不是操作系统级只读沙箱。 |
| `build` | 可直接做低风险只读和部分会话内动作；普通写入、有副作用或高风险的工具通常要审批。 |
| `edit` | 在 `build` 之上自动接受符合策略的工作区文件编辑；`Bash` 仍需单独判定。 |
| `guarded` | 普通调用采用宽松策略，匹配危险规则的命令仍要逐次批准。无客户端时不能完成这类调用。 |
| `yolo` | `--prompt` 的缺省模式，普通调用跳过审批，仍受工具级交互、Plan、Hook 等约束。ORX 当前应继续显式选模式。 |

[官方模式说明](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/05-permissions.md#L3-L35) · [Build/Edit 权限分支](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/permission/service.ts#L565-L645) · [Bash 风险识别](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/tool/handlers/bash.ts#L114-L148) · [无界面审批 broker](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/permission/broker.ts#L24-L38)

**是否能只批准一个项目里的命令？** 底层有项目或会话规则，支持 Bash 命令参数匹配，也有保存审批规则的路径。`--disallowed-tools` 则在注册层移除整个工具，写成 `Bash(…)` 也不会变成参数过滤；没有受支持的 `--allowed-tools` 可由 ORX 在单次 `--prompt` 注入。没有实测本机安装版的规则录入和复用，不能把这项底层能力计为当前 adapter 已支持。[规则判定](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/permission/service.ts#L161-L234) · [Bash 匹配](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/tool/handlers/bash-semantics.ts#L161-L177) · [规则读取和保存](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/tool/executor/permission-rules-persistence.ts#L19-L67)

## ORX 下一步

先保留 `plan` 用于分析和不需要执行命令的语义验证，`edit` 用于纯文件任务；若 Worker 合同要求它自己跑 Python、构建或测试，路由到已具备该执行能力的项目 profile。另一种分工是让 Worker 交付文件，ORX 运行项目预先定义的确定性检查。将审批拒绝留作工具诊断，再结合是否缺失必要产物或必跑检查决定本次失败；合法的只读回退成功时不应误报整个任务失败。只有明确需要 ZCode 持有 Shell 审批时，再做一个独立的 app-server 探针，验收模型初始化和同一条无害命令的接受、拒绝两个分支。
