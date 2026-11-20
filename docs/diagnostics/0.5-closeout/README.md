# ORX 0.5.0 收尾验收

2026-11-20，当前 Codex 会话主控，项目 G013 / R013。当前记录为执行中，尚未创建 0.5.0 Release。

## 已完成

- 首轮 Claude Code sonnet 规划遗漏读取端功能，执行前由主控重规划为 revision 2；原 revision 没有 Worker 执行。
- ZCode GLM-5.3 / low 完成 Analytics T001（attempt 260）及 Swift T002（attempt 262）。独立 Codex CLI 分别验证通过（261、263）。文档任务首次审查指出旧目录迁移步骤缺失，修复后复审通过（267），迁移示例四项测试通过。上游实际 effort 未作为已证明事实。
- 主控补齐跨 Run 修订归属与无 Worker 的兼容边界。Analytics 共 278 项测试通过；跨包 5 项用实际 ORX 状态机生成临时库，与 Store.verifications_current 逐项对照，并核对导入前后源库 SHA-256 不变。
- Swift 带真实 Python wire 夹具的 182 项全部通过、零跳过；App 打包 39 项通过。
- 真实 R013/T001 的原始验收行 742–744 为历史、745–746 为采集时当前窗口。生产 taskBreakdown 在临时复制中离屏渲染为 380/760 pt、浅/深色四图并目视复核；另以删掉新增字段的旧引擎响应核对有效性未知。导出只替换入口和根 body，不修改生产布局实现，不操作用户的桌面或设置。
- 桌面自动化工具将已安装包解析为旧 Bundle ID，无法接管窗口；不把离屏渲染写为已安装界面的点击验收。
- ORX 首轮 1309 passed / 1 failed，修正 G008 文档测试的章节边界后 14 项通过。并发实库重跑为 1308 passed、两项现场快照失败；随后两项在静止实库和一致性临时副本都通过。隔离源码全量其余 1288 项通过，缺少原历史检出的 22 项在补齐依赖后所在的 29 项联合测试全部通过。完整 1310 项已分组覆盖，隔离全量汇总仍将重跑留单次结果。
- 旧 Analytics 仓库 API 当前为 archived=true，历史标签映射已重新读取，见 legacy-repository.json、legacy-tags.txt。

## 复验入口

```sh
uv run --locked pytest -q
uv run --no-project --with-editable './tools/orx-analytics[dev]' python -m pytest tools/orx-analytics/tests
uv run --locked pytest -q docs/diagnostics/0.5-closeout/check_evidence_contract.py
uv run --no-project --with-editable './tools/orx-analytics[dev]' python apps/macos/scripts/gen_wire_fixtures.py /tmp/orx-wire-closeout
ORX_WIRE_FIXTURES=/tmp/orx-wire-closeout swift test --package-path apps/macos
```

## 尚待完成

单提交冻结构建、候选逐项清单、安装候选的跨 CLI 依赖 Run 及最终对账。Cursor 切换命令在参数校验时被拒绝，T002 沿用 ZCode 完成；后续隔离 Run 补 Cursor，不将该次派发记为 Cursor。

实际注销重登、已安装 App 两项目界面保存、正式 Developer ID/公证仍按此前边界待验。未注销用户、未更改系统权限。外部发布在候选可审阅后处理。
