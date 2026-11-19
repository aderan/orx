# 与 ORX 的可选集成

ORX 负责执行控制，orx-analytics 负责观察和分析。二者独立安装。
安装 analytics 不修改 ORX，也不启动 watcher；ORX 日常执行不依赖它。
组织方式见 [ADR-0001](adr/0001-independent-repository.md)。

## 单向只读连接

登记需要观察的项目，然后采集：

```sh
orx-analytics projects add /absolute/path/to/project
orx-analytics collect --skip-global --json
orx-analytics report /absolute/path/to/project --run R001
```

ORX 项目必须已经由 ORX 初始化并具有受支持的 `.orx/state.db`。
analytics 自己不会初始化或升级它。没有 `.orx` 的项目可以登记为原生
会话项目，但不能凭空生成 ORX Run 报告。

analytics 通过 `mode=ro` 与 `query_only` 读取源库，遵循 WAL 和命名列
契约；只向自己的数据目录写导入、快照和分析结果。不要用 ORX 的
`Store.open` 代替只读读取，因为 ORX 打开自己的旧库可能触发迁移。
工具也不需要依赖 ORX Python 包。

## 向 Controller 声明已安装的工具

当前 ORX Controller skill 只在显式配置时尝试 watchdog：

```sh
export ORX_ANALYTICS_BIN="$(command -v orx-analytics)"
```

确认命令在 PATH 上且已安装后使用上述命令；也可以把变量设为工具的
绝对路径。这里声明的是工具入口，不是仓库目录，不要求相邻 checkout。
数据目录另由 `ORX_ANALYTICS_HOME` 或 `--data-dir` 指定。

未配置、未安装、启动失败或不支持当前来源时，Controller 应说明一次
并继续原有 Goal 流程。analytics 只能提供观察信号，不是调度状态权威。

## watcher 的所有权

推荐由操作者统一启动跨项目的长期采集需求（新版 `watch start` 向协调
引擎注册需求，引擎未运行时自动拉起，见[迁移指引](watch-migration.md)）：

```sh
orx-analytics watch start
orx-analytics watch status
orx-analytics findings list
```

同一分析数据目录已有运行实例时，`start` 不应再启动第二个。
Controller 只可清理本会话实际启动的 watcher；已经存在的实例属于
操作者或其他会话，单个 Goal 完成时不能停止它。升级工具时由负责该
实例的操作者执行停止、升级和按需重启，后台进程不会自动载入新代码。

## 从发现到执行

`findings draft <id>` 只输出本地草案，`bundle run` 只导出调查证据。
操作者可检查它们，再把明确的目标交给 ORX。当前没有自动创建 Inbox、
自动发布 Issue、自动恢复会话或自动修复的连接；新增这些连接必须
另行定义接纳、授权和幂等规则。

## 项目索引消费契约（ORX 用户级项目索引）

ORX（G002/T004 起）在每次 `orx run` 派发执行时，向用户级项目索引
登记当前项目；本工具只读消费该索引做自动发现，状态与用量仍以各项目
`.orx/state.db` 为唯一事实源。

**位置解析**（与 ORX `user_data_dir()` 一致）：

```
ORX_DATA_DIR > XDG_DATA_HOME/orx > ~/.local/share/orx
```

索引文件为 `<数据目录>/projects-index.json`，锁文件
`projects-index.lock`（消费方忽略，不获取）。

**格式**（`index_version` 目前为 `1`）：

```json
{
  "index_version": 1,
  "updated_at": "<iso8601 utc>",
  "projects": {
    "/canonical/path": {
      "canonical_path": "/canonical/path",
      "name": "<目录名>",
      "first_seen_at": "<iso8601 utc>",
      "last_seen_at": "<iso8601 utc>",
      "last_run_id": "R002 | null",
      "has_state_db": true
    }
  }
}
```

**消费规则**：

- 键即 canonical path（ORX 侧已做符号链接解析）；消费方合并发现来源
  时按 `Path(...).expanduser().resolve()` 归一后对齐，不凭目录名认定
  同一项目。
- `has_state_db=false` 的项目只做基础会话分析，不采集 ORX 快照。
- `last_run_id` 只是提示，不是身份键；Run 身份=Project+run_id。
- 消费方只读：不写、不删、不加锁；文件可能不存在（旧版 ORX）——
  此时自动发现范围受限，应显式提示而非报错。
- `index_version` 不认识（未来版本）或 JSON 损坏时：ORX 写侧会拒绝
  改写该文件，读侧应跳过索引来源并报告数据健康（版本不支持/来源
  不可读），不影响 registry 已登记项目的采集。
- 索引登记失败在 ORX 侧只留记录、绝不阻塞执行；因此消费方不能假设
  索引完备——项目在索引缺失而 registry 在册时以 registry 为准。

## 发现层如何消费这些来源

`orx-analytics discover` 合并三个来源，键一律是 canonical path
（`Path(...).expanduser().resolve()`）：

1. ORX 项目索引（只读，规则见上一节）；
2. 本仓 registry（`projects add` 的手动登记也在其中）；
3. 用户指定工作目录的有界扫描（`--scan ROOT`，可重复）。

合并与呈现规则：

- 同名目录不合并；项目移动通过显式 `relocate_project`（Python API）
  重新关联历史，绝不凭目录名自动认定。
- 索引缺失（旧版 ORX）时降级提示“自动发现范围受限”，registry 与扫描
  来源不受影响；索引损坏或 `index_version` 不认识时只隔离索引来源。
- 项目删除、目录不可读或 `.orx/state.db` 无法打开时保留历史快照，
  数据健康显示“来源不可读”；schema 版本不在白名单
  （`probe.SUPPORTED_ORX_SCHEMA`，当前 {8..15}）时显示“版本不支持”，
  均不崩溃、不清历史。
- ORX v13/v14/v15（G010 生命周期管理，2026-10-16）为纯增量修订：
  `goals.status` 新增 `paused`/`cancelling`（展示为“已暂停”/“取消中”，
  均非终态），v15 新增 `completion_receipts` 回执表。镜像仅取回执事实
  （digest/身份/时间），不镜像 `receipt_json` 重放负载——回执是给补交
  方的重放面，分析层只读不重放。Goal 状态始终读 `goals.status` 原文，
  不从 run 聚合推导；paused/cancelling 属未完成态（占用唯一 Goal 槽位），
  本工具不据此获得任何调度或迁移权限。
- `discover` 本身只读；`--register` 才把未登记的发现结果显式写入
  registry（等价于逐个 `projects add`）。

有界扫描的边界（`discovery.scan_for_projects`）：

- 深度上限（默认 4 级）与目录预算（默认 5000 个）双限制，超出即停止
  并报告 `truncated`；不做全盘扫描，不扫描未指定的根。
- 剪掉依赖与产物目录（`node_modules`、`.venv`/`venv`、`__pycache__`、
  `target`、`build`、`dist`、`Pods`、`DerivedData` 等）与 `.git` 整体，
  不进入其内部。
- 目录符号链接一律不跟随：符号链接循环不可能发生，扫描也不会经链接
  逃出指定根。
