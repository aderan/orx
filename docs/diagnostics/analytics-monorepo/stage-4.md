# 阶段四安装与回退证据

[本地内部预览清单](preview-candidate.json) 冻结于
`912a1840ab9b7b726a94439ef18ee8b113e3f03a`。ORX 0.4.1，Analytics 0.4.5，
App 0.4.5，ORX schema 15、Analytics store 4、IPC 1。候选的四份 Python 产物、
App 归档和逐归档记录均通过 SHA-256 对账。

| 核验项 | 结果 |
| --- | --- |
| 真实旧产物和数据 | 从 GitHub 下载旧 `v0.4.2`、`v0.4.3` 预览 App 和 CLI wheel，哈希与旧 Release 一致。 |
| 预览身份与签名 | 归档显示 `ORX (Internal Preview)`，Bundle ID 继续为 `org.orxanalytics.macos.dev`。当前候选签名在启动探针前后均通过严格校验。 |
| SQLite 备份 | 新 App 采用 SQLite backup API，Swift 回归覆盖仍有已提交 WAL 的源，恢复副本读到 1,100 tokens，重复启动不重复备份。 |
| 0.4.2 回退 | [实测记录](stage-4-rollback-0.4.2.json) 使用真实包内引擎和 wheel，store 3→4→3。 |
| 0.4.3 回退 | [实测记录](stage-4-rollback-0.4.3.json) 使用真实包内引擎和 wheel，store 4→4→4。 |
| 升级、回退一致性 | 两轮保留自定义目录、项目登记与历史用量（1,111 输入、222 输出、33 缓存），源 ORX fixture 哈希不变。两轮升级后的库均在恢复前保存，恢复副本经 SQLite 完整性检查，旧引擎及 CLI 再读通过。 |
| CLI-only / App-only 兼容 | [与 0.4.2](preview-pair-0.4.2.json)、[与 0.4.3](preview-pair-0.4.3.json) 的版本组合复用引擎，对未知 IPC 版本明确报错，引擎继续服务。 |
| 系统登录项 | 2026-11-19 经当前账户授权，在实际系统设置确认 ORX 初始无登录项，登记签名候选临时副本时为一条，移除后再次确认无条目。原 App 路径保留。 |
| 正式渠道 | 本机的签名预检无 Developer ID Application 身份，历史正式渠道安装包也未找到。本轮记录限内部预览。 |

```sh
uv run --locked python scripts/check_app_upgrade.py \
  --work-dir <empty-directory> --old-archive <legacy.zip> --old-wheel <legacy.whl> \
  --new-archive <candidate.zip> --new-wheel <candidate.whl>
```

加 `--engine-only` 用原归档里的引擎完成数据路径复验。这个模式的报告不包含
App 图形进程保活、退出或自动备份落盘断言。

当前预览发布门禁仍包括真实 App 图形进程、App 退出后 CLI watch 及通知
权限复核。两次旧 App 自动演练中进程提前退出，引擎已启动。独立启动探针
对旧版和候选都观察到 socket，但未稳定观察到引擎仍是所启动 App 的子进程。
两个包的严格签名探针均通过。登录项实测通过系统设置完成，尚未完成由 App
设置触发的同一状态检查以及系统重新登录验证。

## 同账户实测收尾

在当前账户将真实候选的三个偏好临时指向隔离目录，先启动该目录的 0.4.5
CLI watch，再用 Launch Services 启动候选。候选写入了隔离项目的面板缓存，
随后图形进程退出，未取得常驻菜单及正常退出的完整证据。退出后 CLI watch
仍持有需求，服务进程 PID 与启动前一致。正式退出动作未得到验证。
清理时只停止本轮 CLI watch 与命令行、数据路径都匹配的本轮引擎，并恢复
原 App 偏好和缓存。临时系统登录项已经移除，最终签名检查通过。

发布门禁保留。在 GitHub 实查，ORX 当前 Release 仍为 `v0.4.0`，
Analytics 当前历史 Release 为 `v0.4.3`、`v0.4.2`。旧仓未归档，
旧 README 和发布入口保持原状。未创建新远端标签或 Release。

操作者随后确认快速面板没有打开。真实预览安装的桌面门禁仍未通过。

## 直接安装与身份修正复验

获得直接安装授权后，在 macOS 26.6.2（25G83）当前账户把已验签的
0.4.5 预览包安装进 `/Applications/ORX (Internal Preview).app`。
将原 0.4.4 安装移至同卷备份，临时偏好指向隔离数据目录。从 Finder 打开
后，控制中心日志显示图标以正式渠道 ID 注册，并收到隐藏指令。应用紧接着
退出。应用外层 plist 的 ID 是 `org.orxanalytics.macos.dev`，Mach-O 的
`__TEXT,__info_plist` 仍是 `org.orxanalytics.macos`。对原产物执行
`otool -P` 与外层 plist 对比可以复现这一差异。

`f548452f4f5511d327e5f79ca1fec6b393196200` 将同一份渠道元数据链接
进可执行文件并复制进签名包。构建在签名前提取 Mach-O plist 并逐字节
对账。[修正后的候选清单](identity-candidate.json) 来自该提交的干净检出。
14 项构建与联合检查均通过，包括两套 CLI 独立安装、256 项 Analytics
测试、38 项 App 打包测试、167 项 Swift 测试、包内引擎和联合端到端。
6 个产物文件通过 SHA-256 对账。修正后预览仍保留原预览 Bundle ID。

| 复验项 | 结果 |
| --- | --- |
| 临时目录中的修正构建 | 控制中心按正确预览 ID 注册，随后发出 `NSStatusItemChangeVisibilityAction`，App 退出。严格验签前后通过，封条工具因缺少存活的父进程证据返回失败。 |
| 修正后的冻结归档 | 从实际 zip 提取到 `/Applications`，安装前后严格验签通过。Finder 打开后，11:35:41.437 控制中心把预览状态项列入 blocked list，11:35:41.478 App 收到终止请求。系统设置可见的两条 ORX 相关菜单栏开关为开启，屏蔽原因仍未确认。 |
| 退出后共享引擎 | 隔离 CLI watch 仍持有需求，同一 PID 持续服务。由于 App 提前退出，菜单中的正常退出路径待验。 |
| 本机回退 | 新 App 移出安装位置，原路径的 0.4.4 恢复并严格验签通过。三项测试偏好恢复，显示缓存与安装前逐字节一致。隔离 watch 和引擎均已停止。 |
| 旧 0.4.2 数据升级与回退重跑 | 真实旧 App 包内引擎及旧 wheel 对同一冻结候选，store 3→4→3。源库不变，registry 和合成历史用量 1,111／222／33 保留，恢复后的旧 CLI 和引擎可读。 |
| 旧 0.4.3 数据升级与回退重跑 | 同上，store 4→4→4。两轮都通过 SQLite 完整性检查，并保留升级后的数据库。 |

内部预览的真实常驻、菜单内正常退出、通知权限迁移、App 发起的登录项
注册及重登检查仍待验。正式渠道缺少可用的 Developer ID Application
签名身份和可验证的历史正式安装包。第四阶段发布门禁未通过，第五阶段保持
未启动。远端标签、Release、下载资产、旧仓入口和归档状态保持原状。

## 菜单栏身份对照与真实系统接口

在同一账户构建四个带匹配内外层 plist 的 ad-hoc 签名最小应用。
测试应用均不读取 ORX 数据。新 Bundle ID 下，SwiftUI `MenuBarExtra` 和
AppKit `NSStatusItem` 都完成注册并持续运行。复用
`org.orxanalytics.macos.dev` 后，两种实现均被 Control Center 列入
blocked list。SwiftUI 收到 `NSStatusItemChangeVisibilityAction` 后退出，
AppKit 进程保活但图标仍被屏蔽。通过系统设置将两个可见的 ORX 开关分别
关闭再开启，屏蔽依旧，开关恢复为测试前的开启状态。四个探针均已结束。

[类似的 macOS 26 实测报告](https://github.com/steipete/CodexBar/issues/1440)
描述了跨应用残留映射的相同日志序列。本机对相关 Control Center group
container 偏好文件的只读访问被系统拒绝，映射原因尚未确认。

另以[临时 Bundle ID 的同源码桌面对照版](stage-3.md#finder-与配置逐屏验收)
完成以下系统交互：

- App 的「登录时启动」通过 SMAppService 登记，App 显示已登记。
  macOS「登录项与扩展」的「登录时打开」实际出现且仅出现一条临时 App
  项。用 App 取消登记后，两处均显示已移除。
- 以 macOS 退出命令退出临时 App。退出前共享引擎有 `background` 和
  `ui` 需求，退出后 App 进程已结束，`ui` 需求释放，CLI watch
  仍持有需求且引擎 PID 不变。最后停止隔离测试的 watch 和引擎。

这些操作验证了当前代码的桌面和系统接口。实际预览渠道仍须使用原
Bundle ID 验证菜单栏启动、菜单内退出、通知授权迁移和重登；正式渠道
仍受签名及历史安装包条件限制。阶段五保持未启动。

## 启动门禁回归

签名完整的测试 App 会启动引擎，等真实 socket 收到探测连接后自行以
状态码 0 退出。旧检查在捕捉到暂时存在的子进程和 socket 后返回成功。
现在 `check_runtime_seal.py` 还会在引擎应答后观察 App 1 秒。
若 App 在观察期间退出，将 `app_exit_code_before_stop` 写入证据
并报告 `launch-evidence-missing`。39 项 App 打包测试通过。

## 当前账户控制中心重置复验

2026-11-19，经授权在当前账户从「系统设置 → 菜单栏」执行
「重置控制中心」。确认前记录菜单栏设置页的系统控件、第三方开关、
时钟与电池选项。记录保存在本机，避免把个人系统偏好提交到仓库。
系统确认框说明操作会把控制中心布局恢复为默认。确认框关闭后，
设置页中可见的开关及选项与重置前一致。

从 `f548452` 冻结的 zip 再次安装原预览 Bundle ID
`org.orxanalytics.macos.dev`，核对归档 SHA-256，内外层
`Info.plist` 一致，签名通过 `codesign --verify --strict --deep`。
12:37:30 从 Finder 打开实际安装的 App。控制中心在 12:37:30.098
将预览状态项列入 blocked list；App 在 12:37:30.192 收到
`NSStatusItemChangeVisibilityAction`，12:37:30.193 退出。
重置未解除本账户对预览身份的屏蔽，桌面启动门禁继续失败。

按约定移出 0.4.5，将原 0.4.4 恢复到原路径并严格验签，
恢复测试涉及的偏好和缓存。提前退出后，隔离 CLI watch 仍持有
原引擎；复核后已停止本轮隔离 watch 和引擎。第五阶段仍未启动。

### 区分应用身份和菜单栏项目名

仍在当前 macOS 26.6.2 账户运行单独签名的 AppKit 探针。保留预览
Bundle ID `org.orxanalytics.macos.dev`，用公开的
`NSStatusItem.autosaveName` 将项目名称改为 `orx-rehearsal-named`。
12:58:42.904 Control Center 日志确认将同一 host 从 `Item-0` 改名，
12:58:42.921 又将改名后的 host 加入 blocked list。探针进程仍存活。
因此只改菜单栏项目的保存名也无法解除该账户的屏蔽。原 0.4.4
安装及实际数据保留，已停止探针并清理探针专用设置。

对同一探针副本只更换为本机 Apple Development 签名，保持 Bundle ID
与已验项目名不变，严格验签通过。13:00:54.383 控制中心仍将该
host 加入 blocked list。当前账户的屏蔽可以在 ad-hoc 及 Apple
Development 签名下复现。系统公开的偏好查询未返回该应用的归属表。
该签名仅用于本机对照，正式渠道仍无 Developer ID Application 身份。
临时探针已停止，原 0.4.4 安装通过严格验签。

系统布局重置前后的日志仍来自原 Control Center 进程。已确认其由
当前图形会话的系统 LaunchAgent 管理。尝试使用
`launchctl kickstart -kp "gui/$(id -u)/com.apple.controlcenter"`
独立重启该进程，系统完整性保护以退出码 150 拒绝。复查原进程
仍在运行，当前安装仍为严格验签通过的 0.4.4。图形会话重登尚未演练。

## ORX 新 Bundle ID 及本机替换复验

用户指定正式 ORX 身份为 `com.aderan.orx`。内部预览使用
`com.aderan.orx.dev`，两个渠道各自读取同渠道旧偏好域。
[c5bb1e4 的冻结产物清单](aderan-candidate.json) 绑定 ORX 0.4.1、
Analytics 0.4.5、App 0.4.5，14 项联合检查通过。168 项 Swift 测试、
39 项 App 打包测试通过。包内与可执行文件嵌入的身份及前身元数据一致。

- 用真实旧版 App 包内引擎与 CLI wheel 复验切换后的数据升级和回退。
  [0.4.2](aderan-rollback-0.4.2.json) 的 store 为 3→4→3，
  [0.4.3](aderan-rollback-0.4.3.json) 为 4→4→4。两轮 registry、
  历史用量与源库一致，升级前后数据库保留供核对。
- 实际 zip 安装至 `/Applications/ORX (Internal Preview).app`，
  启动前后签名通过。首次启动时控制中心接受新身份，应用保持运行，
  五项测试偏好从旧域迁移。测试进程仅写入隔离 Analytics 数据目录；
  项目发现正常读取用户级索引。应用从测试目录启动自己的引擎，当前
  CLI watch 连接同一进程，终止测试 App 后 watch 仍由同一引擎服务。
  清理测试时原偏好、显示缓存均已恢复。
- 新 App 的终端替换演练生成了升级前 SQLite 备份，随后控制中心屏蔽
  其状态项，进程退出，完整桌面演练因此失败。屏幕锁定时用正常安装路径
  重开，新身份同样被屏蔽。实际用户数据目录的 store 4 已做 SQLite
  在线一致性快照并通过完整性检查，原引擎 PID 保留。
- 按回退条件移出新版，恢复本机 0.4.4 并严格验签。新身份的重复启动、
  通知授权和登录项验收待完成。正式 App 的新身份已配置，
  Developer ID 签名、公证及真实正式安装包仍待核验。第五阶段未启动。

### 解锁后的 Finder 复验

2026-11-19，操作者确认 Mac 已解锁。安装前实际「登录时打开」列表
没有 ORX 条目，「应用通知」列表没有可见的 ORX 条目。使用同一份
`c5bb1e4` 冻结预览 zip，逐项复核校验和，将已验签的
`com.aderan.orx.dev` 产物安装到 `/Applications`。测试使用从
0.4.3 合成用量备份复制的独立数据目录和隔离 CLI watch。

16:46，从 Finder 选中实际 App 并打开。控制中心在
16:46:16.261 将新预览状态项加入 blocked list；App 在
16:46:16.308 收到退出请求，随后进程消失。解锁状态仍复现启动
失败，菜单栏常驻和两次连续启动未通过，通知授权及 App 自身
登录项功能未进入验收。此次演练未新增 ORX 系统登录项或授权。

已移出新版，恢复原路径的 0.4.4 并严格验签。隔离 watch 和引擎
已停止，测试偏好与显示缓存已恢复。原用户库保留，升级前的
SQLite 一致性备份再次通过 `PRAGMA integrity_check`。
阶段五维持未启动。

### 停用历史菜单栏条目后的复验

按操作者要求，通过「系统设置 → 菜单栏 → 允许在菜单栏中显示」
将五条可确认的历史 ORX 记录关闭，包括 `ORX Analytics`、
`ORXAnalytics` 和三个探针身份。系统设置继续列出这些旧记录。
对新版 `com.aderan.orx.dev` 关闭后重新开启，复核新版为开启、
五条历史记录为关闭。操作范围仅限 ORX 相关开关。

重新安装并严格验签同一冻结预览包，从 Finder 打开后，
16:51:54.098 Control Center 仍将新版状态项加入 blocked list，
App 在 16:51:54.099 收到终止请求。已恢复原路径的 0.4.4 并严格验签，
清理隔离进程与偏好。原用户数据库仍在，升级前的一致性备份
再次通过完整性检查。历史菜单栏开关保持关闭。

### AppKit 生命周期与 Finder 连续启动

`42e2ff49eadb217aa4ec9c8c518ae12616e39c40` 将菜单栏改为
AppKit 的 `NSStatusItem`，保留原来的 SwiftUI 快速面板、主窗口及设置。
Finder 冷启动及重新打开会显示同一个主窗口。控制中心隐藏状态项时，
应用仍可运行。主窗口通过「应用选项」打开设置或退出。
[candidate](finder-candidate.json) 记录 14 项联合检查和产物校验；
168 项 Swift 测试、39 项 App 打包测试通过。

从同一份签名通过的 `com.aderan.orx.dev` 预览归档实际安装。
用隔离数据目录完成 [两次 Finder 冷启动](finder-launches.json)。
17:45:29 和 17:48:06，控制中心均将对应 PID 的菜单栏状态项加入
blocked list。两次启动的进程各自存活，并且 Window Server 各报告
一个可见、标题为 `ORX` 的主窗口。第一次运行中从 Finder 重开时，
进程和主窗口保持同一实例；先正常终止，再启动得到第二个 PID。
退出测试 App 后，CLI watch 继续使用同一个隔离引擎。

清除测试偏好和隔离引擎后，使用恢复的真实设置再次从 Finder 打开。
当前新 App 正在运行并显示一个主窗口，原分析库文件及既有 SQLite
备份校验值保持，实际共享引擎 PID 不变。新版保留在「应用程序」，
旧 0.4.4 安装包已保存在本机的可回退目录并通过严格验签。
该账户的菜单栏图标仍由 macOS 隐藏。此轮实测覆盖 Finder 回退和
重复启动，原通知授权及实际重登未作为通过项，未执行发布切换。

### 新版登录项与通知续验

2026-11-19，在「系统设置 → 通用 → 登录项与扩展」中从
`/Applications/ORX (Internal Preview).app` 添加已安装的
`com.aderan.orx.dev`。系统「登录时打开」列表确认只有一条 ORX
记录。重登前的图形会话控制中心 PID 为 698，ORX PID 为 71421，
Window Server 报告一个可见的 ORX 主窗口。新版的菜单栏开关为开启，
该进程的状态项仍在控制中心日志中被列入 blocked list。

新版通知开关在本轮开始时关闭。桌面控制工具仍把预览显示名称解析为
旧 Bundle ID，因此通知授权通过操作者在新版设置中触发。实际
重新登录由操作者执行。确认前保留已登记的登录项和已验证的 Finder
入口。系统登记、授权和实际重登分别验收。

通知续验：操作者完成授权后，macOS「应用通知 → ORX (Internal Preview)」
显示 `allow-notifications` 为开启，应用通知偏好也为开启。此后从 Finder
打开现有进程，Window Server 报告唯一的 `ORX` 主窗口。系统仍只登记了
一条新版登录项。该进程的 Launch Services 启动来源为 Spotlight；
控制中心 PID 和控制台登录起始时间均未变化。此轮未取得系统重登的证据。
已正常退出 ORX，准备验证下一次 macOS 登录是否自动启动。

### 不注销时的菜单栏归属排查

在当前已安装的 `com.aderan.orx.dev` 进程运行期间，经「系统设置 →
菜单栏」将其显示开关关闭后再开启。复查开关为开启，控制中心仍将
同一 PID 的状态项列为 blocked。Window Server 同时报告一个可见的
ORX 主窗口，应用与 Finder 入口保持可用。

同一新身份第一次正常注册，随后在 Codex 环境里从终端直接启动测试副本时
被屏蔽，之后从 Finder 打开仍被屏蔽。工具调用的应用祖先包含
`com.openai.codex`，当前系统设置中的 ChatGPT 菜单栏开关为关闭。
[macOS 26 另一应用的复现](https://github.com/steipete/CodexBar/issues/1440)
已记录过归属到同一 Codex 身份导致的屏蔽。对 ORX 而言仍是待验证的
假设。尚未更改 ChatGPT 设置，等待是否可以短暂开启后恢复的确认。

### ChatGPT 菜单栏显示对照

经操作者授权，在同一 macOS 会话中临时开启 ChatGPT 菜单栏显示。
19:00:52.515，Control Center 在解除 `com.openai.codex` 屏蔽的同一时段，
也对原 ORX PID 50888 记录了 `Unblocking host`。保持 ChatGPT 开启，
刷新 ORX 自己的开关并从 Finder 重启，ORX PID 2452 正常显示一个主窗口，
状态项可以登记，但日志仍为 `clientRequestsVisibility: false`，
窗口服务未观察到新增的菜单栏图标。

19:03:04.776，按约定将 ChatGPT 开关恢复为关闭后，Control Center
同时将 ORX PID 2452 和 `com.openai.codex` 列入 blocked list。
系统设置仍显示 ORX 的开关开启。已复核 Finder 能打开同一个 ORX 主窗口，
通知偏好仍为开启，安装包签名有效。本轮未注销 macOS。
切换 ChatGPT 开关足以改变 ORX 的屏蔽状态，尚未证实能恢复实际可见图标，
也未直接读取受保护的系统归属记录。

### ORX 独立显示的本机复验（2026-11-19）

- Locove 0.2.0 使用 AppKit `NSStatusBar.system.statusItem(withLength:)`，
  通过关联自身 Bundle ID 的 LaunchAgent 驻留。首个 ORX Analytics
  预览标签 `v0.4.2` 使用 SwiftUI `MenuBarExtra`。新版 ORX 已使用
  AppKit。用与 Locove 同类型的临时 LaunchAgent 启动已安装的 ORX，
  Control Center 仍将状态项屏蔽。临时任务已卸载。
- 仅恢复 ORX 自己的状态项显示偏好后，Control Center 收到
  `clientRequestsVisibility: true`，但在 23 毫秒内将其移入 blocked
  list。原始偏好副本确认 `com.aderan.orx.dev` 自身已获准显示，
  同时被列在已关闭的 `com.openai.codex` 的 `menuItemLocations` 下。
  Locove 自身显示已获准，使用独立归属。
- 用 Finder 备份当前偏好，校验副本中的唯一改动为从
  `com.openai.codex` 的 `menuItemLocations` 移除
  `com.aderan.orx.dev`。两个开关及其他条目均保留。应用副本后，
  当前用户的偏好代理和 Control Center 重新启动，用户未注销。
  这次修复针对本机历史状态。测试图形启动应打开已安装的应用包，
  构建的无界面冒烟继续使用 `--smoke-test`。
- 系统设置复核 ChatGPT 开关关闭、ORX 开关开启。通过 Launch Services
  对同一预览安装包冷启动两次（PID 40655、44940），Control Center 均
  登记 ORX 并在 Window Server 产生一个屏幕内的状态项窗口
  （23988、24002）。分别确认一个可见 ORX 主窗口（23986、24000）。
  第二次启动后复查状态项仍可见，App 严格验签通过。
- 原 Analytics store 仍在，实际共享引擎 PID 不变，升级前的一致性
  备份通过只读 `PRAGMA integrity_check`。此次保留已安装的
  0.4.5 内部预览。登录项的实际重登、菜单图标点击和正式渠道仍待验收。

### 本机共享 Analytics 引擎切换（2026-11-19）

- 用 SQLite backup API 创建切换前一致性备份，完整性检查通过。
  原 0.4.4 签名包的引擎在隔离目录读取备份并通过协议 1 查询。
  停止旧写入进程后另存静止库备份，用于实际切换和回退。
- 首次切换中，原 CLI watch 的旧 socket 报断管。85 秒验收窗口
  未覆盖其下一轮 60 秒续约。按约定停止新版写入，保留切换后的库，
  恢复静止备份和原 0.4.4 引擎。原 watch PID 22839 回到已续约状态。
- 按断线后两轮续约所需的时间重试。已安装的
  `07092c39810c93e23a2804dc9c529d609f5bd55d` App 启动 0.4.5 包内
  引擎 PID 86028，仍用原数据目录、原 socket 和协议 1。共享引擎收到
  App PID 86025 的启动刷新，原 watch PID 22839 于 21:30:47
  重新续约。独立运行原 CLI 的 `watch status` 复核了两个 PID。
- 通过 ORX CLI 完成隔离本机任务 R001/T001。经实际共享引擎导入，
  Swift 客户端读取到 Controller `codex`、选中 `orx-host`、
  选中原因为回退。候选 `other-host` 因要求 ZCode 宿主被过滤。
  逐字段与 ORX 路由日志对账，源库哈希一致，测试项目已从登记移除。
  现有 ORX 项目的历史 R011/T007 也核对了当次 profile 和过滤原因；
  原记录没有 Controller 字段。
- 原 10 个项目、39 条 Run、1,070 条尝试、432 条用量记录及 ZCode、
  Codex 会话标识均核对保留。最终备份的 SHA-256 为
  `8c64386556809aa80ac317337e1fee8a96231629746a0808df63fea3d5cdb91e`。
  独立验签和 SQLite 完整性复核通过。没有变更发布状态。

### ORX 退出期间的共享引擎续约（2026-11-19）

- 操作者在已安装 ORX 的主窗口打开隔离执行 R001/T001，确认看到
  Controller `codex`、选中的 `orx-host` 和 `other-host`
  的宿主不匹配原因。
- 21:50:14 正常退出 ORX PID 86025。App 完全退出期间，原 CLI watch
  PID 22839 在 21:50:47 对原 socket 续约。21:50:49 重开 ORX，
  新 App PID 80523 显示一个主窗口。
- 退出前、关闭期间和重开后共采样 36 次。引擎 PID 86028、启动时间、
  socket inode、watch PID 和 watch 后台租约标识保持一致。已移除临时
  验收项目登记，执行列表查询不再返回该项目，原 10 个登记保留。

### 0.5.0 目录收口（2026-11-20）

ORX 0.5.0 的两个独立 CLI 和 App 从同一提交构建。Analytics 默认使用
ORX 的用户数据根下的 `analytics/`，App 的可重建显示缓存用
`Application Support/ORX`。路由页只使用一个规划深度并保留请求推理
档位，历史页面继续展示来自 Analytics 的原始执行记录。

- 冻结候选 `03c1ed05f89b0f9b2e2f7c59b27f8c5c09ce68c6` 的 14 项
  联合检查通过。Analytics 258 项、Swift 175 项和打包 39 项通过。
  安装预览前分别验证两个 0.5.0 wheel。App 的实际路由客户端在用户
  默认和当前项目均读到 `standard` 与候选的请求 effort。两层原
  TOML 校验值一致。
- 退出旧 App，停止旧 watch 和引擎后，使用 SQLite backup API 保存
  静止库。备份完整性检查通过，SHA-256 为
  `aed0d9850832ba7cfab2f8cc2e671b3a9f7c05bcea70280ce49193e8cb391771`。
  旧目录的持久文件和既有备份一并迁入新目录。旧绝对路径无运行目录
  或软链。项目登记 10 个、Run 40 条、尝试 1,071 条、用量观测 432 条
  及原生会话标识核对保留。
- 新 App 启动新目录的引擎 PID 48950，独立 `orx-analytics` 0.5.0
  不带目录参数连接同一 socket，watch PID 48973。关闭 App 时 watch
  于 12:20:26 续约，重开后仍是同一个引擎、socket 和 watch。
  首次及重开均有 Finder 主窗口且 Control Center 注册 ORX 图标。
  预览严格验签通过，旧签名包及独立 SQLite 备份保留。
- ORX 全量首次运行通过 1,304 项，单条断言仍期待旧 `auto`。修订
  该断言后重跑 CLI 与桌面路由共 92 项，通过。冻结产物的运行时代码
  与验收期间一致。
