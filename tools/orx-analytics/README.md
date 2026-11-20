# orx-analytics


跨项目、跨 coding agent 的只读执行观测：ORX（zcode/Codex/Cursor 派发）
与非 ORX 项目（zcode/Codex/Cursor 原生会话）的用量、耗时、交付质量、
数据覆盖、异常信号、证据交接与改进闭环。

适用于想了解“任务花了多少、哪里返工、是否停摆、结论依据是什么”的
个人开发者。所有采集和分析在本地执行；安装后不需要 ORX Python 包，
也不需要调用模型或供应商计费接口。

**它是 ORX 仓库中可独立安装的工具。** ORX 保存执行事实并控制 Goal 的推进；
orx-analytics 通过只读契约消费这些事实，同时读取原生 agent 会话。
分析结果只生成信号和本地草案，恢复与修复由操作者或获授权的 Controller
决定。仓库与包的边界见 [ORX 同仓架构决策](../../docs/adr/0001-orx-monorepo-independent-packages.md)。

| 能力 | 现在提供什么 |
|---|---|
| ORX 执行分析 | Run 的交付质量、用量、完成时间、首次通过率、返工、覆盖率 |
| 原生来源采集 | ZCode SQLite、Codex JSONL、Cursor SQLite，保留来源和缺口 |
| 原生会话报告 | `session` 和 `bundle session` 当前支持 ZCode 会话树 |
| 持续观察 | 本地 watch、异常规则、finding 去重；macOS 新发现通知 |
| 调查交接 | 自包含证据包、SHA-256 清单、离线复算脚本 |

不同来源的读取深度和平台支持不同，见[兼容性与来源限制](docs/compatibility.md)。

设计不变量（docs/metrics.md、docs/correlation.md 为冻结契约）：

- 源库只读（`mode=ro` + `query_only`），永不迁移/检查点/改写；
- 未知保持未知：缺失不补零、不插值、不升级；部分覆盖发布分母；
- 关联只有权威（ORX 写入的 `session_ref`）与推断（带依据+置信度）两类；
- 无计费依据，不算费用；
- 发现只生成本地草案，不自动发布、不自动修复。

## 安装

需要 Python **3.11+**。推荐通过 [uv](https://docs.astral.sh/uv/getting-started/installation/)
安装到独立工具环境；Git 来源安装还需要 `git`：

```sh
uv tool install --python 3.12 'git+https://github.com/aderan/orx.git@main#subdirectory=tools/orx-analytics'
orx-analytics --version
orx-analytics --help
```

当前开发线为 **0.5.x**，Analytics 与 ORX 随同一个仓库迭代。
需要复现时将 `@main` 换成完整提交。旧仓 Git 安装用户需显式重装到
新地址。[历史 Release 和下载](https://github.com/aderan/orx-analytics/releases)
继续用于旧版安装和回退。
pipx、普通 Python 虚拟环境、PATH、升级和卸载见[完整安装指南](docs/installation.md)。

运行时零第三方依赖（标准库）。数据目录默认 `~/.local/share/orx/analytics/`，可用
`--data-dir` 或环境变量 `ORX_ANALYTICS_HOME` 覆盖；优先级为命令行 >
环境变量 > 默认值。全局选项应放在子命令之前：

```sh
orx-analytics --data-dir /path/to/analytics-data projects list
```

## 首次使用

```sh
# 1) 探测本机数据源（诚实报告能力与缺口，不伪造）
orx-analytics probe --project /path/to/orx-project

# 2) 登记项目（绝对路径为身份；无 .orx 的项目自动按基础会话分析）
orx-analytics projects add /path/to/project
orx-analytics projects add /path/to/another --name Climbo

# 3) 采集一轮（全局源 + 各 ORX 项目快照；可重复，天然键幂等不重复计数）
orx-analytics collect

# 4) 看报告
orx-analytics report /path/to/orx-project --run R003      # 六读数+会话侧
orx-analytics session sess_xxxx                            # 原生会话报告
orx-analytics provenance                                   # 数据从哪来
```

## 日常持续运行

```sh
orx-analytics watch start --interval 600   # 后台守护：采集→关联→异常→发现
orx-analytics watch status                 # 轮次/最后错误/发现数
orx-analytics watch stop
orx-analytics watch once                   # 手动单轮（不驻留）
```

- 增量：zcode 按行号游标、Codex 按文件字节游标（断行安全：撕开的尾行
  下轮续读）、Cursor 按 composer 更新时间；ORX 每轮追加一个快照。
- 恢复：单轮失败记录在 `watch.state.json` 的 `last_error` 并继续；游标
  保证恢复后不漏算不重算（回归测试覆盖）。
- 有界开销：每轮只处理增量；异常规则只查当前快照与已导入行。
- 通知：本轮**新增**的 finding 发一条 mac 通知（confidence=low 的
  自我存疑行不叫，如 no_progress 的"可能是思考时间"）；指纹去重保证
  同一 finding 只在首次插入时通知。`ORX_ANALYTICS_NOTIFY=0` 关闭。
- 停摆即报：`fast_fail_stall` 规则盯"会话最新 turn 是零 token 错误
  turn"（实测签名：1 请求/0 retry/0 token/0 工具）——R006 2.48h 与
  To2050 2h47m 两次停摆同款。no_progress 要等 1h 间隔才够得着，这条
  下一轮就报（默认间隔 10min，另加该轮处理耗时）。

watch 是跨项目的长期观察进程，建议由操作者统一启动和管理。升级前先停止，
升级后按需重新启动；某个 ORX Goal 完成不意味着应停止共享 watcher。
可选 ORX 接入与生命周期规则见[集成指南](docs/orx-integration.md)。

## 异常调查

```sh
orx-analytics findings list [--project /path]
orx-analytics findings draft <id>     # 结构化 issue 草案（本地）
orx-analytics findings dismiss <id> --note "..."
```

规则（命中与不命中都有测试）：用量离群（同 role/profile
基线，样本不足不判定）、重复失败、短窗重规划、长无进展、快失败停摆
（零 token 错误 turn 且为会话最新 turn）、覆盖缺口
（未评估用量/未关联会话）、同 attempt 检查轮超限、声明边上的疑似重复
交付（仅 rev1 已 passed 的声明 redo 触发，且提示需核对范围）。
每条发现带触发依据、比较基线、置信度、待补证据——是信号，不是结论。

## 交接（把疑点交给任意 agent）

```sh
orx-analytics bundle run /path/to/project --run R003 --dest mybundle \
  --title "..." --objective "..." \
  --fact "..." --hypothesis "..." --excluded "..." --next "..."
orx-analytics bundle session sess_xxxx --dest mysessbundle ...
```

包内容：`HANDOFF.md`（一页：目标/已确认事实/假设/反例/已排除/下一步）、
`OPERATOR.md`（继续调查指引）、`data/*.json`（结论所依赖的全部行）、
`recompute.py`（仅凭包内数据重算全部 headline，篡改即 MISMATCH）、
`integrity.json`（SHA-256 清单）、`bundle.json`（指标/定义/覆盖/provenance）。
接收方不需要本机任何路径。

## 故障处理

| 症状 | 处理 |
|---|---|
| `probe` 报 GAP | 按输出补齐（如 Cursor bubbleData 为空是本机事实，报告会标注） |
| collect 报 `schema gate refuses` | 仅支持 ORX schema {8,9,10,11,12,13,14,15}。较新库需升级 analytics；较旧库由 ORX 按自己的升级流程处理，analytics 绝不代为迁移 |
| watch `last_error` 非 null | 读 `provenance` 看哪个源；单源失败不阻塞其他源 |
| 关联 `ambiguous` | 多个会话命中同一 attempt 标记，或标记命中但会话创建时间在 attempt 跨度 ± 300s 之外（过期标记/证据血缘），或提示词角色声明与 attempt 角色不符（verifier 引用 worker 编号）；查看 links.evidence 候选与窗口数字，人工裁决 |
| 想重算历史 | 每轮 ORX 快照 append-only：`report` 永远用最新快照，旧快照数据仍在库中 |

## macOS App 与分发渠道（G011 T005）

菜单栏 App（自带 Python 运行时，最低 macOS 13 / Apple Silicon）有两个
互不混淆的分发渠道，构建与安装说明见
[docs/app-installation.md](docs/app-installation.md)：

- **内部预览**（`../../apps/macos/scripts/build_app.sh dev`）：ad-hoc 签名、未公证的
  zip，App 名 `ORX Analytics (Internal Preview)`，渠道/受众/Gatekeeper
  限制与完整来源提交标注在 Info.plist、包内 `distribution.json` 清单、
  归档名与逐归档构建记录里。**它是明确标注限制的内部预览，不是正式
  安装包。**
- **正式分发**（`../../apps/macos/scripts/build_app.sh dist`）：要求 Developer ID
  Application 签名（含内置 Python 全部嵌套可执行文件）、hardened
  runtime、notarytool 公证、stapler 装订与 spctl 安装评估；凭据缺失
  或任一检查失败即**失败关闭**（机器可读原因、不产出归档、绝不静默
  降级为 ad-hoc 预览）。

渠道与依据在构建前固定在 ORX 仓的分发决定文件
（`docs/diagnostics/g011/distribution.json`）中，两个渠道的通过/阻塞
结果分槽记录；门禁逻辑在 `scripts/app_distribution.py`，测试
`tests/test_app_distribution.py`。

## 仓库布局

```
src/orxanalytics/   probe/dbutil/store/registry/importers/correlate/
                    metrics/bundle/findings/anomalies/watch/collect/cli
docs/               metrics.md correlation.md source-probes.md（冻结契约与探测基线）
tests/              合成来源回归、CLI 入口、接收方复算与篡改检测
scripts/            源码包与独立 wheel 安装验收
legacy 对应物        orx_analytics.py（起点脚本，保留为对照口径）
```

安装和开发入口见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变化见
[CHANGELOG.md](CHANGELOG.md)。包构建配置在 `pyproject.toml` 和 `MANIFEST.in`；
`scripts/check_install.py` 检查源码包→wheel→干净环境安装的完整流程。
CI 在 Linux 与 macOS 上运行回归和安装检查。

ORX 侧配套（G006，ORX 仓库 commit 852cd0a）：`orx task claim
--discover-session`、`orx task session-discover`、park 不再错盖控制器
会话、complete/fail 晚绑定 `--session`。真实端到端证据：R005
（attempt 101 ↔ `sess_subagent_agent_371a9db1…`，stored 权威关联）。

0.4.4 修复空闲项目重复快照并默认每项目保留一份成功快照；保留配置、日志边界与自动项目发现见 [App 安装文档](docs/app-installation.md#044-存储保留与自动发现)。
