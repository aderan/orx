# ORX CLI + Analytics 0.5.0 正式发布

2026-11-20，[v0.5.0 已正式发布](https://github.com/aderan/orx/releases/tag/v0.5.0)，非 draft、非 prerelease。本记录接续早期 [0.5 收尾候选](../0.5-closeout/README.md)，早期 4ff2040 证据作为历史保留；本次正式产品来源为 **8784fc9ce90f35c24eeb61e3e80033171d294666**。

机器证据：[冻结](freeze.json)、[发布与消费者验收](publication.json)、[公开来源清单](release-manifest.json)、[公开 SHA-256](SHA256SUMS-public.txt)。冻结内部全套清单为 candidate.json / SHA256SUMS-candidate.txt，包含本地 App，不能代替公开附件列表。

## 来源与 CI

- [PR #1](https://github.com/aderan/orx/pull/1) 已合并；main 合并提交 255fd2c7aa7a901c4cb859cf7e65405fc517ee6f，与产品来源 8784fc9 的源码树逐项相同。
- [PR CI](https://github.com/aderan/orx/actions/runs/44704836859) 和 [main CI](https://github.com/aderan/orx/actions/runs/44705343460) 各五项通过：ORX、Analytics Linux Python 3.11/3.14、Analytics macOS Python 3.12、macOS 联合构建。
- 首轮 CI 检出旧测试仍强制要求“Analytics 位于独立仓库”。本地单项复现红结果后，将其改为同仓源码、独立安装语义，保留可选集成、缺失继续与 watcher 清理的全部检查。相关 71 项通过后重新推送，未跳过测试。旧失败/取消记录保留在 dist/release-0.5.0/。
- 从 8784fc9 本地干净检出冻结，14 项联合检查全绿，六项内部产物哈希一致。两个 CLI 和 App 软件版本均为 0.5.0；ORX schema 15、Analytics store 4、引擎协议 1。
- v0.5.0 注释标签实际解析到 8784fc9。后续追加验收记录的提交不移动标签、不替换受验产物。

## 公开发布与安装

Release 仅有六个附件：ORX CLI 与 Analytics CLI 各自的 wheel、sdist，以及 release-manifest.json、SHA256SUMS.txt。内部预览 App 没有上传。

先建立草稿并下载，核对所有哈希，在两个独立新环境安装两 CLI。实际依赖 Run 通过后，转为正式发布；随后六个公开下载 URL 均以无认证请求取得 HTTP 200，逐字节与草稿受验产物一致。公开发布后另建两个新环境安装公开下载的 wheel，再读同一 Run，仍 done、stale=[]。

README 中两个 Git 标签安装命令也在新环境实测，distribution direct_url.json 均报告 requested_revision=v0.5.0、commit_id=8784fc9…，版本均为 0.5.0。没有用开发工作区包冒充远端安装。

本机两个 CLI 已从公开下载的 wheel 更新，managed skills 已刷新并保留更新前备份，原有 watch 续租正常。项目配置与五份用户原有未提交文件的 SHA-256 均保持。

## 下载包真实 Run

当前 Codex 会话主控，没有终端 Controller 代启动。隔离用户配置/数据与独立工具环境，复用各 CLI 既有登录。Claude Code sonnet 规划两项任务：T001 实现金额合计，T002 依赖前者实现 JSON 文件 CLI。测试/样例由主控预置，三个文件哈希全程不变。

ZCode 完成 T001。T002 将 Cursor 排到首位后，额度预检将其标记为 exhausted，ORX 沿项目候选回退到 ZCode；Cursor 没有启动 Worker。本次实际链路为 **Claude → ZCode → Codex**，不是本次四 CLI 全部执行通过。此前四 CLI 成功记录保留在早期收尾证据中。此处没有绕过额度检查或隐去回退。

Codex CLI 独立审查通过，Goal G001 / Run R001 done，两项任务 passed、stale=[]，实际样例输出 1900，退出 0。T002 通过 dependency_artifacts 收到 T001 / attempt 2 的 money_core.py，该模块未重复列入 preread。未见执行权限阻塞或模型超时。effort 仅记录请求/参数事实，不证明上游实际推理强度。

同交付再 verify 无新模型调用。使用独立安装的 Analytics 读取真实 Run，当前窗口与 ORX 相符，采集前后源数据库哈希一致。原始项目、响应、日志、SQLite 备份、固定测试及 Git 安装证据保存在本机 dist/release-0.5.0/consumer-evidence/；账户额度原始诊断只留本地，未作为公开附件发布。

## App 边界

本次正式发布范围为两个 CLI。现有本机 App 继续内部预览；8784fc9 同源内部候选仅保存在 dist/release-0.5.0/candidate-final/。Developer ID、公证、实际注销重登及默认安装路径的桌面工具 Bundle ID 误识别仍按已有边界留痕，没有宣称正式 App 已交付。
