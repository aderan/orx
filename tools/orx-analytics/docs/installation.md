# 安装、升级和卸载

orx-analytics 是 Python 3.11+ 的独立 CLI，运行时只使用标准库。
安装 Git 来源需要 Git 和网络；构建时会下载 setuptools 等构建依赖。
安装完成后的分析不需要联网，也不需要安装 ORX。

macOS 菜单栏应用（自带内置 Python 运行时的可安装 `ORX Analytics.app`）
的安装、升级、回退与卸载是独立流程，见
[app-installation.md](app-installation.md)。

当前包版本为 0.4.3，分发来源是本 GitHub 仓库。以下 `@main` 指主分支
最新提交；需要可复现安装时用完整 commit SHA 替换 `main`。尚未发布到
PyPI，不使用裸 `pip install orx-analytics` 作为本项目的安装方式。

## uv 工具环境（推荐）

先按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)
安装 uv，再运行：

```sh
uv tool install --python 3.12 'git+https://github.com/aderan/orx-analytics.git@main'
orx-analytics --version
orx-analytics --help
```

uv 会为工具创建隔离环境；`--python 3.12` 明确指定满足最低要求的解释器。
如果终端找不到命令，运行 `uv tool update-shell` 后重新打开终端。
工具环境与 PATH 的行为见 [uv 工具文档](https://docs.astral.sh/uv/guides/tools/)。

如果本机 Git HTTPS/代理连接不可用，且已经配置 GitHub SSH 身份，可用
同一仓库的 SSH 来源安装，不需要更改全局代理配置：

```sh
uv tool install --python 3.12 'git+ssh://git@github.com/aderan/orx-analytics.git@main'
```

升级仍跟踪 main 的安装：

```sh
orx-analytics watch stop
uv tool upgrade orx-analytics
orx-analytics --version
# 如果你需要长期观察，再启动（引擎未运行时会自动拉起）：
orx-analytics watch start
```

只有在你负责该 watcher 时才停止它。使用自定义数据目录时，所有 watch
命令也要使用同一个 `--data-dir` 或 `ORX_ANALYTICS_HOME`。从旧版独立
watcher 升级到引擎协作语义的识别与停旧起新流程见
[迁移指引](watch-migration.md)。
从固定 commit 切换到 main，或明确重装来源时：

```sh
uv tool install --reinstall --python 3.12 'git+https://github.com/aderan/orx-analytics.git@main'
```

卸载命令为 `uv tool uninstall orx-analytics`；先停止自己管理的 watcher。

## pipx 工具环境

已有 pipx 和 Python 3.11+ 时可以使用：

```sh
pipx install --python python3.12 'git+https://github.com/aderan/orx-analytics.git@main'
pipx ensurepath
orx-analytics --version
```

首次更新 PATH 后重新打开终端。升级用 `pipx upgrade orx-analytics`，
卸载用 `pipx uninstall orx-analytics`。详情见
[pipx Git 来源示例](https://pipx.pypa.io/stable/reference/examples.html)。
uv 和 pipx 选一种管理同一个命令即可。

## 普通虚拟环境

无需 uv 或 pipx，使用已有 Python 3.11+：

```sh
python3.12 -m venv "$HOME/.venvs/orx-analytics"
"$HOME/.venvs/orx-analytics/bin/python" -m pip install \
  'git+https://github.com/aderan/orx-analytics.git@main'
"$HOME/.venvs/orx-analytics/bin/orx-analytics" --version
"$HOME/.venvs/orx-analytics/bin/python" -m orxanalytics --help
```

这里的路径只是可替换的安装示例，工具不查找这个目录。直接使用命令的
绝对路径，或自行把该虚拟环境的 `bin` 加到 PATH。
升级用同一个环境的 Python：

```sh
"$HOME/.venvs/orx-analytics/bin/python" -m pip install --upgrade \
  'git+https://github.com/aderan/orx-analytics.git@main'
```

仅卸载包用该环境的 `python -m pip uninstall orx-analytics`。

## 从本地源码或 wheel 安装

```sh
git clone https://github.com/aderan/orx-analytics.git
cd orx-analytics
uv tool install --python 3.12 .
```

本地安装不会因源码变动自动更新；修改后重新安装。参与开发时用
[贡献指南](../CONTRIBUTING.md) 的 editable 环境。

维护者可以用 `python -m build` 生成源码包与 wheel。拿到构建好的 wheel
后，在目标虚拟环境中用 `python -m pip install --no-index --no-deps
/absolute/path/to/orx_analytics-0.4.3-py3-none-any.whl` 离线安装。构建工具与
源码包不是运行时依赖。项目的 `scripts/check_install.py` 会验证这个流程。

## 数据、配置和卸载后的保留

| 项目 | 位置与行为 |
|---|---|
| 默认分析数据 | `~/.orx-analytics/analytics.db`，包含项目登记、导入结果、游标、findings |
| 数据目录优先级 | `--data-dir` > `ORX_ANALYTICS_HOME` > `~/.orx-analytics/` |
| watch 状态 | 同一数据目录中的 `watch-demand.pid`、`watch-demand.state.json`（CLI 需求）与 `watch.state.json`（once 轮次）；`engine.sock`/`engine.lock` 属于协调引擎，`watch.pid` 只由旧版循环写入（见[迁移指引](watch-migration.md)） |
| macOS 通知 | `ORX_ANALYTICS_NOTIFY=0` 关闭；不影响采集与 findings |
| ORX 可选接入 | `ORX_ANALYTICS_BIN` 由 ORX Controller 读取，见[集成指南](orx-integration.md) |

路径参数使用绝对路径；全局 `--data-dir` 放在子命令之前。
升级会保留分析数据，首次打开可能迁移 analytics 自己的存储：当前
store 版本为 4，v1/v2/v3 的 `analytics.db` 会被原地进行增量迁移
（v1→v2 增补 `attempts.nonce` 镜像列，v2→v3 增补 `orx_receipts`
回执镜像表，v3→v4 为 `zc_requests` 增列镜像 zcode `model_usage` 的
逐请求 token 并重置 zcode 采集游标以幂等补齐——重复采集不重复计数；
既有行原样保留、不回填）；大于 4 的未知未来版本被
拒绝，不猜测、不降级。迁移只发生在 analytics 自己的数据目录里，
它从不迁移 ORX 或 agent 源库。需要回退时，升级前停止 watcher 并
备份整个分析数据目录；旧工具未必能读取新版本分析库，回退应恢复
相应备份。
卸载 Python 包不会删除分析数据或已导出的证据包。

## 安装故障

| 症状 | 处理 |
|---|---|
| Python 版本不满足要求 | 使用 3.11+，不要依赖 macOS 的系统 `python3` 版本 |
| 命令找不到 | uv 更新 shell PATH；pipx 使用 ensurepath；venv 使用绝对路径 |
| 版本没有更新 | 确认命令来自哪个工具环境；固定 commit 的安装不会跟随 main |
| Git 来源下载失败 | 检查 Git、网络及仓库访问；可以改用本地 wheel |
| probe 显示 GAP | 安装可能正常，缺少原生来源是能力缺口，见[兼容性](compatibility.md) |
| 较新 ORX schema 被拒绝 | 升级支持该契约的 analytics，不修改源库版本号 |

Python 包可独立安装；持续 watch 目前针对 macOS/Linux，Cursor 默认路径
和系统通知针对 macOS。不要把可安装等同于所有平台的所有来源都受支持。
