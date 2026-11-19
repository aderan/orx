# Analytics 同仓导入基线

核对日期为 2026-11-19（北京时间）。依据为 ORX 工作区中的 `docs/adr/0001-orx-monorepo-independent-packages.md` 与 `docs/orx-analytics-monorepo-migration.md`。

| 仓库 | 本地 main | 远端 main | 工作区 |
| --- | --- | --- | --- |
| https://github.com/aderan/orx | `5633f75f52fcf2d866504b4a44ef4a449ba7d78e` | `f2b988cad1f6a7816e307a1b9eda1b4a905c9d8b` | 领先 10 条提交；31 条未提交路径，无暂存改动。迁移在独立 worktree 进行。 |
| https://github.com/aderan/orx-analytics | `d476e28c09e5022030179e71f6f1082248d897b4` | `d49ea5d62a51941eab9cb5b52f75a442163b9156` | 领先 3 条提交；已跟踪及可见未跟踪文件干净。 |

ORX 当前源码版本为 0.4.0，源库 schema 为 15。选定 Analytics 0.4.4 的完整树 `abe8f92593c66600cd45a75a00b3d7d7949f5ac3`，其 store 版本为 4、引擎协议为 1。该版本延续了 0.4.3 的兼容接口，含空闲 WAL 重复导入、快照和诊断保留、自动项目发现修复。三个本地提交依次为：

- `2c0bbb438ebfe41bb21bbb0af655c55242b93042` 修复空闲重复导入并约束快照保留。
- `453265d6c0db520518e0db2009082cf5623b5b22` 完成有界采集、项目发现和 0.4.4 源码。
- `d476e28c09e5022030179e71f6f1082248d897b4` 记录 0.4.4 的原仓本机验收。

[增量 Git bundle](analytics-unpublished.bundle) SHA-256 为 `ba020dc78519ba2e2b3eaa99fd440ab64cf061bc79e1e4e335160de2653ece0c`。它依赖旧远端可达的 `d49ea5d62a51941eab9cb5b52f75a442163b9156`。在旧仓新克隆执行 `git bundle verify /path/analytics-unpublished.bundle`，再执行 `git fetch /path/analytics-unpublished.bundle refs/heads/main:refs/heads/import-audit` 即可恢复原提交树。

[文件清单](import-map.json) 记录 114 个文件的原路径、目标路径、Git blob、模式和 SHA-256。机械导入提交可通过 `git show <import-commit>:<destination> | shasum -a 256` 逐项复验。Swift 包原路径保持 `apps/macos/`，App 打包及运行时验证脚本归 `apps/macos/scripts/`，两份 App Python 测试归 `apps/macos/tests_python/`，其余源码、文档和安装验证归 `tools/orx-analytics/`。源树的 11 份历史报告留在原历史，可用旧远端及 bundle 查阅。导入树中的原生会话、SQLite 和 IPC 测试样本由 Python 夹具及 Swift 测试构造。未发现源仓许可证文件。导入范围按提交树构建，排除了工作区会话、数据库、缓存、构建物和凭据。

核对使用 `git status --porcelain=v1 -uall`、`git log origin/main..HEAD`、`git ls-remote <url> HEAD refs/heads/main 'refs/tags/*'` 和 GitHub Release API。远端 ORX 标签为 `v0.1.0`、`v0.2.0`、`v0.2.1`、`v0.3.0`、`v0.3.1`、`v0.4.0`。Analytics 历史标签 `v0.4.2` 指向 `2461a260387caf0e6acb63446d4be3b8d1a43b46`，`v0.4.3` 指向 `d0867103a447e94ca1fd01731a60be62aeb2823a`。历史联合组合仍是 ORX `v0.4.0`（`f1cc7757c953d3fee5bff5630613a6d2fb380a74`）分别搭配 Analytics `v0.4.2` 和 `v0.4.3`。两份 App 的 GitHub 归档均标注为内部预览，旧仓可写且未归档。发布资产及 API 提供的哈希见 [旧发布资产快照](legacy-releases.json)。
