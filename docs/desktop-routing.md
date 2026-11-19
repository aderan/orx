# 桌面路由配置接口

`orx config routing --json --scope user` 查看用户层生效配置。
`orx config routing --json --scope project --project-root /absolute/project`
读取该项目叠加用户和内置默认后的配置。命令按绝对路径确定项目，不读取
运行状态库。响应包含 `contract=1`、ORX 版本、`revision`、目标路径、
值和来源、候选顺序、模型档位及推理强度。

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
