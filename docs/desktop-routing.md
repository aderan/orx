# 桌面路由配置接口

`orx config routing --json --scope user` 查看用户层生效配置。
`orx config routing --json --scope project --project-root /absolute/project`
读取该项目叠加用户和内置默认后的配置。命令按绝对路径确定项目，不读取
运行状态库。响应包含 `contract=1`、ORX 版本、`revision`、目标路径、
值和来源、候选顺序、模型档位及推理强度。

`model_selection.controller` 中的 `session_harness`、`native_agents` 和
`source` 表示本次 CLI 查询声明的宿主。兼容配置 `candidate` 仍来自
`controller.profile`。Finder 配置查询可以没有宿主。要检查指定宿主，
可在 `config` 前传 `--host` 和可选的 `--host-agent`。

候选按配置顺序返回。`declared_harness` 保存声明值，`harness` 可按
本次宿主解析。`host_mode`、`agent_ref` 和 `host_blocked_reason`
说明宿主执行方式及本次宿主限制。ModelSpec、执行能力、T0–T3 或 S、
请求 effort 与可选的 `native_effort` 覆盖独立展示。Planner 的
`depth` 是不带显式 `--depth` 时的默认值，取 `light`、`standard` 或 `deep`。

把编辑内容保存为 JSON 请求，`revision` 取自刚读取的对应作用域：

```json
{
  "revision": "从响应取得的 revision",
  "changes": {
    "plan.depth": "standard",
    "worker.profiles": ["first-profile", "second-profile"]
  }
}
```

用同一命令追加 `--request /absolute/request.json` 校验和预览。
预览成功后在相同作用域追加 `--apply` 保存。接受的字段为
`controller.profile`、`plan.depth`、`plan.light.profiles`、
`plan.standard.profiles`、`plan.deep.profiles`、`worker.profiles`
和 `verify.profiles`。ORX 验证 profile 引用，将本次修改原子写入一个
文件，并在替换前复查配置和 profile 文件。草稿过期时重新读取再提交。
成功后桌面端重新读取生效配置。已有的执行和用量继续按当次事实显示。

当次派发使用 Analytics 的任务详情接口。该接口从只读镜像获取
已关联路由日志中的 Controller、当次候选及其过滤结果，以及尝试记录中
实际模型、推理和推理来源。新增字段是可选的。旧记录或旧引擎缺失字段时，
桌面仍能显示现有历史数据。
