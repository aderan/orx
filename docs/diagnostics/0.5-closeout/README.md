# ORX 0.5.0 收尾验收

2026-11-20，当前 Codex 会话主控，G013 / R013 revision 2。
两个 CLI 和 App 已统一为 **0.5.0**，本机个人使用的内部候选验收通过。
冻结产品提交 `4ff2040ae10f55e2c6dfbaab97eb67466586f488`；后续证据文档提交不改变候选来源。尚未创建 0.5.0 tag / Release。

机器清单见 [candidate-acceptance.json](candidate-acceptance.json)，产物和六项 SHA-256 见 [candidate.json](candidate.json)、[SHA256SUMS.txt](SHA256SUMS.txt)。最终本地候选在 `dist/0.5-closeout/candidate-final/`，原 `candidate/` 已被替代。

## 本轮交付

Analytics `tasks.get` 保留全部历史验收原始结果，新增逐行 `current=true/false/null`、原因和窗口摘要。窗口依据同一采集快照的最新 Worker 与 verification-context 水位；坏 context 不回退，缺依据保持未知，跨项目/Run/修订不串读。这证明采集时的 ORX 记录，不保证采集后文件未变。历史 first-pass 等指标口径未改。

桌面分别展示当前、历史和未知，兼容旧引擎。历史通过不冒充当前通过；Goal、Run、当前计划进度和全部历史用量的口径保留。当前版本、安装、升级和回退文档已整理，0.4.x 历史记录保留。

Claude sonnet 首轮计划遗漏读取端功能，任何 Worker 启动前已重规划。ZCode 完成 T001 / T002（260 / 262），Codex 独立审查通过（261 / 263）。文档首审 265 指出旧数据根迁移缺失，补齐可运行示例与回归后 267 通过。T004 最终证据由独立 Codex CLI 终审；终态另存 `run-final.json`，该文件不参与自身交付指纹。

## 最终检查

| 检查 | 结果 |
| --- | --- |
| ORX 全量 | 1,310 passed，258.39 秒 |
| Analytics | 278 passed |
| Swift + 真实 Python wire | 182 passed，0 skipped |
| App 打包 | 39 passed |
| 跨包真实 Store 对照与文档迁移 | 9 passed |
| 冻结联合构建、独立安装、实际包内引擎 | 14/14，退出 0 |

原始日志在 `dist/0.5-closeout/acceptance-logs/`。ORX 全量使用 runtime 源码逐文件一致的隔离副本；两项读取现场数据的历史诊断用一致性 SQLite 副本，29 项历史联合测试保留原检出依赖，不跳过。之前实库并发的快照竞争、文档章节边界及隔离副本缺历史依赖的失败均保留执行历史；修正后有单次 1,310 全绿结果，不以分组合计代替。

跨包测试使用真实 ORX 状态机生成库，与 `Store.verifications_current` 对照并验证源库前后 SHA-256 相同。迁移测试直接执行安装文档示例，覆盖 WAL、目标存在、存活引擎锁和嵌套目录拒绝。

## 真实四 CLI 依赖 Run

摘要见 [live-run.json](live-run.json)，原始响应、日志、SQLite 副本和小任务源码归档到 `dist/0.5-closeout/live-run/`。使用隔离配置/数据与独立安装的 ORX wheel，复用各 CLI 现有登录；不是新账户认证测试。当前会话逐步决定命令，没有终端 Controller 代启动。

| 角色 | 实际执行 | 秒 |
| --- | --- | --- |
| Planner | Claude Code sonnet / low，attempt 1 | 54.549 |
| T001 Worker | ZCode GLM-5.3 / low，attempt 2 | 30.127 |
| T002 Worker | Cursor Grok 4.7 / medium，attempt 3 | 108.178 |
| Verifier | Codex GPT-6.1 Sol / low，attempt 4 | 22.534 |
| 变化后复验 | Codex，attempt 5 | 20.587 |

实现 `money_core.total_cents`，再由依赖它的 `invoice_cli` 读取 JSON 并打印合计。三个预置测试/样例文件哈希未变。T002 通过 `dependency_artifacts` 得到 T001 / attempt 2 的 `money_core.py`，未将模块重复放入 preread。实际示例输出 **1900**、退出 0；测试覆盖非法布尔值、负数、非整数及缺失文件。Cursor 首次自测发现产品错误，在同一 attempt 修复，该红记录保留为历史。

四 CLI 均通过 adapter 的 YOLO / 等价参数启动，未见权限阻塞或超时。sonnet 按 Claude Code 别名使用，不以后台映射作为失败条件。effort 为请求/启动参数事实，`requested_validated` 不证明上游实际推理强度。

- 不改交付再次 verify：没有模型启动，当前验收行身份不变。
- 仅给前置模块加注释：status 检出 T001 产物及 T002 依赖失效；verify 使相关旧验收退为历史，重跑两项命令及所需审查。
- 最终 Goal / Run done，`evidence_freshness.stale=[]`，Worker 仍为 2 / 3。Analytics T001 当前 `[6]`、历史 `[1]`；T002 当前 `[7,8]`、历史 `[2,3,4,5]`，原始 passed 未覆写。
- 三次 Analytics 采集均验证源数据库前后哈希相同，使用最终候选 Analytics 安装。初轮和最终 ORX wheel 字节相同。

## 安装、桌面和数据

详见 [local-install.json](local-install.json)。默认 App 路径 `/Applications/ORX (Internal Preview).app`、`~/.local/bin/orx`、`~/.local/bin/orx-analytics` 已更新为共同候选。App plist 来源 4ff2040，严格深度验签退出 0，实际进程二进制路径已核对。旧 App、SQLite 一致性备份和 managed skills 原件可回退，skills 更新成功。

旧引擎停止后，新引擎 PID 73293、watch PID 73294 沿用原数据根。App 正常退出后引擎仍存活，需求续租成功。watch 顶层十月的旧 `database is locked` 遥测不是当前错误；新 engine / demand 正常且 demand.last_error=null。10 个项目登记及旧 44 个 Run、1,091 个 attempt、439 个 usage 业务身份保留。snapshot ID 按保留策略轮换，不能用它判断业务历史丢失。

同源候选的原生窗口显示 R013/T001 当前 2（通过 2）、历史 3、未知 0；旧引擎显示未知。两项目设置页分别 ORX standard→light→standard、To2050 standard→deep→standard，预览不写入，保存仅改目标项目。序列化会去掉注释；界面恢复语义后，主控恢复原文件字节，两份最终 SHA-256 与基线一致，不宣称 GUI 自己保留注释。此项补齐之前配置根验收的“双项目界面保存待核对”。

默认安装路径 App 已由 Finder 打开，但桌面工具仍将其解析为旧 Bundle ID，不能接管窗口。同源候选副本可正常操作。安装路径的进程/版本/签名与候选 GUI 是分开取得的证据，不冒称默认路径窗口点击通过。

生产 taskBreakdown 在临时复制中导出明暗 × 380/760 pt 四图及 legacy 窄宽图并目视核对；只替换导出入口/root body，保留生产布局，未改系统主题。图片在 `/tmp/orx-closeout-evidence-render-v2/` 与 `dist/0.5-closeout/render-legacy/`。

测试最初误登记本轮自己的 17 个临时项目；备份后仅移除这些确切登记，源库未删除。跨包测试加入配置/数据隔离，9 项重跑通过且不再污染登记。孤立镜像历史未清除。App 历史列表恢复为原 38 条加 R013，共 39 条。用户原有五份未提交文档逐字节保持。

## 发布边界

旧 Analytics 仓库仍 archived=true，历史标签未移动，ORX main 的同仓源码切换已上线。[旧仓事实](legacy-repository.json)、[Release 列表](releases-before.json) 已重新读取；历史下载证据保留在[切换记录](../analytics-monorepo/source-cutover.md)。

个人内部版可用。以下未冒报完成：

- 实际注销/重登后登录项启动：没有注销当前用户。
- Developer ID / 公证：当前为 ad-hoc 内部预览，不是正式对外安装包。
- 0.5.0 tag / Release 与远端资产复算：未发布。后续应分发已冻结产物，不能重建后继续沿用本清单哈希。

日常使用继续当前会话主控，不引入 LiteLLM，不增加终端代启动。
