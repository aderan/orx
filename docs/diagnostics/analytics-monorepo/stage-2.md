# 阶段二验收

2026-11-19（北京时间），Apple Silicon / macOS 26.6.2、Swift 6.4.2、构建驱动 Python 3.12.15。采用[同仓验收步骤](candidate-runbook.md)。冻结提交 `ec8198ee398e677ae0f0b492497cbb6474d143ba` 的 [候选清单](stage-2-candidate.json) 绑定 ORX 0.4.0、Analytics 0.4.4 与 App 0.4.4，ORX schema 15、Analytics store 4、IPC 协议 1。内置 Python 3.12.8。四份 Python 产物、内部预览 App 归档及 App 构建记录的六项 SHA-256 均以 `shasum -a 256 -c SHA256SUMS.txt` 复验通过。

| 检查 | 结果 |
| --- | --- |
| ORX 无外部输入的 pytest 套件 | 1,172 通过，2 项 M1.2 现场数据测量需要单独提供状态库。原历史联合测试从对应旧检出运行。 |
| 两套安装验证 | 通过。Analytics 候选 wheel 本身也在单独虚拟环境离线安装并核对版本。 |
| Analytics Python、App 打包 Python、Swift（含真实 Python wire 夹具） | 分别 254、38、163 项通过。 |
| 产物 App | ad-hoc 签名严格验证、内置 Python 启动和可执行文件冒烟通过。 |
| 同仓端到端 | 安装的 ORX wheel 产生暂停、恢复、取消与结束事件，App 内置引擎读到这些事实。两次采集使用同一只读源，最后保留一份成功快照，原状态库校验与非 sidecar 文件清单不变。 |
| WAL、旧数据、未知 schema | 71 项专项测试通过，覆盖只读导入、活跃 WAL、重复采集、旧 schema、未来 schema 拒读和 watch 所有权。另用已安装的当前 wheel 打开实际历史 store v2 夹具副本，迁移至 v4、保留原快照并新增回执及请求字段，未来 store 被拒绝，原夹具哈希一致。 |
| 历史 G011 | 原 ORX `v0.4.0` + Analytics `v0.4.2`，在 ORX 历史检出运行 `uv run --locked pytest -q tests/test_joint_release.py`，21 项通过。 |
| 历史 G012 | 使用原验收驱动 `f2b988c`、ORX 产品 `v0.4.0` + Analytics `v0.4.3`。原始两个样本再生一致，`freeze --goal-id g012` 通过；`pytest -q tests/test_joint_release.py -k g012` 为 7 项通过。 |
| CLI 或 App 单独升级 | 0.4.2 与 0.4.3 旧 Release 都下载并对账。跨版本实际产物复用同一进程，协议 999 返回 `unsupported_protocol`，所有权不变。证据见 [0.4.2](stage-2-pair-0.4.2.json)、[0.4.3](stage-2-pair-0.4.3.json)。 |

第一轮候选在隔离 HOME 中选到了系统 Python 3.9，修正为显式沿用验收驱动的解释器。第二轮候选逐阶段读到正确的 Goal 状态，最后一条历史断言仍要求在结束时保有早期快照。现按 0.4.4 的一份快照策略，在暂停发生时验证暂停数据，再在结束时验证当前事实，历史路径仍保留原断言。本阶段仅验收内部预览构建，系统安装替换、通知与登录项留给阶段四。
