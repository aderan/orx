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
