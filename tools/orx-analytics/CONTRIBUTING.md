# 开发与验证

需要 Git 和 Python 3.11+。沿用 setuptools 构建和 pytest 测试，运行时
保持零第三方依赖；`build` 仅用于验证可分发的源码包与 wheel。

```sh
git clone https://github.com/aderan/orx-analytics.git
cd orx-analytics
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -o addopts= -q
.venv/bin/python scripts/check_install.py
git diff --check
```

源码使用 `src/` 布局，开发前安装 editable 包。`python -m orxanalytics`
和 `orx-analytics` 进入同一个 CLI。仓库目前没有独立 formatter/linter
配置；沿用现有 Python 风格，不绕过测试或提交钩子。

## 改动流程

复杂工作拆为 3–5 个阶段，写入 `IMPLEMENTATION_PLAN.md`，注明目标、
可验证结果、测试与状态。先理解现有实现，行为改动先补失败用例，再做
最小实现。每阶段验证并提交可工作的代码，全部完成后移除临时计划。
同一问题最多三次失败尝试后，记录证据并重新评估方法。

测试在公开行为处验证结果：只读采集、报告口径、关联依据、发现去重、
包内复算或安装后的 CLI。使用 `tests/conftest.py` 的合成来源，不读取
开发者的真实 agent 会话，不调用模型、不弹系统通知。通知行为使用
受控 subprocess 测试。watch 默认采集路径在测试中也被隔离。

## 安装验收

`scripts/check_install.py` 在临时目录中：

1. 复制可分发的源码、文档、测试和检查脚本，排除本地数据与缓存。
2. 使用 PyPA build 构建 sdist，再从 sdist 构建 wheel。
3. 检查 README、版本、Python 要求、运行依赖与分发内容。
4. 在新虚拟环境中离线安装 wheel，从仓库外调用两个 CLI 入口。
5. 验证版本/帮助不创建数据，登记包含空格的项目路径，执行隔离采集，
   并检查 `--data-dir` 覆盖环境变量。

build 的隔离环境可能需要网络下载构建依赖；wheel 安装阶段明确使用
`--no-index --no-deps`。这证明安装后的工具不依赖源码 checkout 或其他
运行包。构建过程见 [PyPA build 文档](https://build.pypa.io/en/stable/)。

## 发布检查

版本只在 `src/orxanalytics/__init__.py` 的 `__version__` 中定义，包元数据
与 reader 标识从它派生。改版本时同步 CHANGELOG 和文档中的版本示例。
ORX schema、analytics store 版本和软件版本分别管理。

扩大来源支持时同步 [兼容文档](docs/compatibility.md) 与合成来源回归。
用量语义变化时同时检查 metrics、anomalies、bundle 和 `recompute.py`，
既有未知值不补零。不要修改历史报告来伪装新的测量结果。

提交前运行完整测试、安装检查和 `git diff --check`。GitHub CI 同样执行
这些测试和安装检查；当前矩阵覆盖 Linux/Python 3.11、macOS/Python 3.12、
Linux/Python 3.14。GitHub main 是当前分发来源；需要固定版本时记录
commit SHA。PyPI 发布或 tag/release 流程另行安排，不属于现有 CI 的自动行为。

运行数据库、原始采集数据、证据包、虚拟环境和本地执行状态不进入 Git。
使用 `var/` 或仓库外的数据目录保存这些产物；`reports/` 只保存经检查的
报告。公开仓库前也要检查提交历史，删除最新版本里的文件不等于删除历史。
0.4.1 首次公开发布前，GitHub push protection 检出了历史运行数据库中的
密钥；发布历史已移除生成数据与虚拟环境，原始完整历史备份仅在本机保留。
