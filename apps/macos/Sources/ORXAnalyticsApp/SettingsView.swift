import SwiftUI
import ORXAnalyticsCore

/// 设置页 (design §8/§9):
/// - 通知: default OFF; enabling is the ONLY trigger for requesting the
///   system permission; the OS's actual verdict is displayed.
/// - 登录启动: default OFF, managed via SMAppService.mainApp, and the
///   displayed state is the SYSTEM's actual status — never an assumed
///   desired state.
/// - 数据目录: the shared analytics data-dir is shown; a custom one is
///   an EXPLICIT choice applying on the next launch.
/// - 刷新偏好: the panel-open refresh tier.
/// - 显示缓存: the separate, explicit display-cache cleanup. Quitting
///   or uninstalling never deletes ORX project data (design §9).
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var login = LoginItemController()
    @State private var confirmedClear = false
    @State private var pendingDataDir: URL?

    var body: some View {
        Form {
            notificationSection
            loginItemSection
            dataDirectorySection
            refreshSection
            cacheSection
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 560)
        .onAppear {
            login.refreshSystemState()
            Task {
                await model.refreshNotificationAuthorization()
                if model.settings.notificationsEnabled {
                    RuntimeCoordinator.shared.activateNotificationRouting()
                }
            }
        }
    }

    // MARK: 通知（默认关闭）

    private var notificationSection: some View {
        Section {
            Toggle("启用异常通知", isOn: notificationBinding)
            LabeledContent("系统权限状态") { Text(authorizationText) }
                .font(.caption)
        } header: {
            Text("通知")
        } footer: {
            Text("通知默认关闭；只有在你开启时才向系统申请权限。相同异常只提醒一次，同一轮多个异常合并为一条通知，点击通知打开对应执行。低置信度推测只在面板中显示，不发通知。")
                .font(.caption)
        }
    }

    private var notificationBinding: Binding<Bool> {
        Binding(
            get: { model.settings.notificationsEnabled },
            set: { on in
                Task { @MainActor in
                    if on {
                        await model.enableNotifications()
                        if model.settings.notificationsEnabled {
                            RuntimeCoordinator.shared.activateNotificationRouting()
                        }
                    } else {
                        model.disableNotifications()
                    }
                }
            })
    }

    private var authorizationText: String {
        switch model.notificationAuth {
        case .notDetermined:
            return model.settings.notificationsEnabled ? "已开启，等待系统确认" : "未申请（默认关闭）"
        case .granted:
            return "已授权"
        case .denied:
            return "系统已拒绝：请在 系统设置 → 通知 中允许 ORX Analytics"
        }
    }

    // MARK: 登录启动（默认关闭，显示系统实际状态）

    private var loginItemSection: some View {
        Section {
            Toggle("登录时启动", isOn: loginBinding)
            LabeledContent("系统实际状态") { Text(login.systemState.text) }
                .font(.caption)
            if let error = login.lastActionError {
                Text(error).font(.caption).foregroundStyle(.secondary)
            }
        } header: {
            Text("登录启动")
        } footer: {
            Text("默认关闭。通过系统的 SMAppService（登录项）管理；上面显示的是系统当前的实际登记状态，不以本应用的设置为准。开发环境直接运行的可执行文件无法登记，属正常现象。")
                .font(.caption)
        }
    }

    private var loginBinding: Binding<Bool> {
        Binding(
            get: { login.systemState.isEnabled },
            set: { login.setEnabled($0) })
    }

    // MARK: 数据目录（显式选择）

    private var dataDirectorySection: some View {
        Section {
            LabeledContent("当前数据目录") {
                Text(model.settings.resolvedDataDirectory())
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
            }
            if model.settings.dataDirectoryOverride != nil {
                Button("恢复默认数据目录") { model.setDataDirectoryOverride(nil) }
            }
            Button("选择数据目录…") { chooseDataDirectory() }
            if let pending = pendingDataDir {
                Text("已选择 \(pending.path)，重启应用后生效。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } header: {
            Text("数据目录")
        } footer: {
            Text("应用与 CLI 共用同一 analytics 数据目录（默认 ~/.orx-analytics，可用 ORX_ANALYTICS_HOME 覆盖）。自定义目录需要在此显式选择，更改在下次启动应用后生效，不会移动或复制任何数据。")
                .font(.caption)
        }
    }

    private func chooseDataDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "选择 analytics 数据目录（包含 engine.sock 的目录）"
        if panel.runModal() == .OK, let url = panel.url {
            model.setDataDirectoryOverride(url.path)
            pendingDataDir = url
        }
    }

    // MARK: 刷新偏好

    private var refreshSection: some View {
        Section {
            Picker("面板/详情打开时自动刷新间隔", selection: refreshBinding) {
                Text("5 秒").tag(TimeInterval(5))
                Text("10 秒").tag(TimeInterval(10))
                Text("15 秒").tag(TimeInterval(15))
                Text("30 秒（默认）").tag(TimeInterval(30))
                Text("60 秒").tag(TimeInterval(60))
            }
        } header: {
            Text("刷新偏好")
        } footer: {
            Text("面板或详情打开时的状态刷新节奏，默认 30 秒（详情窗口与面板共用同一偏好）。这只是状态检查频率，不代表 token 每隔该时间就有新数据；手动刷新随时立即生效。")
                .font(.caption)
        }
    }

    private var refreshBinding: Binding<TimeInterval> {
        Binding(
            get: { model.settings.panelRefreshInterval },
            set: { model.setPanelRefreshInterval($0) })
    }

    // MARK: 显示缓存（独立、明确的清理操作）

    private var cacheSection: some View {
        Section {
            if confirmedClear {
                Button("确认清理显示缓存", role: .destructive) {
                    model.clearDisplayCache()
                    confirmedClear = false
                }
            } else {
                Button("清理显示缓存…") { confirmedClear = true }
            }
        } header: {
            Text("显示缓存")
        } footer: {
            Text("缓存只影响面板显示速度。清理它不会触碰 ORX 项目数据，也不会删除 analytics 采集数据；退出或卸载应用同样不会删除任何项目数据。")
                .font(.caption)
        }
    }
}
