---
status: accepted
date: 2026-11-18
---

# ORX 同仓维护执行工具、分析工具和桌面应用

ORX 和 Analytics 已有联合验收与发布，跨仓需要分别固定提交、准备检出并记录兼容组合。随着桌面应用同时提供执行观察和 ORX 配置，决定将 `orx-analytics` 源码纳入 ORX 仓库的 `tools/orx-analytics/`，保留独立包、CLI 和数据目录，并将现有菜单栏应用演进为 `apps/macos/` 下的 `ORX`。同仓使执行契约、分析读取和界面可以一起提交与验收，同时保留 Analytics 单独安装和分析非 ORX 会话的能力。

本决策取代旧 Analytics 仓库 [ADR-0001](https://github.com/aderan/orx-analytics/blob/v0.4.3/docs/adr/0001-independent-repository.md) 中的仓库组织决定。该文保留为历史记录。独立仓库适合独立维护和发布，Git submodule 仍需管理两份提交和子模块指针，合入 `src/orx/` 则会把两套工具的打包职责耦合在一起。实际协调成本可见 [G012 联合验收记录](../diagnostics/g012/joint-acceptance-report.md)。

- ORX 拥有配置、路由和执行事实。Analytics 按 [观测契约](../observability-contract.md) 只读消费 ORX 数据，并管理自己的采集、指标、缓存和分析库。ORX CLI 的安装与执行无需 Analytics。
- 用户通过一个 `ORX` 菜单栏入口查看执行、模型与路由、用量分析。桌面应用通过 ORX 接口管理执行配置，通过 Analytics 引擎查询用量。各包和存储版本独立演进，由联合验收确认兼容组合。
- 迁移切换后，由 ORX 仓库发布后续源码和安装产物。旧仓库保留历史版本与迁移入口，停止作为日常开发和新版本发布的来源。

分阶段交付、发布切换和回退见 [完整迁移方案](../orx-analytics-monorepo-migration.md)。
