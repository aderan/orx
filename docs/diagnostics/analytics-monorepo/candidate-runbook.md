# 同仓候选与历史复验

在 ORX 根目录执行以下命令。macOS 候选要求 Apple Silicon 和 Swift，Python 3.12 的开发依赖由 uv 准备。

```sh
uv run --locked python scripts/joint_release.py monorepo --commit HEAD \
  --work-dir /tmp/orx-candidate
cd /tmp/orx-candidate
shasum -a 256 -c SHA256SUMS.txt
```

工作目录应为空。脚本从指定提交创建一次性检出，构建 ORX 和 Analytics 的 sdist 与 wheel，验证隔离安装，生成用于 Swift 解码测试的真实 Python 线格式夹具，构建并验签 App。端到端步骤用安装产物驱动 ORX 执行并用 App 内置引擎查询。输出 `candidate.json` 记录两包和应用版本、三个契约版本、产物哈希与测试结果，原始日志在 `logs/`。默认构建内部预览，正式渠道需要 `--channel official --decision <file>` 及原有签名、公证凭据。构建可用 `ORX_APP_RUNTIME_CACHE` 指向固定运行时缓存，仍会核对内置 Python 的固定哈希。

历史复验使用 [G011](../g011/joint-acceptance-runbook.md) 和 [G012](../g012/joint-acceptance-runbook.md) 的原样本。G011 的 ORX 检出设为 `v0.4.0`，Analytics 旧仓检出设为 `v0.4.2`；G012 的 ORX 产品仍固定 `v0.4.0`，其 Analytics 产品检出为 `v0.4.3`。两种组合使用旧脚本约定的 `.release-workspaces/orx-analytics`，不以同仓 Analytics 替代旧提交。这些历史用例和需要既有实际 ORX 状态库的 M1.2 用量测量保留其原检出和输入要求。日常 CI 执行其余 ORX 可重复测试、Analytics Python 矩阵，并从单一冻结提交执行 macOS 联合检查。
