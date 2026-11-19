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
