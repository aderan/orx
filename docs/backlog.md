# ORX Backlog

## 1.0 前候选：无人值守执行与任务授权

状态：待排期。第一步解决普通任务因缺少审批客户端而无法执行的问题，再让项目按任务收紧权限。

### 执行规则

- ORX 启动的非交互 CLI 在已授权范围内直接执行。需要额外权限时，执行端立即返回明确原因，由 Controller 决定换用权限匹配的候选、显式请求授权或暂停。由 ORX 启动的原生 subagent 使用相同的调度原则，实际权限按 ADE 宿主提供的能力判断。
- 项目先按角色定义权限。Worker 可编辑工作区并运行任务要求的构建和测试。Planner 默认以读取为主，Verifier 按验收条目开启必要的命令或浏览器能力。项目可明确允许某个隔离的 profile 使用 `yolo` 或该 harness 的等价设置。
- Plan 声明任务需要的权限，例如写入路径、命令或网络。Controller 依据项目已授予的权限作出决定，在派发前检查，遇到范围不足时将授权请求放在主控会话里。adapter 把决策映射到宿主实际支持的控制，并记录生效范围；无法落实的限制应导致该候选不适用。后备候选必须遵守相同的授权范围。

### 验收

1. 对启用的 CLI 和原生宿主分别验证权限映射。用真实小 Run 验证 Worker 能无人值守地写入并跑测试，以及 Verifier 能完成需要的检查。
2. 用同一套检查验证越界操作会被拒绝。需要审批、宿主能力不足和进程超时给出不同的可操作诊断；禁止等待隐藏审批或因切换候选而扩大权限。

### 已验证的触发场景

2026-11-18，使用 ZCode CLI 0.17.1、GLM-5.3 Flash、请求档位 `low`，在临时目录用相同提示和同一份 `conversion.py` 各跑一次非交互 `--prompt`，只改变 `--mode`。测试要求 Bash 运行一条 `python3 -c`，断言 `32°F → 0°C` 和 `212°F → 100°C`，成功后打印 `MODE_CHECK_OK`。

| 模式 | `stream-json` 中的 Bash 证据 |
| --- | --- |
| `edit` | 一次 `permission.requested`，随即以 `No permission client configured for Bash` 拒绝。Bash 未启动。 |
| `yolo` | 无审批事件；Bash 实际运行，退出码为 `0`，输出 `MODE_CHECK_OK`。 |

当前 ORX 的 ZCode Worker 显式使用 `edit`。此前真实 R001 中，同类拒绝发生后模型重复尝试，最终达到 ORX 的超时上限。本次提示要求拒绝后立即停止，两次 CLI 调用均完成。以上实测覆盖 ZCode 的 Bash 路径，其他 harness 的等价设置列在后续验收中。可参见 [ORX 的 ZCode 接口研究](zcode-cli-interface-research-2026-11-18.md) 和 [ZCode 官方权限说明](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/05-permissions.md)。
