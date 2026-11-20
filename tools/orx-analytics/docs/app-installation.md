# ORX 安装、升级与回退（设计 §9）

App 交付物是自带 Python 运行时的 `ORX.app`：干净、无开发环境的
Mac（最低 macOS 13，Apple Silicon）无需预装 Python、uv 或 Homebrew。
应用复制到「应用程序」后，从 Finder 首次启动并确认 ORX 主窗口。系统允许
显示 ORX 时，菜单栏也会出现图标。系统隐藏图标时，从 Finder 双击 ORX
仍可打开主窗口，右上角的「应用选项」可打开设置或退出。
本文是安装、升级、回退、卸载与缓存的
唯一操作说明；CLI 安装见 [installation.md](installation.md)。

## 分发渠道（G011 T005：内部预览 ≠ 正式分发）

App 有两个互不混淆的分发渠道。**ad-hoc 签名的包只能是明确标注限制的
内部预览，绝不是正式安装包**；面向普通用户的正式安装包必须完成签名、
公证与安装验证。

| | 内部预览（internal-preview） | 正式分发（official） |
|---|---|---|
| 构建命令 | `apps/macos/scripts/build_app.sh dev` | `apps/macos/scripts/build_app.sh dist` |
| 应用名 / Bundle ID | `ORX (Internal Preview)` / `com.aderan.orx.dev`（独立 defaults 域） | `ORX` / `com.aderan.orx` |
| 归档 | zip（`…-internal-preview-dev-macos13-arm64.zip`） | DMG（`…-official-dist-macos13-arm64.dmg`） |
| 签名 | ad-hoc（无身份、未公证） | Developer ID Application ＋ hardened runtime（含内置 Python 与全部嵌套 Mach-O、安全时间戳、固定 entitlements） |
| Gatekeeper | 首次打开被拦，需下述手动确认 | 公证＋装订后直接放行 |
| 受众 | 内部测试（内部测试专用，不得对外分发） | 普通用户 |
| 前置决定 | 渠道与依据先固定在分发决定文件（`docs/diagnostics/g011/distribution.json`，ORX 仓） | 同左，且**必须**提供该文件并先通过凭据预检 |

渠道标识贯穿四个可见位置：App 的 `Contents/Info.plist`
（`ORXAnalyticsChannel` / `ORXAnalyticsChannelAudience` /
`ORXAnalyticsGatekeeperNotice` / `ORXAnalyticsSourceCommit` 完整提交）、
包内清单 `Contents/Resources/distribution.json`、归档文件名（含渠道
token）与逐归档构建记录（`<归档名>.json`，记录渠道/版本/完整提交/
sha256/门禁证据）。App 内「诊断 → 构建渠道」直接展示这些信息。

**无静默降级**：正式通道在凭据缺失或任一检查（嵌套签名、公证、装订、
安装评估）失败时立即以机器可读 JSON 拒绝并退出非零，不产出任何归档，
也绝不会把失败悄悄转成 ad-hoc 内部预览产物。两个渠道的通过/阻塞结果
在分发决定文件的 `results` 中**分槽记录**。门禁逻辑在
`apps/macos/scripts/app_distribution.py`（测试 `apps/macos/tests_python/test_app_distribution.py`）。

当前状态（实测，见决定文件 `credential_facts_probed_live`）：本机无
Developer ID Application 证书、无 notarytool 存储凭据——**内部预览可
构建（如实标注），正式通道按设计失败关闭**。解锁步骤见该文件
`channels.official.user_steps_to_unblock`。

版本号唯一来源是同仓 `tools/orx-analytics/src/orxanalytics/__init__.py`
的 `__version__`，构建时
写入 App 版本与 `Contents/Resources/orxanalytics-version.txt`。

构建脚本固定内置运行时（版本与 SHA256 写死在 `apps/macos/scripts/build_app.sh`）：

- python-build-standalone `cpython-3.12.8+20250115`（aarch64-apple-darwin，
  install_only），SHA256
  `e29003b69465c33692830032d9d237d84ea43a2e8461db9134641640fb49f040`。
- 首次构建下载到 `var/runtime-cache/`，之后离线复用；**每次构建都重新
  校验 SHA256**，不匹配即中止。
- App 的分析代码（`src/orxanalytics`，stdlib-only）随包复制进
  `Contents/Resources/orxanalytics`；引擎入口是包内
  `orx_engine_serve.py`（见下「引擎与进程」）。
- 构建末尾自测：用内置运行时在临时目录拉起引擎并确认 socket 监听后
  才产出归档（自测使用空的 `ORX_DATA_DIR`，不读任何真实会话）。

## 安装

### 内部预览（ad-hoc，仅内部测试）

1. 下载内部预览 zip（文件名含 `internal-preview`），解压后把
   `ORX (Internal Preview).app` 拖入「应用程序」。解压请用
   Finder（双击归档）或 `ditto -x -k <归档>.zip <目标目录>`：归档由
   ditto 生成、以 AppleDouble 条目携带扩展属性，命令行 `unzip` 会把
   这些条目落成 `._*` 杂散文件并破坏 ad-hoc 签名封条
   （`codesign --verify --strict` 会报 sealed resource 失效）。
2. 首次双击会被 Gatekeeper 拦截（ad-hoc 签名、未公证是本渠道的如实
   属性，不是待修复缺陷）。右键点 App 选「打开」确认一次；若仍被拦，
   在终端执行
   `xattr -dr com.apple.quarantine "/Applications/ORX (Internal Preview).app"`。
   **此绕过步骤只属于内部预览渠道，永远不得作为正式渠道的安装验证
   证据**（正式渠道的通过标准见下一节，与 quarantine 无关）。
3. 确认 Finder 能打开 ORX 主窗口；「诊断 → 构建渠道」会显示内部预览
   标注与完整来源提交。

### 正式分发（Developer ID ＋ 公证）

1. 下载正式 DMG（文件名含 `official-dist`；包名为 `ORX.app`，
   无任何预览字样）。打开 DMG，把 App 拖入「应用程序」。
2. 双击即可运行：包已 Developer ID 签名、公证并装订票证，Gatekeeper
   直接放行——**无需右键打开，也绝不需要清除 quarantine**。
3. 安装验证（可选，面向发布前检查）：
   `spctl --assess --type execute -vv "/Applications/ORX.app"`
   应输出 `accepted`（来源 `Developer ID`）。构建时已对副本做过同一
   评估并记录在构建记录里；清除 quarantine 不属于本渠道的任何步骤。

## 引擎与进程

- App 是引擎的生命周期管理者：socket 无应答时，用内置 Python 运行时
  启动 `orx_engine_serve.py <data-dir>`（获取单实例文件锁 → 绑定用户
  私有 Unix socket → 有界崩溃重启地运行调度器），等 socket 就绪后重试
  连接。协议版本不兼容时明确报告、不强行接管。
- **运行时封条（0.4.3 修复，v0.4.2 已披露缺陷）**：`makeChild` 启动引擎
  子进程时强制设置 `PYTHONDONTWRITEBYTECODE=1`，覆盖继承环境的任何冲突
  值（含 `0`、空串与未设置——在内置 CPython 3.12 下这三种状态都会写
  字节码）。v0.4.2 的安装后首启曾向已签名 `.app` 内写入 73 个
  `__pycache__` pyc，使 `codesign --verify --strict` 由 0 变 1（证据见
  ORX 仓 `dist/g011/gui-acceptance/seal-break-*`）；构建脚本的封条门禁
  只能保护构建期，运行期的封条只能由 App 自己守住。
- 引擎入口 `python -m orxanalytics.engine [--data-dir DIR]` 提供完整服务：
  单实例锁、socket server 和受监督的调度循环。App 与 CLI watch 使用
  同一服务协议；包内 `orx_engine_serve.py` 也组合相同的公开引擎 API。
- 退出 App 只释放自己的采集需求；引擎若仍被 CLI watcher 需要，会继续
  运行（设计 §7 App/CLI 共存）。注销或重启后引擎不自动恢复，下次启动
  App 时自动重新拉起。
- 引擎日志在 `<data-dir>/logs/engine-launch.log`（容量有上限）；默认
  data-dir 是 `~/.local/share/orx/analytics`，可在设置中显式选择其他目录。

## 安装后封条检查（可重复验证）

发布冻结候选或安装副本验证（面向发布前检查，日常使用不需要）：

```
python3 apps/macos/scripts/check_runtime_seal.py --app </path/to/App.app> --evidence-dir <DIR>
```

退出码 0＝封条完好；1＝封条破坏或缺证（含机器可读原因）；2＝用法错误。
证据目录（必须在 App 外）落盘 `seal-check-report.json`、启动前后两份完整
bundle 内容快照（每个文件的 SHA-256、symlink 目标、目录）、前后两次
`codesign --verify --deep --strict` 日志、两轮真实 App 启动日志。检查
流程：先严格验签并快照 → 以 App 自己的可执行文件拉起两轮（首启经
`makeChild` ＋ 重启内置引擎），每轮要求真实启动证据（引擎 socket 可
连接、引擎进程是所启动 App 的子进程且命令行指向该 bundle、引擎启动日志
出现在数据目录）。引擎就绪后继续观察 App 是否运行，提前退出时记录退出码并判为
失败。最后停止 App 与引擎，再次快照与严格验签。**任何** bundle
内容变化（新增字节码会单独点名）、任一次验签失败或启动证据缺失都必须
失败——bundle 完好不能替代启动证据。

诚实约束（检查永远不得用来掩盖缺陷）：不向子进程注入禁写变量，反而
刻意给 App 对抗值 `PYTHONDONTWRITEBYTECODE=0`（在内置 CPython 3.12 下
该值仍启用字节码写入，只有 `makeChild` 自己的强制能守住封条）；不直接
调用 Python 跑引擎（只启动 App 可执行文件这条生产路径）；不重新签名；
剥离 `ORX_ANALYTICS_ENGINE_PYTHON` 等开发重定向变量，保证检查的永远是
包内运行时；运行数据与日志全部写在 App 外（数据目录默认
`<evidence-dir>/runtime-data`，可用 `--data-dir` 另指）。

## 升级（保留数据）

1. 保留旧 App 和对应 CLI 安装包。确认旧版的登录项、通知授权、数据目录
   和项目登记，关闭旧版登录启动并退出。协调 CLI watch 与需要更换的引擎停止写入，
   用 SQLite backup API 创建独立快照，并检查 `PRAGMA integrity_check` 返回 `ok`。
2. 在「应用程序」中替换对应渠道的旧 `ORX Analytics` 副本。预览仍标注
   `Internal Preview`。启动后检查项目登记、历史用量、自定义目录、通知授权
   和实际登录项。按原设置恢复登录启动，确认只有一条生效记录。
3. **备份先于迁移**：App 每次启动引擎前检查
   `<data-dir>/app/last-launched-engine-version`。当已存在
   `analytics.db` 且即将运行的分析代码版本与上次不同，先用 SQLite backup API 将一致性快照写入
   `<data-dir>/backups/analytics-pre-<版本>-<时间戳>.db`（只保留最新
   3 份），备份成功后才允许新版本引擎打开数据库；备份失败则拒绝启动并
   在面板给出原因——绝不带着未备份的数据库跑迁移。
4. 同版本重启不产生新备份。0.4.4 起默认仅保留每项目最近一份成功快照；
   最新快照仍含源库保存的历史计划与执行，不再累积重复采集副本。

0.4.5 起 App 自动备份使用 SQLite backup API，包含已提交的 WAL 数据。
备份通过完整性检查后才进入 `backups/`。升级前先取消 App 登录启动、退出
App、释放 CLI watch 需求，并确认待替换引擎已停止。
仍有其他进程使用分析库时不要覆盖它。

### 0.5.0：从旧默认数据目录迁移

0.5.0 默认使用 ORX 用户数据根的 `analytics/`（通常为
`~/.local/share/orx/analytics/`），旧默认是 `~/.orx-analytics/`。
升级软件不会自动证明数据已搬迁。显式 `--data-dir`、
`ORX_ANALYTICS_HOME` 和 App 的数据目录覆盖仍会优先生效。

1. 用旧路径执行 `orx-analytics --data-dir "$HOME/.orx-analytics" watch status`，
   保存项目登记、历史 Run/尝试/用量数及当前引擎信息。退出 App、暂停登录启动，
   与其他使用者协调后，用同一 `--data-dir` 的 `watch stop` 停止自己管理的 watcher。
   确认该目录的所有写入者已经停止。若引擎仍运行，先核对其 PID 的完整命令行
   确实指向这个目录，再发送 SIGTERM 并等待退出；不能仅凭遗留 pid 文件杀进程。
2. 在所有客户端停止后执行下面的复制。它持有旧引擎锁，若引擎仍持锁则拒绝。
   用 SQLite backup API 读取已提交 WAL、校验完整性，保留旧目录和独立备份，
   再原子发布新目录。目标已存在时拒绝合并，先检查已有数据，不覆盖它。

```python
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import shutil
import sqlite3
import tempfile

old = Path.home() / ".orx-analytics"
new = Path.home() / ".local/share/orx/analytics"  # 自定义 ORX_DATA_DIR 时改为其 analytics 子目录
if not (old / "analytics.db").is_file() or new.exists():
    raise SystemExit("旧数据库缺失或目标已存在；请先核对路径，不覆盖数据")
if old.resolve() in new.resolve().parents or new.resolve() in old.resolve().parents:
    raise SystemExit("新旧目录不能互相包含；请先核对路径")
new.parent.mkdir(parents=True, exist_ok=True)
with (old / "engine.lock").open("a+b") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("旧引擎仍持锁；请先协调停止所有客户端和该目录的引擎")
    stage = Path(tempfile.mkdtemp(prefix=".analytics-move-", dir=new.parent))
    with closing(sqlite3.connect((old / "analytics.db").as_uri() + "?mode=ro", uri=True)) as src, \
         closing(sqlite3.connect(stage / "analytics.db")) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份完整性检查失败；旧目录保留，目标未发布")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(stage / "analytics.db", old.parent / f"analytics-before-050-{stamp}.db")
    runtime = {"analytics.db", "analytics.db-wal", "analytics.db-shm", "engine.sock",
               "engine.lock", "engine.state.json", "watch.pid", "watch.state.json",
               "watch-demand.pid", "watch-demand.state.json"}
    for entry in old.iterdir():
        if entry.name in runtime:
            continue
        if entry.is_symlink():
            raise RuntimeError(f"请先人工检查符号链接：{entry}；旧目录保留")
        if entry.is_dir():
            shutil.copytree(entry, stage / entry.name)
        elif entry.is_file():
            shutil.copy2(entry, stage / entry.name)
        else:
            raise RuntimeError(f"发现未识别的运行文件：{entry}；目标未发布")
    stage.rename(new)
```

3. 使用新安装的 CLI，显式指定新目录执行 `projects list`，与原登记核对。
   在 App「设置 → 数据目录」选择同一新路径；若原来设置了旧绝对路径覆盖，
   必须更新它，不能以新默认值已变化为由跳过。清理或更新自己设置的旧环境覆盖。
   启动新目录的 `watch start`（需要持续观察时），重开 App，再用同一路径的
   `watch status` 确认它们使用同一引擎和 socket。
4. 核对历史 Run、尝试、原生会话与用量，实际退出 App 后确认 CLI watch 仍续约。
   备份、旧目录和旧安装包暂时保留。这里不自动注销当前账户；登录启动的实际
   重登验收单独进行。项目 `.orx/state.db` 不搬迁、不写入。

回退时先协调停止新版写入，另存新目录，再恢复旧软件并重新指向保留的旧目录
或 `analytics-before-050-*.db` 一致性备份。不要把新旧引擎同时指向同一库，
也不要复制遗留 socket、PID、锁或 WAL/SHM 文件来冒充服务已启动。

## 回退（明确拒绝读取，不损坏数据）

- 降级安装到不认识现有 `analytics.db` 的 `store_version` 的旧版时，该版
  会明确报错拒绝打开（`store_version … != …; this reader cannot open
  it`），不会改写或损坏数据库。App 表现为引擎启动失败、面板提示原因，
  缓存数据继续展示。
- 处置顺序：① 重新装回能读取该版本的新版即可恢复使用；② 如需真正回退
  数据，先取消新版登录项并退出新版，协调 CLI 与引擎停止。保存升级后的
  库和恢复前快照。从完整性检查通过的快照恢复 `analytics.db`，再启动对应
  旧 App 和 CLI，核对历史与登录项。恢复期间仅允许一个版本打开数据目录，
  `store_version` 由程序管理。
- 回退永远只动 analytics 自己的数据库与备份目录，不触碰任何 ORX 项目
  数据（各项目 `.orx/state.db` 全程只读，见 ORX 仓
  `docs/observability-contract.md`）。

## 退出、卸载与缓存清理（各是独立操作）

- **退出**：菜单栏 → 退出，只结束 App 自身（见上「引擎与进程」）。
- **卸载**：把 `.app` 拖入废纸篓即可。卸载不会删除 `~/.local/share/orx/analytics`
  （analytics 数据）、任何 ORX 项目数据或用户级项目索引；想连数据一起
  清理需手动删除 `~/.local/share/orx/analytics` 目录。
- **清缓存**：设置里的「清理显示缓存」是单独、明确的操作，只清面板的
  展示缓存并回到空状态；它不删除 analytics 数据库，也不可能在退出或
  卸载时自动触发。

## 开发运行（非安装包）

- `swift run`（apps/macos，无内置运行时）下引擎自动拉起不可用，面板会
  提示缺运行时。开发替代方案：
  - 用 `apps/macos/scripts/build_app.sh dev` 产出内部预览版 App（渠道标注
    internal-preview）再运行；
  - 或设 `ORX_ANALYTICS_ENGINE_PYTHON`（Python 解释器路径）＋可选
    `ORX_ANALYTICS_ENGINE_SERVE`（默认在解释器同目录或上级目录找
    `orx_engine_serve.py`，如仓库根的 `apps/macos/Resources/` 下）＋
    `ORX_ANALYTICS_ENGINE_PYTHONPATH`（指向仓库 `src/`）。
- 冒烟：`apps/macos/scripts/smoke_macos_app.sh`（--smoke-test，合成引擎，无 GUI）。

## 已知限制（如实声明）

- **正式分发通道被凭据阻塞（按设计失败关闭，非待办缺陷）**：本机无
  Developer ID Application 证书、无 notarytool 存储凭据（实测记录见
  `docs/diagnostics/g011/distribution.json` 的
  `credential_facts_probed_live`）。正式构建的完整门禁链（嵌套＋外层
  Developer ID 签名、hardened runtime、公证、装订、spctl 安装评估）
  已实现并真实调用相应工具，凭据缺失时以机器可读原因拒绝、不产出
  归档、不降级为预览。解锁需用户完成该决定文件列出的两步配置。
- **内部预览的限制是渠道属性**：ad-hoc 签名、未公证、Gatekeeper 首开
  需手动确认——这些标注在 App 元数据、包内清单、归档名、构建记录与
  本文档中，不得当作正式安装包对外分发。
- 仅 Apple Silicon（arm64）构建；未做 Intel 通用二进制。
- 无自动更新框架：升级是手动替换安装包（设计允许）。
- `python -m orxanalytics.engine` 入口不绑定 socket（见「引擎与进程」），
  App 通过包内 `orx_engine_serve.py` 绕开；src/ 修复列入后续任务。


## 0.4.4 存储保留与自动发现

- `ORX_ANALYTICS_SNAPSHOT_KEEP` 为正整数，默认 `1`；在启动引擎的环境设置。
  每次成功导入自动淘汰该项目多余快照及全部快照明细；失败不会淘汰最后成功快照。
- 每来源保留最近 `max(100, SNAPSHOT_KEEP)` 条已结束采集日志，另保留最近 SNAPSHOT_KEEP 条成功记录和运行中记录。
  这些是诊断日志，原始用量、findings 和源库数据不受该保留策略影响。
- SQLite 删除后复用空闲页，文件不一定立即缩小。既有大库需要在 App 和共享引擎停止后
  清理冗余数据并执行 `VACUUM` 才能将空闲页归还文件系统；日常采集不反复执行压缩。
- 引擎启动及每 300 秒读取 ORX 用户级索引并合并 registry，自动登记存在 state.db 的新项目。
  `ORX_ANALYTICS_SCAN_ROOTS` 可选，以系统路径分隔符（macOS 为冒号）列出扫描根目录；
  扫描沿用深度 4、目录预算 5000、不跟随目录符号链接、不进入依赖目录的现有约束。
  不配置扫描根时只读索引与已登记项目。索引缺失、损坏或发现异常会在 health.discovery 中报告。

### 应用身份迁移

正式版从 `org.orxanalytics.macos` 迁到 `com.aderan.orx`。内部预览
从 `org.orxanalytics.macos.dev` 迁到 `com.aderan.orx.dev`。首次启动补入
同渠道旧版的数据目录覆盖、刷新频率、通知偏好和 ORX 可执行文件路径。
新版已有值优先，旧版偏好保留供回退。项目登记和历史用量继续从原数据目录读取。

替换前在「系统设置 → 通用 → 登录项与扩展」移除旧应用的登录项。
新版启动后可在 App 设置中重新开启登录启动。有旧通知偏好时，可在新版设置
点击「申请通知权限」，由 macOS 为新应用授予权限。回退时先注销新版的
登录项，退出并移走新版 App，再放回对应旧版。
