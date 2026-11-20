# ORX 0.5.x 用户配置根迁移

2026-11-20 在 Apple Silicon macOS 26.6.2 上验收。

## 构建与路径

- ORX CLI 源码 `43cbf1d0799f4d1ce60e32d0278c4b869055f2a2`，
  `orx_agent-0.5.0-py3-none-any.whl` 的 SHA-256 为
  `9041f9fb775ee4ec6083006f2143f2be8dea7fdf4ae8db41c4d6175e4a509019`。
  工作区构建与提交后重建逐字节相同，wheel 先在隔离环境验证，再通过
  `uv tool install` 安装到本机。原 0.5.0 wheel 保留。
- `orx config migrate-home --json` 将两个文件一起从
  `~/.config/orx/` 复制到新默认 `~/.orx/`。初次装载前两组文件逐
  字节对比通过。旧目录的原件继续可供回退。
- 同一 wheel 实测 `ORX_HOME` 指向另一个配置目录。CLI 路径报告及
  Swift 路由客户端均在那里找到用户配置和具名 profile。覆盖顺序由
  测试核对为 `ORX_CONFIG_DIR`、`ORX_HOME`、显式的
  `XDG_CONFIG_HOME/orx`、`~/.orx`。从家目录调用时没有项目
  配置。自定义配置根解析后仍报告原来的用户数据目录。

## CLI 和已安装的桌面 App

两者都通过 `~/.local/bin/orx` 使用新版 CLI。已安装的 0.5.0
内部预览 App 来自 `03c1ed05f89b0f9b2e2f7c59b27f8c5c09ce68c6`，
应用包严格验签通过。从 Finder 选中并冷启动该安装包，Launch
Services 报告 `com.aderan.orx.dev`，PID 为 69560。

CLI 以 `orx config set` 保存用户默认、ORX 项目、To2050 项目
的规划深度，再通过桌面使用的 `orx config routing` 接口回读。
每次仅目标配置产生变化，另外两处文件字节及生效深度一致。
CLI 验收的三次临时修改均恢复到各自原始文件。

另以两项目及用户层的配置副本，用生产 Swift 路由客户端和已安装
wheel 对三处执行读取、预览、保存、回读。预览时文件未写入，保存
只修改配置，三个 `profiles.toml` 校验一致。

已安装 App 启动后的用户层候选修改由 CLI 回读，确认
`plan.light.profiles` 保存于 `~/.orx/config.toml`，来源为用户层。
这次候选修改保留。ORX 与 To2050 的已安装 App 界面保存待复核。
本机桌面工具可从 Finder 打开安装包，接管窗口时把该包识别成
旧 Bundle ID `org.orxanalytics.macos.dev`。实际运行进程的内外
plist 和 Launch Services 均为 `com.aderan.orx.dev`。

## 数据核对和回退

- ORX 和 To2050 的 `.orx/state.db` 与项目 `profiles.toml`
  的 SHA-256 与基线一致。用户 `profiles.toml` 和
  `projects-index.json` 的 SHA-256 也一致。
- `~/.local/share/orx/analytics/` 的数据库与 socket 的文件
  标识一致。共享引擎 PID 48950 和原 CLI watch PID 48973
  保持连接，watch 继续在同一个 socket 续约。
- 需要恢复旧 CLI 时可重装原 0.5.0 wheel，并将 App 路由页的
  ORX 路径改回原独立安装目录。新版 CLI 也支持用
  `ORX_CONFIG_DIR="$HOME/.config/orx"` 验证保留的原件。

ORX Python 共 1,310 项通过，其中 29 项历史联合测试使用原独立
Analytics 检出，2 项依赖实际执行数据的诊断测试使用临时 SQLite
快照。ORX 独立 wheel 检查和 Swift 路由客户端 5 项通过。
本机可复验日志保存在工作区的 `dist/config-home-local/`。
