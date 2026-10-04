# ZCode 原生子代理接入：阶段 A 实测记录

日期：2026-10-04。本文是 [zcode-subagent-analysis.md](zcode-subagent-analysis.md) 三阶段建议中 **阶段 A（原生能力实测）** 的执行记录。分析文档中标注"尚未实测"的项目，凡能在已有会话内完成的，本文均给出实测结果；需要新会话的，给出验证协议。官方能力核查背景见 [研究记录](zcode-capabilities-research.md)。

## 结论一览

| 项 | 状态 | 证据 |
| --- | --- | --- |
| GLM-5.3-Flash 本账号可用 | ✅ 实测 | ListModels：`account:bigmodel-individual-coding-plan/GLM-5.3-Flash`，enabled |
| Flash 推理档位 | ✅ 实测 | ListModels：low / high / max（默认 max），与 GLM-5.3 相同 |
| 定义文件 `model` 字段格式 | ✅ 实测 | 应用内置 plugin agent 定义使用 provider-qualified ID，与 ListModels 返回值一致 |
| Flash 子代理真实执行 + 可查记录 | ✅ 实测 | db.sqlite `model_usage`：`zcode-documents:visual-judge` 在 GLM-5.3-Flash 上有 completed 请求记录（含 token/耗时） |
| 子代理会话账本与父子关系 | ✅ 实测 | `session.parent_id` + `task_type='subagent_child'`（本机已有 554 条） |
| 子代理稳定身份句柄 | ✅ 实测 | Agent 工具返回的 agentId 与 db 子会话 id 一一对应（`agent_2aebc895…` ↔ `sess_subagent_agent_2aebc895…`） |
| 实际模型归属记录 | ✅ 实测 | `model_usage.agent` + `model_id` 按子代理会话记录实际模型 |
| 平台注入模型自报 | ✅ 实测 | 子代理指令含 "powered by account:…/GLM-5.3"（平台注入，非代理猜测；仍以 db 记录为准） |
| 独立上下文 | ✅ 实测 | 探针子代理报告：看不到父会话任何内容 |
| 子代理不能再派发（Agent 工具） | ✅ 实测 | 探针子代理无 Agent 工具；但见下方 CreateWorkflow 绕行 |
| 结果回传父会话 | ✅ 实测 | 探针结果完整返回（含实际命令输出） |
| 工作目录 | ✅ 实测（共享） | 子代理 cwd = 父会话项目根；独立上下文 ≠ 独立文件系统 |
| 自定义 agent 灰度覆盖本账号 | ✅ 实测（新会话） | 2026-10-04 13:19 新会话：Agent 工具子代理列表出现 `orx-worker` / `orx-verifier`，可派发 |
| orx-worker / orx-verifier 端到端最小任务 | ✅ 实测（新会话） | 见下方 e2e 执行记录；两角色契约完整回传，db 模型归属命中 |

## 证据明细

### 1. 定义文件格式（model 字段取值已定）

应用自带定义（如 `/Applications/ZCode.app/Contents/Resources/glm/packages/documents-plugin/agents/visual-judge.md`）：

```yaml
model: account:bigmodel-individual-coding-plan/GLM-5.3-Flash
thoughtLevel: max
tools: [Read, Bash]
```

`model` 使用与 ListModels 相同的 provider-qualified ID——研究记录中"未核实模型标识格式"一项就此关闭。内置定义在 Flash 上使用 `thoughtLevel: max`，与 ListModels 的 low/high/max 一致。

### 2. 会话账本（db.sqlite，`~/.zcode/cli/db/db.sqlite`）

- `session`：`parent_id` 指向父会话，`task_type='subagent_child'`（另有 `workflow_child`、`selection_side_chat`、`fork`）。
- `model_usage`：逐模型请求记录，含 `session_id`、`agent`（如 `zcode-general-purpose`、`zcode-documents:visual-judge`）、`model_id`、状态、input/output/reasoning tokens、耗时。**这是"实际用了什么模型"的权威来源**，profile 期望值不能替代它。
- `session_task_link`：目前仅记录 workflow actor，Agent 工具子代理不写此表；父子关系以 `session.parent_id` 为准。

本次探针（general-purpose 子代理，13:09:25 派发）的落盘记录：

- 子会话：`sess_subagent_agent_2aebc895…`，parent = 本会话，返回的 agentId 与会话 id 前缀一致。
- model_usage：`zcode-general-purpose | GLM-5.3 | completed`，input 67,660（其中 cache_read 67,392）、output 3,223、tool_call 2、30.6s。

历史 Flash 子代理执行（今日 12:31–12:33，documents 插件 visual-judge）：

- `zcode-documents:visual-judge | GLM-5.3-Flash | completed` ×3，input 36k–42k、output 100–895、19–20s/次。

观测备注：探针与历史记录的 `reasoning_tokens` 均为 0。仅作现象记录；按分析文档 §8 的纪律，不据此对档位计费或思考开销下结论。

### 3. 活体探针结果

向 general-purpose 子代理派发五步探针，返回：cwd 为项目根；`shasum` 真实执行且结果正确；**无 Agent/SubAgent 工具**（平台级禁止下级派发）；指令中带平台注入的模型名；对父会话内容零可见。

### 4. 绕行通道与防护

子代理虽无 Agent 工具，但工具面含 **CreateWorkflow / AmendWorkflow / ResumeWorkflowRun / OffPeakCreate**——经 workflow 仍可编排骨代理。已给 `orx-worker` 定义加 `disallowedTools` 堵住；`orx-verifier` 用 `tools: [Read, Bash]` 白名单天然排除。

## 已安装的定义

权威来源在本仓库 [agents/orx-worker.md](../agents/orx-worker.md)、[agents/orx-verifier.md](../agents/orx-verifier.md)，已复制到 `~/.zcode/agents/`（官方位置，需新会话生效）。

| 定义 | 模型 / 档位 | 工具 | 要点 |
| --- | --- | --- | --- |
| orx-worker | GLM-5.3 / max | 全量（禁 workflow 派发四件） | ONE assignment 契约（plan/task），scope 纪律，evidence JSON，不碰 state.db；effort 自报按定义声明（configured 事实） |
| orx-verifier | GLM-5.3-Flash / max | `[Read, Bash]`，injectAgentsMd:false | 独立法官：ORX_REASON + ORX_VERDICT 两行契约；从一手材料判定；不修复工作 |

设计边界如实记录：verifier 的 Bash **未被硬限制为只读**（分析文档 §6 的告诫仍成立）；已通过排除 Write/Edit 获得部分硬保证，Bash 写入仍靠契约约束，证据文件由 Controller 落盘。verifier 关闭 AGENTS.md 注入以保持独立判断。

## 新会话验证协议（阶段 A 收尾）— ✅ 已执行通过（2026-10-04 13:19–13:21）

1. 新开 ZCode 会话（同一台机、任意工作目录），确认可用子代理列表出现 `orx-worker` / `orx-verifier`。若未出现：自定义 agent 灰度未覆盖本账号，或定义被诊断忽略（检查 name/description）。
2. 给 `orx-verifier` 一个最小判定任务（例：验证指定文件 shasum 是否等于给定值），验收：两行契约完整回传。
3. 给 `orx-worker` 一个最小任务（例：读文件并产出 evidence JSON），验收：结果回传、无越界。
4. 查 db 复核模型归属：子会话 `task_type='subagent_child'`，`model_usage.agent` 为自定义角色名（内置模式是 `zcode-<type>`，自定义的确切命名待实测），`model_id` 分别为 GLM-5.3 / GLM-5.3-Flash。
5. 全部通过后，本文档相应项由 ⏳ 改 ✅，阶段 A 关闭，进入阶段 B（ORX 执行契约）。

复核命令（任意一条）：

```sh
sqlite3 -readonly ~/.zcode/cli/db/db.sqlite \
  "SELECT mu.agent, mu.model_id, mu.status, datetime(mu.started_at/1000,'unixepoch','localtime') \
   FROM model_usage mu JOIN session s ON s.id=mu.session_id \
   WHERE s.task_type='subagent_child' ORDER BY mu.started_at DESC LIMIT 5"
```

## e2e 执行记录（阶段 A 关闭）

新会话（13:19）按上述五步协议执行，全部通过：

1. **加载确认**：新会话可用子代理列表出现 `orx-worker` / `orx-verifier` —— 灰度覆盖本账号，`~/.zcode/agents/` 定义生效。
2. **orx-verifier 最小判定**（shasum 核对 `pyproject.toml`）：回传完整两行契约
   `ORX_REASON=…shasum -a 256…matches exactly` / `ORX_VERDICT=pass`。agentId `agent_f2a98534…`。
3. **orx-worker 最小任务**（读 `pyproject.toml` + `src/orx/__init__.py`，报包名/版本/单源接线）：事实正确（orx-agent 0.2.1，hatch dynamic），evidence JSON 按契约形状落盘（summary/commands/artifacts），无越界。agentId `agent_65bdcbfa…`。
4. **db 复核**（复核命令原样执行）：两子会话均 `task_type='subagent_child'`，`model_usage` 逐请求 completed：
   - `zcode-orx-worker | GLM-5.3`（3 requests）
   - `zcode-orx-verifier | GLM-5.3-Flash`（2 requests）
   子会话 id `sess_subagent_agent_<agentId>` 与 Agent 工具返回的 agentId 一一对应，与阶段 A 旧结论一致。
5. **新事实（阶段 B 直接输入）**：自定义角色在 `model_usage.agent` 的命名与内置同为 **`zcode-<name>`**（无 plugin 前缀）。阶段 B 按 `zcode-orx-worker` / `zcode-orx-verifier` 查询实际模型归属即可，无需新命名方案。

e2e 产物为临时探针（`tmp-e2e/`），核验后已删除，不留运行残留。

## 对阶段 B 的输入更新（相对分析文档）

1. **稳定身份已有**：agentId ↔ 子会话 id 直接对应，可作 ORX `session_ref` 的子代理事实来源，无需发明新句柄。
2. **模型字段格式已定**：provider-qualified ID；preset/profile 直接复用同一字符串，避免两套标识。
3. **实际模型归属有权威来源**：`model_usage.agent + model_id`；分析文档中"期望≠实际"的缺口，阶段 B 应把 attempt 的模型来源接到这里，而非相信 profile。
4. **下级派发是工具面属性**：探针证明子代理无 Agent 工具，但有 workflow 绕行；ORX 侧的角色定义应以 disallowedTools/白名单落地，而非仅提示词。
5. **工作目录共享**：分析文档 §6 的"独立上下文≠独立文件系统"获实测确认，第一版写入型 worker 串行的结论不变。
