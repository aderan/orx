# 升级到 ORX 0.5.0

先确认当前命令来源与数据目录，再切换安装；软件版本相同也应核对构建提交和发布 SHA-256。项目配置、用户配置、执行库和 Analytics 数据是不同的对象。

## 备份

1. 协调停止相关执行写入。不要中断仍在交付的 Worker，也不要因 UI 无响应另起同一任务。
2. 对项目 `.orx/state.db` 和 Analytics 数据库使用 SQLite backup API 或 `VACUUM INTO` 生成一致性备份，检查 `PRAGMA integrity_check`；活跃 WAL 下不要只复制 `.db`。保留项目 `.orx/` 的配置、计划、日志及用户配置。
3. 记录已安装版本、构建来源、watch 和引擎所有权。仅停止自己管理的 watch，避免影响其他消费者。

## 安装与用户配置

使用发布资产或同一个版本标签安装两个独立 CLI；精确来源和资产 SHA-256 以 Release 附带的清单为准：

```sh
uv tool install --reinstall --python 3.12 'git+https://github.com/aderan/orx.git@v0.5.0'
uv tool install --reinstall --python 3.12 'git+https://github.com/aderan/orx.git@v0.5.0#subdirectory=tools/orx-analytics'
orx version --json
orx-analytics --version
orx config path --json
orx config migrate-home --json
orx --host codex skill update
```

在 ZCode 中使用 `--host zcode`。`migrate-home` 将旧 `~/.config/orx/` 的 config.toml 与 profiles.toml 成组复制到默认 `~/.orx/`，原件保留；冲突时拒绝覆盖，不要手工删除已有配置来强行迁移。显式覆盖路径继续有效，见 `orx config path --json`。

Analytics 默认目录为 `~/.local/share/orx/analytics/`，随 ORX 用户数据根变化；`ORX_ANALYTICS_HOME` 和 `--data-dir` 优先。旧 `~/.orx-analytics/` 的持久数据需按 [App 与数据迁移说明](../tools/orx-analytics/docs/app-installation.md) 协调停止写入、备份并搬迁。仅升级软件不会证明旧数据已经搬迁。项目 `.orx/` 保持原处。

## 验收语义

ORX schema 仍为 15；0.5.0 通过追加 task_events 记录内容版本和失效水位。首次推进没有指纹的历史交付会重新检查。旧通过保留为历史，不继承为当前有效裁决。Analytics 中依据缺失时显示有效性未知，状态以最近采集的 ORX 记录为准。

升级后确认候选顺序、当前宿主、两个 CLI 版本以及 App 构建来源；用一个有依赖任务的小目标验证派发、产物交接和独立验收。App 正常退出后，仍有 watch 需求的共享引擎应继续服务。

## 回退

保留旧安装包和上述一致性备份。先停止新版相关写入、保存升级后的库，再恢复旧软件和其受验的配置及数据备份。即使 schema 号相同，也不通过手改 schema_version 或删除事件制造兼容。旧 Analytics 引擎使用原匹配版本和数据根；具体已验证旧版组合见 [兼容表](../tools/orx-analytics/docs/compatibility.md)。原生会话、项目登记与历史用量必须在恢复后复核。
