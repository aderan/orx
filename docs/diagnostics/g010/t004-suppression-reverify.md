# G010 T004：权限修复后的显式复验与抑制重启用（2026-10-16）

任务：先复现被抑制 profile 无法再次运行的行为，再消费 G009 已有权限
配置指纹，记录阻塞时条件及复验事实，为对应任务和 profile 提供有界
重启入口；保持原有故障分类、配额门禁和恢复身份规则。

上游缺口记录：docs/recovery-hardening-report.md T008「抑制的重启用
缺少配置指纹消费者」——同任务同 profile 的抑制解除原本只能靠该
profile 记录一次异指纹/异类结果，但被排除的 profile 无法再被启动来
记录该结果（鸡生蛋）；T007 的权限配置指纹（preflight）当时无任何
机制消费。T008 给出的候选方案即本任务实现的形态：显式复验入口消费
「条件指纹变化」。本任务零改动 G009 的故障分类、配额/配额域门禁与
恢复身份规则——它们全部按原样被复用（详见下文「不变量」）。

## 复现（先红后绿的起点）

`tests/test_routing.py::test_suppressed_profile_stays_excluded_until_the_explicit_reverify`
把缺口钉成默认语义：cursor profile 连续 3 次同条件权限阻塞后被抑制；
此时用户把真实权限配置从 allowlist 改为 unrestricted（磁盘上的有效
指纹确实变了），再跑两次 `orx run` + task retry——profile 仍不会被
重新启动（仍 3 个 attempt），停止仍以 suppressed_blocker 解释，任务
停在 runnable。修复配置本身不改变任何路由决定：没有任何机器消费
指纹。该测试实现前即绿（它钉的是旧行为），实现后仍绿（新语义要求
显式复验才解除）——它是默认路径的永久回归钉。

新增行为用例实现前全红（`dispatch.task_reverify` 不存在，
AttributeError）：routing 8 例 + e2e 2 例；实现后全绿。

## 交付形态

CLI：`orx task reverify <task> --profile <name> --reason <what changed>`
（dispatch.task_reverify）。退出码：0=做出决定（lifted 或 refused，
refused 是被记录的诚实否定而非错误）；1=任务/profile 未知或生命周期
门禁（paused/cancelling/cancelled）；2=输入类（reverify_reason_invalid
/reverify_profile_required，1-512 字符纪律沿恢复面）。

数据面零 schema 变更：三个追加式 task_event 携带机器事实（reason 列
存 JSON，读取端容忍不可解析载荷，从不改写历史）：

- `suppression_baseline`：抑制生效时（run_slice 里 suppression 非空）
  每个可预检 profile 按阻塞 episode（blocker fingerprint）记录一次
  有效配置指纹+来源+CLI 身份+probed_at。origin=block_time。
- `suppression_reopen`：复验通过（lift）。记录双侧指纹、来源、操作者
  重启原因、attempt 水位（attempts_at_reopen）。
- `suppression_reverify_refused`：每次拒绝的审计行（code + 当时指纹）。

消费端：`_suppressed_blockers` 对每个达到上限的 (task, profile) 先查
`_suppression_reopen_active`——最新 reopen 的 attempt 水位等于该
profile 当前 worker attempt 数 ⇒ reopen 未消费（排除不生效）；该
profile 下一次任何结果的 attempt 使水位前移 ⇒ reopen 消费，尾部
streak 重新独立裁决。三处调用面（run_slice、task_retry、资源重路由）
共享同一函数，语义一致。

## 判定规则（有界性）

1. 前置：该 (task, profile) 的原始 streak ≥ 上限（未达标如实拒绝
   not_suppressed；未消费的 reopen 不改变此前置）。
2. 生命周期门禁先行（paused/cancelling/cancelled 在任何记录之前
   抛 GoalPausedError/GoalCancellingError，exit 1）。
3. host/shell harness 无权限配置可消费：拒绝
   harness_not_preflightable，步骤给路由改序/host 接手/replan。
4. 缺少阻塞时基线（G010 前的抑制、或当时无可读配置）：拒绝
   no_baseline_recorded，同时把当前有效配置记录为补充基线
   （origin=reverify_capture）——之后只有「相对该基线的变化」才能
   解除；明确补证步骤原文在 payload 的 steps。
5. 相同有效指纹：拒绝 same_effective_fingerprint；仅 mtime 变化时
   如实标注 mtime_changed=True 并说明「touch 不是条件变化」
   （复用 probes.preflight_invalid_reasons 的 mtime tripwire，指纹
   比较只看有效轴值+来源）。
6. 已复验过且失败过的同一指纹：拒绝 config_already_reopened——
   每个不同有效指纹恰一次机会（对本任务该 profile 的全部历史
   reopen 集合查重）。
7. 全部通过 ⇒ lift：记录 reopen；不启动任何 worker、不建 attempt、
   不动任务状态；重启由下一次 `orx run` 正常派发执行（新 attempt；
   host 指派则新 nonce）。结果显式声明 preflight 通过不是执行成功
   （cannot prove in-agent shell execution），且同阻塞复发时从记录
   streak 恢复有界抑制。

指纹绑定与 T007 完全一致（harness、CLI 二进制+版本、profile/model/
effort、role=worker、workspace=项目根、任务命令验证条目首 token、
五轴值+来源、源文件 axes 摘要）；probed_at 与 mtime 不入哈希——
基线与复验两侧输入相同，比较才有意义。remedy 文案（每个抑制面共用）
已补 `orx task reverify` 用法（config 分支的机器入口）。

## 不变量（原有语义保持）

- 故障分类（health.failure_class / blocker_fingerprint / streak）
  零改动；代码红永不抑制的规则不动。
- 配额/资源门禁独立于 lift：解除抑制的 profile 仍受 resource_status
  与 quota_domain 门禁（测试：lift 后置 exhausted，停止原因变为
  exhausted 而非 suppressed_blocker，任务保持 runnable）。
- 恢复身份规则不动：复验不建 attempt/nonce/session；迟到交付与
  stale 门禁原样。
- 作用域恰为 (task, profile)：他任务的 streak、他 profile、历史失败
  行全不动（测试逐项断言，含 attempts 结果列表原样）。
- 生命周期：paused/cancelling/cancelled 复验拒绝且零写入（e2e 测试
  断言 seam 事件为空、attempt 数不变）；Analytics 无任何新调度权。

## 验证（全部确定性，零付费模型调用）

- 红基线：新增 10 例中 9 例红（1 例为复现钉，按设计实现前即绿）。
- 门禁三文件：tests/test_probes.py + test_health.py + test_routing.py
  + test_state.py → 172 passed；tests/test_host_recovery_facts.py +
  test_host_progress_e2e.py + test_delivery_gate_e2e.py → 64 passed
  （含 CLI 面 1 例新增）。
- 全量 `.venv/bin/python -m pytest -q` → 1124 passed（T003 基线
  1115 + 新增 9；复现钉含于其中），零失败零新增 skip。
- `git diff --check -- . ':!docs/m1.2-report.md'` 干净。
- 指纹消费为真实只读预检（读真实配置文件 + `--version` 探测）；
  测试用隔离 HOME + PATH 假 agent 使其完全确定性，无网络。

## 用户未提交文件披露

工作区的 docs/m1.2-report.md（用户未提交改动）在本任务全量验证后
sha256 仍为 T001 postscript 记录的
5289e6e713483ae1930513a4443967b90906d5a3433f7493770e5a52b89d0fa5
（本轮全量未进一步改变其字节）；该文件保持未暂存、未提交、不属于
本任务任何提交，prescribed 检查以 `:!docs/m1.2-report.md` 排除之。

## 限制（如实）

- reopen 生效期间（lift 后、该 profile 下一次 attempt 前）不再复验
  当前配置是否仍等于复验时指纹——与 operator override 同界的操作者
  责任窗口；消费点（下一次 attempt）之后全部回到记录事实裁决。
- mtime-only 在拒绝分支如实标注，但基线 mtime 以 JSON 浮点往返存储
  （st_mtime 精度内比较，跨文件系统精度差异只影响标注不影响判定——
  判定只依赖指纹）。
- 复验不预测执行成功：lifted 后首次重启若因新的（不同文本）阻塞失败，
  streak 以新 fingerprint 重新计数，下一 episode 的基线在其抑制生效时
  捕获——这正是「新条件下仍失败时恢复有界抑制」的路径，已由
  config_already_reopened 规则配合封住同一指纹的无界重试。
