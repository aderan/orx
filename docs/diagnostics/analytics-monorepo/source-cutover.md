# ORX 源码入口切换

2026-11-20（北京时间）。ORX Analytics 源码随 ORX 在同仓维护，两个
Python CLI 可独立安装，macOS App 的源码入口在 `apps/macos/`。

## 新入口与验收

- [ORX 主分支](https://github.com/aderan/orx/tree/main) 以快进方式
  接收同仓迁移。首次上线提交为 `2cbde5156614c5b16154ab810794107f3c3d0754`。
- 从 GitHub 上的 `main` 分别安装两个 CLI，均报告 0.5.0，各自的
  环境中没有另一个包。通过安装元数据核对了同一 Git 提交和 Analytics
  子目录。另在隔离 uv 工具环境中从旧 Git 标签 v0.4.3 安装，通过
  `uv tool install --reinstall` 切换新来源并再次确认 0.5.0。
- 从 GitHub 全新检出的上述提交生成同仓内部预览候选。两套 Python 包、
  App 均为 0.5.0，ORX schema 15、Analytics store 4、引擎协议 1。
  14 项联合检查通过，涵盖 Swift、包内引擎与只读端到端演练。
  内部预览 zip 的 SHA-256 为
  `9e6d6599a8b65de67bef57bba9cbd9749a27f8c3ecaa3d230f78190ef037fd24`。
- [首次 CI](https://github.com/aderan/orx/actions/runs/44686128108)
  揭示四处彩色帮助文本断言和三处依赖 BSD `ls` 行为的断言。
  提交 `8a920b0bcb9706c2da1ec05a4d8d25293b8b6256` 调整测试的
  文本和测试命令。七项本机复验、远端 Linux ORX 测试与安装、
  远端三组 Analytics 测试与安装通过。 [该提交的 CI](https://github.com/aderan/orx/actions/runs/44686632342)

## 旧仓归档

- 用户确认从源码切换点归档旧仓。旧仓 README 提交
  `0d724c27c50c07aebb66f4192ced8b6995649e69` 说明源码已合入 ORX，
  后续随 ORX 迭代，并提供新的安装、App 和 issue 入口。GitHub
  API 已确认旧仓为归档只读。 [旧仓 README](https://github.com/aderan/orx-analytics#readme)
- 旧仓 `main` 仅追加 README，旧标签的远端映射与两版 Release
  资产清单前后逐字节一致。归档后重下两个 App zip，与归档前的
  受验文件逐字节一致。原独立仓本地 0.4.4 提交通过已提交的
  [导入基线 Git bundle](baseline.md) 继续可复验。

## 下载与回退

- 旧 Analytics v0.4.2、v0.4.3 的 App zip、wheel 和 sdist 从
  GitHub 下载，分别与各自的 SHA-256 清单核对。两个清单共同引用
  的 ORX v0.4.0 wheel 也下载核对。 [历史下载](https://github.com/aderan/orx-analytics/releases)
- 源码入口上线时，ORX Releases 上已有 v0.4.0。0.5.0 App 的
  内部预览只在本机完成构建和自动验收。正式安装包及实际注销重登
  仍待验收，回退步骤见 [App 安装指引](../../../tools/orx-analytics/docs/app-installation.md)。
