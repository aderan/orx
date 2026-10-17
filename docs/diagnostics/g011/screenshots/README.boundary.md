# 截图目录边界说明（T007）

本目录为空（无 .png）。验收窗口内（2026-10-17 23:45 – 2026-10-18 00:59
本地，含 25+30 分钟两次有界等待）操作者桌面会话**始终处于锁定状态**
（锁屏 shield 常驻；00:01:57 的唯一活动是登录窗口切换到另一用户头像，
不是本会话解锁，已用改进探针排除）。锁定会话下：

- 全屏 `screencapture` 得到的是锁屏/登录窗像素；
- 窗口级 `screencapture -l<id>` 返回 "could not create image from window"；
- 状态项点击经 System Events 可寻址，但 WindowServer 不合成用户会话
  窗口（面板不出现）。

已取得的**非像素真实 UI 证据**（见 dist/g011/gui-acceptance/）：
菜单栏状态项读数（System Events："2"，与引擎 active_runs 一致）、
面板窗口存在性（CGWindowList：ORX Analytics 360×643）、以及面板数据
源（引擎 socket overview）逐态分层事实。不以锁屏像素或模拟图冒充
App 界面；视觉核对以 docs/diagnostics/g011/joint-acceptance-report.md
§9 的边界记录为准。

修订复跑轮（rev4）补充事实：命令门禁复跑与验证期间（2026-10-18
01:41 与 01:48 本地两次探针）会话仍处锁定
（CGSSessionScreenIsLocked = true），像素截图在本轮同样物理不可得；
独立视觉验证者的 fail 判定（T007 attempt 209，会话锁定为由）为终局
记录、保留不动。解锁后约 10 秒的人工复核清单见
joint-acceptance-report.md §9.1（菜单栏徽标、面板控制/执行分层
徽标、历史与详情分层；复核结果随发布说明移交）。
