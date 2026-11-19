# 阶段一验收

2026-11-19（北京时间），Apple Silicon / macOS 26.6.2，Python 3.12.15。机械导入为 `2dd49d9`，Python 打包适配为 `e516ff7c9fc558d17941a693f4a9d1d7f8ac1c07`。原工作区继续保留原任务与改动。

```sh
git clone --no-local <ORX-repo> <scratch>/checkout
cd <scratch>/checkout
git checkout e516ff7c9fc558d17941a693f4a9d1d7f8ac1c07
git status --porcelain
git ls-tree -r HEAD | awk '$1 == "160000"'
git submodule status
uv build --out-dir <scratch>/orx
uv run --locked python scripts/check_release.py
cd tools/orx-analytics
uv run --no-project --python 3.12 --with-editable '.[dev]' python scripts/check_install.py
uv build --out-dir <scratch>/analytics
git status --porcelain
```

- 检出前后工作树状态为空，无 gitlink、submodule 或 `.release-workspaces`。导入索引与原树的 114 个文件模式、blob 和 SHA-256 全部一致。源 bundle 的依赖及校验见 [基线](baseline.md)。
- ORX 的安装检查报告 `problems=[]`。安装 wheel 后完成确定性执行闭环，验证原生角色和技能资源。历史 schema 8 夹具可从真实旧代码再生，升级后原行保持，真实旧版拒读并可从备份恢复。
- Analytics 从 sdist 构建 wheel 并离线安装，两个 CLI 入口都报告 0.4.4。目标虚拟环境中无法解析 `orx`，仍成功导入独立 Codex 会话，记录 1,000 输入、100 输出和 1,100 总 token。模拟来源的内容保持一致。
- ORX sdist 回归检查先复现 Analytics 和 App 文件被带入，修正后仅包含 ORX 自己的源码及所需打包资源。

本轮生成的产物校验如下。它们是本地构建证据。

| 文件 | SHA-256 |
| --- | --- |
| `orx_agent-0.4.0-py3-none-any.whl` | `8b1a650b4b0e0cda57332d974411d3052bbe01ee6d16ffb60db1b9cd04cf99b2` |
| `orx_agent-0.4.0.tar.gz` | `2b9a25a86e7f0d51c05cc649897d52ff6c1fe2eb9098081161ec5745ff9b5da1` |
| `orx_analytics-0.4.4-py3-none-any.whl` | `f93a0d0f2079c6bfdf8f75ff3b548f077aa94b9a39c9d2a9b77bcd6a25422f26` |
| `orx_analytics-0.4.4.tar.gz` | `dcbeeea44eff04a6cf6b4a4a9dd050c4ae08445431ee3ea30713a4005712d4f0` |
