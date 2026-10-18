# T006 运行时封条修复与冻结候选验证记录

任务：落地 v0.4.2 发布说明已披露的运行时 seal 缺陷修复
（G012 T006；缺陷证据在 `dist/g011/gui-acceptance/seal-break-codesign.log`
与 `seal-break-pyc-files.txt`：安装后首启写 73 个 `__pycache__` pyc，
`codesign --verify --strict` 0→1）。

## 修复内容（clone 仓 .release-workspaces/orx-analytics）

- `apps/macos/Sources/ORXAnalyticsCore/EngineProcessManager.swift`
  `makeChild`：
  - 强制 `PYTHONDONTWRITEBYTECODE=1`，覆盖继承环境的任何冲突值（`0`、
    空串、未设置——内置 CPython 3.12.8 下三者都会写字节码）；
    `environment` 参数可注入，生产路径仍取 `ProcessInfo` 环境。
  - 顺带修复一个真实潜伏缺陷：`FileHandle(forWritingAtPath:)` 不会创建
    文件，全新安装（及容量轮转删除后）`<dataDir>/logs/engine-launch.log`
    从未落盘、引擎 stderr 静默进 nullDevice——文档承诺的引擎启动日志
    在首装机器上从未出现。现在缺文件时显式创建空文件再打开。
- `apps/macos/Tests/ORXAnalyticsCoreTests/EngineProcessManagerTests.swift`：
  `EngineProcessManagerRealChildSealTests`——真实子进程回归：可导入的
  临时 Python 模块经生产 `makeChild` 路径在三种敌对父环境（`0`/空/缺省）
  下导入，断言磁盘上零新增 pyc/`__pycache__`（非只查环境字典），并带
  无保护对照用例证明该解释器本来就会写字节码；另有首装日志落盘回归。
- `scripts/check_runtime_seal.py`：可重复的安装后封条检查（见
  `docs/app-installation.md`「安装后封条检查」）；`tests/test_runtime_seal.py`
  用 ad-hoc 签名的合成 .app 覆盖通过、新增字节码拒绝、预破签拒绝、
  缺启动证据拒绝与证据目录拒入 bundle 五类路径。

## 冻结候选实测（2026-10-18，本机）

- 构建：clone 仓 `scripts/build_app.sh dev`（源提交
  `f8c81b598f2912cad5726d9712f7bdfcc2390684`，含上述修复；版本号仍为
  0.4.2，版本提升属发布任务）。
- 安装副本：构建产物 ditto 到临时目录（模拟安装副本；未触碰
  /Applications 里用户正在运行的 0.4.2 预览与 `~/.orx-analytics`）。
- 检查命令：
  `scripts/check_runtime_seal.py --app <副本> --evidence-dir <本目录>/seal-check --launch-timeout 120`
- 结果：**verdict pass（退出码 0）**，证据在 `seal-check/`：
  - `seal-check-report.json`：两轮（first-launch、engine-restart）均取得
    真实启动证据——引擎 socket 可连接、引擎进程为所启动 App 的子进程
    （经 `makeChild`、命令行指向该 bundle 的内置运行时）、引擎启动日志
    出现在数据目录；启动前后 bundle 各 2000 条目，added/removed/changed
    全空；`codesign --verify --deep --strict` 前后均 0。
  - `bundle-before.json` / `bundle-after.json`：启动前后完整内容快照
    （每文件 SHA-256、symlink 目标、目录）。
  - `codesign-before.log` / `codesign-after.log`、`app-launch-1.log` /
    `app-launch-2.log`。
  - 检查对 App 施加的是对抗值 `PYTHONDONTWRITEBYTECODE=0`（该值在内置
    CPython 3.12.8 下仍启用字节码写入），封条完好只可能来自 App 自己的
    `makeChild` 强制。
- 运行数据说明：引擎沙箱数据目录（`seal-check/runtime-data/`，含
  analytics.db、engine.sock、日志）在验证后清理；启动证据本体持久记录于
  报告 JSON 的 `rounds[]`（engine pid/ppid/命令行、socket、日志路径）。
