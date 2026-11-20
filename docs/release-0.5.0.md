# ORX 0.5.0

0.5.0 将跨 CLI 的 Goal 执行与只读 Analytics 收到同一仓库。ADE 中接受目标的当前会话作为 Controller，项目配置决定 Planner、Worker 与 Verifier 的候选顺序。ORX CLI 负责执行状态和依赖，skill 负责推进流程；Analytics 提供观测，不获得恢复或调度权限。

## 版本与数据契约

| 组件 | 软件版本 | 数据契约 |
| --- | --- | --- |
| ORX CLI (`orx-agent`) | 0.5.0 | ORX schema 15 |
| Analytics CLI | 0.5.0 | Analytics store 4 |
| macOS App | 0.5.0 | 引擎协议 1，内置 Analytics 0.5.0 |

软件版本同步不意味着数据库协议同步升版。旧 schema 8–15 仍按明确的只读兼容范围处理，未知未来版本拒绝读取。

## 执行与验证

- Codex、ZCode、Cursor、Claude 可声明当前主控身份；原生候选按实际宿主能力筛选，独立 CLI 按项目配置执行。Codex 和 ZCode 有真实主控验收。
- T0–T3 为通用能力级别，S 为专有能力池；Low、Medium、High、Max 为独立的请求推理档位。Controller 判断规划深度，项目配置确定候选顺序；保留旧配置读取。请求档位不作为上游实际推理生效的证明。
- Claude Code、ZCode、Cursor、Codex adapter 使用其无人值守权限模式。Verifier 断连、超时或输出格式错误只重验未完成条目；合法 fail 进入修复流程。
- Worker 输入包含依赖产物和来源 attempt。交付产物、preread 或依赖输入变化时，对应旧验收失效，历史行和日志保留；交付未变则复用完成项。
- Analytics 任务详情区分历史结果、最近采集的当前验收窗口和有效性未知。它不监控采集之后的文件变化，当前内容是否变化仍由 ORX 在 status、run、verify 中检查。

## 安装与迁移

两个 CLI 独立安装，源码入口统一在 ORX。用户配置默认位于 `~/.orx/`，Analytics 数据默认位于 ORX 数据根的 `analytics/`。迁移不会把项目 `.orx/state.db` 搬进用户配置目录。

安装、备份、迁移和回退顺序见 [升级到 0.5.0](upgrade-0.5.0.md)。macOS 内部预览与正式渠道的签名和安装条件见 [App 安装指引](../tools/orx-analytics/docs/app-installation.md)。历史 0.4.x 发布说明和下载保留，旧 Analytics 仓库已归档。

## 验收与渠道

本版本的实际候选提交、测试结果、逐产物 SHA-256 和安装记录以 [收尾证据](diagnostics/0.5-closeout/README.md) 及其 candidate-acceptance.json 为准。Release 发布状态以远端实际标签和资产为准。

本次正式 Release 分发 ORX CLI 与 Analytics CLI 的 wheel、sdist 和来源/校验清单。
macOS App 继续使用 ad-hoc 签名的内部预览渠道，不随公开 Release 上传。
正式 App 的 Developer ID、公证及真实注销重登属于独立门禁；没有实测通过的项目不视为通过。终端代启动 Controller 和细粒度任务授权不在本版交付范围。
