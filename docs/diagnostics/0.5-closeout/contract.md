# 0.5.0 收尾验收约定

本轮沿用当前用户目标“执行收尾工作。”及前次列出的四项收口。

## Analytics 只读证据

保留 tasks.get 的全部历史 verification 原始 passed 值和行身份，新增可选的逐行 current 和原因及任务窗口摘要。当前窗口按同一 snapshot、revision、task 的最新 Worker attempt 和最新 verification_context 的 verification_after 水位投影，与 ORX Store.verifications_current 的已记录窗口一致。现代 context 排除 attempt_id=NULL；旧 Worker 的行明确属于历史。缺少 context、context 不匹配当前 Worker、非法 JSON/版本/字段、不可复用输入均不得伪装成已确认有效；最新坏 context 不回退到较早的好 context。其他项目、Run 和修订不得混入。

摘要和 UI 明确它们基于最近采集到的 ORX 记录。Analytics 不自行调度、不修改 ORX 库、不重新计算未登记的文件指纹，也不承诺采集之后的文件仍未变。历史 first-pass 等既有指标保持其既定历史口径；当前有效行不能由历史 passed 计数替代。

回归至少包括：同交付补验保持已有通过；产物/依赖失效水位推进后旧行仍在但退出当前窗口；Worker 重试排除前次结果；无绑定 legacy 行；损坏 context；跨项目、Run、修订隔离；源库 SHA-256 不变。使用真实 ORX 行为生成的临时库交叉核对窗口，不能只用手写镜像证明兼容。

## 桌面

沿用 apps/macos/DESIGN.md，明确历史结果和当前窗口，未知保持未知。旧引擎缺新字段可解码并提示未知。检查明暗主题与最小宽度，不重设计图片中的执行列表。Goal 控制状态、Run 记录状态、当前计划任务进度和全部历史用量各保持原口径。

## 候选与发布

两个 CLI 和 App 软件版本 0.5.0；数据协议仍按各自常量声明。候选必须从单一提交构建，记录 hashes、独立安装和真实依赖 Run。整理当前安装、兼容与升级文档，保留 0.4.x 历史说明。已有源码切换、旧仓归档与下载对账重新核查。不注销当前账户，不把正式签名/公证或未实测的重登写为通过；候选准备完整后再决定外部分发。
