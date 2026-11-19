# 阶段三验收

ORX 0.4.1、Analytics 0.4.5、ORX App 0.4.5。桌面只读预览为
`69d1e40`，编辑、历史记录与应用身份为 `4a9d374`，分析 store 并发修复为
`2346fc7`，Finder 重开为 `cbbcbef`。迁移分支合入已提交的
`5c85be6`、`c82d258`，使用现有 T0–T3 与 S 字段。原主工作区的未提交文档保留。

- [阶段三候选](stage-3-candidate.json) 从 `2346fc7` 构建，安装、Swift、签名、App 冒烟和当前组合端到端均通过。ORX 可独立运行测试 1,198 项，Analytics 256 项，Swift 167 项。
- 在独立安装的 ORX 0.4.1 wheel 上，用最小 `PATH=/usr/bin:/bin`、无项目 cwd 和指向 A 的 `ORX_PROJECT` 请求 B。读到 B 的 deep，预览 standard 时文件字节一致，保存后 B 的候选顺序与深度更新，A 仍为 light，B 的状态库原样。CLI 回归另覆盖用户默认、非法 profile、多值保存拒绝和外部修改导致的过期草稿。
- Swift 测试覆盖从用量携带准确项目路径、两项目同名 Run、Finder 风格可执行文件查找、ORX 缺失与旧版。菜单、通知入口和用量跳转共用已有的单窗口路由。真实 Finder 启动隔离副本后，临时目录出现正确引擎 socket 与 App 缓存。电脑操作工具无法接管无常规窗口的菜单栏应用，未取得任务详情到配置页的逐屏交互证据。Finder 重开提交单独通过 Swift 客户端测试和签名 App 冒烟，后续发布候选须重跑全量。
- 历史详情使用现有快照里的当次模型事实。已关联的路由日志新增只读镜像，旧引擎缺失该可选字段时仍可读取。Analytics store 保持 4，IPC 保持 1。新增镜像初次建表触发的并发登记问题已修复。新版本并发登记通过，未知 store 拒绝后字节一致。

## Finder 与配置逐屏验收

从冻结提交 `f548452f4f5511d327e5f79ca1fec6b393196200` 复用真实
App 的内置资源和 Swift 代码。为隔离本机的旧菜单栏记录，只重新链接
临时 Bundle ID，并对该副本验签。该副本、项目 A 和 B、ORX 0.4.1
安装及 Analytics 0.4.5 数据均为临时验收环境。

- 从 Finder 启动后再次打开包，进入唯一的 ORX 主窗口。三个页面均可见，
  执行列表同时显示 A 和 B 的 `R001`。
- 打开 B 的旧 Revision 中有用量的任务，显示 6,000 输入、600 输出、
  4,500 缓存、2/2 次尝试及历史请求模型与 profile。从任务链接进入
  「模型与路由」，目标文件为 B 的 `.orx/config.toml`。界面预览
  `standard → deep` 后才允许保存。保存后 B 显示 `deep`；
  A 保持 `light`，A 配置和 A、B 的 SQLite 源库 SHA-256 不变。
- B 预览 `deep → standard` 后，从应用外追加一条注释。保存被 ORX
  拒绝并提示 `configuration changed since it was loaded; reload and try again`。
  保存按钮禁用，外部编辑后的文件逐字节一致，B 仍为 `deep`。
- 将临时 App 的 ORX 路径切到不存在的文件，路由页显示缺失原因，
  「用量分析」仍列出 A 和 B 及各自的 416,054 token。恢复测试 CLI 后
  能继续加载路由。
- 清理后 A、B 的配置和源库共四个文件均与试验前 SHA-256 一致，
  原显示缓存已恢复。预览渠道原身份的启动与发布状态见
  [阶段四](stage-4.md)。
