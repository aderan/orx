# 跨宿主初始化与干净安装验收

2026-11-19，源码提交 `36d8df5`。构建本地 wheel 后，在两个独立的非 editable 安装环境中完成真实 Run。包版本保持 0.4.0，本次没有发布新版本。完整逐次执行、安装来源、文件哈希与用量见 [acceptance.json](acceptance.json)。

## 入口与配置

- `orx --host codex ...`、`orx --host zcode ...` 声明本次调用的 Controller；重复 `--host-agent <引用>` 声明实际可启动的原生角色，省略表示没有。已知主控会话可用 `--host-session`。显式声明覆盖旧环境变量，本次调用结束后不保留到其他调用。
- 默认 self profile 改为 `harness = "current"`，路由时解析为当前声明的宿主。缺少声明时，预览、路由和 doctor 提供提示。已有指派保留原宿主；切换主控会显示对应阻塞，不改写原 attempt。
- 项目 CLI profile、候选顺序和主控身份分开。`preset install cli` 提供可编辑的跨 CLI 示例配置；在新项目初始化前安装，项目继承用户配置。已有覆盖值和旧 profile 不自动迁移或覆盖。
- `next_actions` 的可执行命令保留本次主控参数。旧 `ORX_CONTROLLER_HARNESS/AGENTS` 继续兼容。没有增加 ORX 代启动终端主控的入口。
- `--host <宿主> skill install` 在该宿主技能目录尚不存在时也能安装；支持 Codex、ZCode、Cursor、Claude Code。`--home` 可指定独立安装目录，PBV 保持显式可选。

主控身份不从 `controller.profile` 猜测，也不把原生能力写成项目全局状态。`current` 只用于 host self，不能用来声明 CLI 或原生 subagent。默认 `unconfigured` 模型是占位值，不代表主控会话的实际模型或可调整的推理档位。

## 测试

新增 26 项回归，覆盖四种主控的初始化、参数优先级与隔离、原生引用筛选、指派归属、下一步命令直接执行、preset 继承和 skill 安装更新。全量 **1275 passed**，耗时 185.75 秒。Controller skill 的官方验证脚本通过。

首次安装冒烟发现相对 `--home` 产生无法解析的符号链接。先补失败回归，再将安装路径解析为绝对路径；重建 wheel，重新创建两个最终环境后验收。原预检项目只运行了规划，没有 Worker，记录保留在临时目录，未计作最终通过的 Run。

另在安装包中验证无用户 preset 的默认初始化：同一个项目分别声明 Codex 与 ZCode，Planner、Worker、Verifier 的 current profile 均可解析。四种宿主的干净 skill 安装链接均可读取。

## 两次真实 Run

两边使用同一 wheel、全新 venv、用户配置目录、项目和 skill 安装目录；复用本机已有 CLI 与登录。确认 import 来自各自 site-packages，没有 editable 安装；安装后的全部 Python 源文件与受验源码逐项哈希一致。ZCode 主控实际读取了这次安装的 Controller skill。

真实任务是离线购物清单工具：T001 编写 `cart_core.py`，按整数分计算数量乘单价并检查无效值；T002 依赖 T001 编写 `cart_cli.py`，读取 UTF-8 JSON 并调用前序模块。T002 的 preread 为空，模块通过 dependency_artifacts 交接，关联 revision 1 / T001 / Worker attempt 2。

| 主控 | 规划 | 两项执行 | 独立验证 | 结果 |
| --- | --- | --- | --- | --- |
| 当前 Codex ADE 会话 | Claude Code sonnet / low，25.099 秒 | ZCode GLM-5.3 / low，26.599、18.827 秒 | Codex CLI Sol / low，19.011 秒 | 两项 passed，R001 done，输出 1900 |
| 真实 ZCode Flash / low 会话 | Claude Code sonnet / low，42.282 秒 | ZCode GLM-5.3 / low，27.366、40.284 秒 | Codex CLI Sol / low，15.779 秒 | 两项 passed，R001 done，输出 1900 |

实际路由记录全部带对应主控声明。没有设置 ORX 主控身份环境变量，native agent 列表均为空。两条最终 Run 共 8 次 CLI 执行，无权限阻塞、超时或修复重试；ZCode 主控自己另有独立会话及原生用量记录。工作实现均由配置中的 Worker 完成。验收只通过公共 ORX CLI 改状态，诊断 SQL 使用只读连接。

## 边界

“干净”指 ORX 包、虚拟环境、配置、项目和技能安装；CLIs 与登录沿用本机环境。CLI preset 中的模型名是可编辑示例，模型可用性仍取决于本机目录与订阅。原生角色仍由当前会话据实声明，本次没有启动原生 subagent；Cursor/Claude 主控仅验证了安装与自动化契约。没有扩展内容快照失效、并行、权限隔离或完整成本归因。
