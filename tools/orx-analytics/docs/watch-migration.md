# 旧版独立 watcher 迁移指引（watch → 协调引擎）

- 状态：现行。随 G002 设计 §7（`docs/menubar-app-design.md`）的进程协
  作改造引入；`watch start` 的语义从“启动独占采集循环”变为“向协调引
  擎注册持续采集需求”。
- 适用版本：引入引擎协调之前的所有 orx-analytics（≤0.4.x 的
  `watch start`，独立循环进程）。
- 现网已知活体（2026-10-16 记录）：`~/.orx-analytics` 数据目录下
  `watch.pid` 指向 pid 98698，600 秒轮询的旧版独立 watcher。

## 新旧语义对照

| | 旧版（≤0.4.x） | 新版（引擎协作） |
|---|---|---|
| `watch start` | fork 独立循环进程，独占采集 | 确保引擎在运行（未运行则拉起），再 fork 一个分离的“需求持有进程”，通过引擎 socket 持续续期 CLI 自己的 background 采集需求 |
| `watch status` | 报告自身循环轮次 | 报告引擎状态（运行/协议版本/调度层）＋ CLI 自身需求（pid、最近续期）＋ 旧版 watcher 检测结果，并保留 `once` 轮次状态字段 |
| `watch stop` | 向 `watch.pid` 中的 pid 发 SIGTERM | 只释放 CLI 自己的需求（引擎与其他客户端不受影响）；当检测到活体旧 watcher 时，才是“用户确认后的停旧”入口 |
| `watch once` | 同步单轮 | 不变：同步单轮（隔离测试路径），导入为幂等 upsert |
| `--interval` | 循环间隔 | 仅对 `once` 的休眠有意义；采集节奏由引擎分级调度（约 30/60/300 秒，G012 调低默认频率）决定 |

新版文件布局（同一数据目录）：

| 文件 | 含义 |
|---|---|
| `watch.pid` | **只由旧版循环写入**；新版只读取它做检测与迁移，绝不写入 |
| `watch-demand.pid` / `watch-demand.state.json` | 新版需求持有进程的 pid 与续期状态 |
| `engine.sock` / `engine.lock` / `engine.state.json` | 引擎的本地 socket、单实例文件锁、崩溃诊断 |

## 如何识别旧版 watcher

满足以下全部条件即是旧版独立 watcher：

1. 数据目录存在 `watch.pid`，且其中 pid 存活（`watch status` 的
   `legacy_watcher` 字段非空即表示引擎/新版 CLI 已检测到）。
2. `ps -ww -p <pid> -o command=` 的完整命令行同时包含
   `orxanalytics.watch` 与 `_loop`（旧版循环的固定调用形态：
   `python -m orxanalytics.watch _loop --data-dir … --interval …`）。

两条都要看：pid 存活只说明“有个进程”，命令行签名才说明“它是我们的
watcher”。pid 复用会让陈旧的 `watch.pid` 指向无关进程，这是下述“绝不
盲杀”规则存在的原因。

## 迁移步骤（经用户确认后停旧起新）

以下操作**全部由用户显式执行**，工具不会自动替代任何一步：

```sh
# 1) 识别：确认检测到旧 watcher，并记下 pid
orx-analytics watch status
#    → "legacy_watcher": {"detected": true, "pid": 98698, "guidance": …}

# 2) 人工核对命令行签名（例：现网 98698）
ps -ww -p 98698 -o command=
#    → 应当包含 “orxanalytics.watch … _loop …”

# 3) 停旧（唯一会发信号的路径，且仅在签名匹配时执行）
orx-analytics watch stop

# 4) 起新：拉起引擎（如未运行）并注册 CLI 持续采集需求
orx-analytics watch start

# 5) 验证
orx-analytics watch status
#    → engine.running=true、engine.compatible=true、demand.held=true、
#       legacy_watcher=null
```

自定义数据目录时，所有命令使用同一 `--data-dir` 或
`ORX_ANALYTICS_HOME`（与既有约定一致）。

## 绝不自动盲杀

- `watch start` 与 `watch status` 永远不向任何进程发信号；检测到旧
  watcher 时只报告与迁移指引。
- `watch stop` 是唯一发信号的入口，且只处理自己数据目录 pidfile 中的
  pid；发信号前必须通过 `ps` 命令行签名校验（旧循环：`orxanalytics.watch
  _loop`；需求持有进程：`orxanalytics.watch _hold`）。
- 签名不匹配、命令行不可得或 pid 已被复用时，`stop` 明确拒绝并输出
  `ps` 复核命令，由用户人工确认后处理；pidfile 原样保留供检查。
- 引擎侧同样只报告（`health` 的 `engine.legacy_watcher` 字段），从不
  主动终止旧 watcher。

## 与 App 的共存与退出

- App 与 CLI watch 连接同一引擎，各自持有独立的需求（连接级租约）。
- `watch stop` 只释放 CLI 自己的需求；App 打开面板的采集不受影响，
  反之退出 App 也不会停止 CLI 要求的采集。
- 引擎不因 CLI 需求释放而退出：空闲时按约 60 秒低频巡检。需要完全
  停止引擎时，从 `engine.lock` 读取持有 pid，按上面的签名思路人工
  确认后停止（`engine.main` 进程）。

## 迁移期间的数据安全

- 所有导入都是按自然主键的幂等 upsert，ORX 每轮快照为追加式审计记
  录、读取模型只取每个项目最近一次成功快照；同一数据被新旧两个采集
  方重复导入也不会重复计数（有幂等回归测试覆盖：重复采集、刷新、引
  擎重启后 zcode 用量合计、ORX 快照用量与任务消耗均不变）。
- 因此迁移窗口内旧 watcher 多跑几轮不会污染数据；仍建议尽快完成迁
  移，避免双采集开销与重复的异常通知。

## 回退

回到旧版行为没有自动路径：`watch stop` 释放新版需求后，用旧版本工具
重新执行其 `watch start` 即恢复旧循环（它写入 `watch.pid`，新版与引
擎会再次把它识别为待迁移的旧 watcher）。跨版本切换前先停掉当前一侧，
不要让两个采集方长期并存。
