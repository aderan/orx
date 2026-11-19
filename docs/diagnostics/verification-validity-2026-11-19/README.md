# 验收证据失效规则：真实 Run 验收

2026-11-19。实现提交 `7565e11`。新增 23 项回归，全量 **1298 passed**，耗时 218.38 秒。规则定义见 [验收证据有效性](../../verification-validity.md)，逐次执行、context 指纹及历史行证明见 [acceptance.json](acceptance.json)。

## 实现边界

检查复用同时依据 Worker attempt 和内容版本。版本覆盖交付产物、preread、依赖产物及其输入版本、任务验证契约。变化以任务为单位使旧检查失效，并传播到相关下游；其他任务的有效记录继续复用。历史行不删除，重验日志不覆盖。缺少指纹的历史结果重新验证一次，旧无归属裁决不能进入新的 Worker 交付。

Verifier 绑定派发时的 context。回归覆盖 CLI 在执行中修改输入、host 在途裁决、恢复原内容、删除文件、工作区外符号链接、目录缓存、glob、完成回执重放和依赖重新验收。陈旧结果返回 null verdict / evidence_changed，不形成产品失败裁决，也不重复派发仍在途的 attempt。

status 只读展示陈旧证据及下一步；run/verify 负责持久化失效、重新验收和依赖等待。没有加入后台文件监控或文件系统快照隔离。

## 真实工作

当前 Codex ADE 会话主控隔离项目 G001/R001。Claude Code sonnet / low 规划；ZCode GLM-5.3 / low 实现 T001 `text_core.py` 和依赖它的 T002 `count_cli.py`；Codex CLI Sol / low 独立验证。T002 的 preread 为空，前序模块通过 dependency_artifacts 交接。

工具统计 UTF-8 文件中的非空行。示例包含 alpha、一个空行、beta，CLI 输出 `2`；空文本与纯空白行检查通过，缺失路径非零退出。

## 验收过程

| 场景 | 观测结果 | 当前有效 verification 行 ID |
| --- | --- | --- |
| 首次验收 | 第一条真实语义检查 pass；第二条响应由受控替身注入畸形裁决，无模型调用 | T001：1–4；T002：5–9，第二条待验 |
| 内容保持不变 | 只启动第二条真实验证，原 9 行、日志哈希和两个 context 均不变，Run done | T001：1–4；T002：5–10 |
| 修改 T002 产物 | 追加无行为影响的注释，只重验 T002；T001 行及 context 4 不变，Run done | T001：1–4；T002：11–16 |
| 修改 T001 依赖产物 | 追加无行为影响的注释，T001 和 T002 旧验收均失效；按依赖顺序重新验收，Run done | T001：17–20；T002：21–26 |

输入变化由 status 只读识别并返回 verify 动作。产物修改后的 T002 context 为 15；依赖修改后 T001/T002 context 为 20/22。context ID 属于 task_events，与上表 verification 行 ID 是不同的身份空间。

最终 Run 保持 revision 1，两项任务 passed，无陈旧证据，当前有效 10 条检查均通过。整个最终 Run 只有 Worker attempt 2、3，没有因协议故障或输入变化重跑 Worker。26 条历史 verification 行及其日志哈希逐阶段保留；对应 CLI 仍输出 `2`。

## 预检与限制

预检发现同一 Worker 交付前的失败自测混入当前验收计数。补充失败回归，将有效窗口从本次交付门禁行开始，之后在新隔离项目重新完成上面的最终 Run。预检原始项目保留，路径见机器证据；它不计作最终成功验收。

最终 Run 有 9 次真实角色 CLI 执行，另有 1 次明确标记的协议替身，没有权限阻塞或真实 CLI 超时。真实文件变化发生在验收调用之间；在途修改场景由回归覆盖。本次不是自然失败率或性能基准。原生 token 用量按来源保留，当前 Codex 主控及金额成本没有完整归因。
