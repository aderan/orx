import AppKit
import SwiftUI
import ORXAnalyticsCore

struct SettingsView: View {
    private enum Page: String, CaseIterable {
        case general = "通用"
        case panel = "快捷面板"
        case data = "数据与缓存"

        var symbol: String {
            switch self {
            case .general: return "slider.horizontal.3"
            case .panel: return "menubar.rectangle"
            case .data: return "externaldrive"
            }
        }
    }

    @EnvironmentObject private var model: AppModel
    @StateObject private var login = LoginItemController()
    @State private var page: Page = .general
    @State private var directoryChanged = false
    @State private var confirmingClear = false

    var body: some View {
        HStack(spacing: 0) {
            navigation
            Rectangle().fill(ORXColor.border).frame(width: ORXLayout.stroke)
                .accessibilityHidden(true)
            ScrollView {
                VStack(alignment: .leading, spacing: ORXSpace.lg) {
                    ORXPageHeader(page.rawValue)
                    switch page {
                    case .general: general
                    case .panel: panel
                    case .data: data
                    }
                }
                .padding(ORXSpace.xl)
            }
        }
        .orxSurface()
        .onAppear {
            login.refreshSystemState()
            Task {
                await model.refreshNotificationAuthorization()
                if model.settings.notificationsEnabled {
                    RuntimeCoordinator.shared.activateNotificationRouting()
                }
            }
        }
        .orxCacheConfirmation(isPresented: $confirmingClear, onConfirm: model.clearDisplayCache)
    }

    private var navigation: some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text("ORX 设置")
                .font(ORXType.title)
                .padding(.horizontal, ORXSpace.sm)
                .padding(.bottom, ORXSpace.sm)
                .accessibilityAddTraits(.isHeader)
            ForEach(Page.allCases, id: \.self) { item in
                Button {
                    page = item
                } label: {
                    Label(item.rawValue, systemImage: item.symbol)
                        .font(ORXType.label).padding(ORXSpace.sm)
                }
                .buttonStyle(ORXRowButtonStyle(selected: page == item))
                .accessibilityAddTraits(page == item ? .isSelected : [])
            }
            Spacer()
        }
        .padding(ORXSpace.md)
        .frame(width: ORXLayout.settingsNavigationWidth)
        .background(ORXColor.inset)
    }

    private var general: some View {
        VStack(alignment: .leading, spacing: ORXSpace.lg) {
            settingsRow("外观主题", detail: "适用于 ORX 的窗口和快捷面板。") {
                Picker("外观主题", selection: Binding(get: { model.settings.appearance }, set: model.setAppearance)) {
                    ForEach(AppAppearance.allCases, id: \.self) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
            }
            settingsRow("菜单栏关注角标", detail: "有待关注项时，在 ORX 图标右上角显示提示。") {
                Toggle("菜单栏关注角标", isOn: Binding(
                    get: { model.settings.menuBarAttentionBadgeEnabled },
                    set: model.setMenuBarAttentionBadgeEnabled))
                    .toggleStyle(.switch).labelsHidden()
                    .accessibilityIdentifier("menu-bar-attention-badge")
            }
            settingsGroup("登录启动") {
                settingsRow("登录时启动", detail: login.systemState.text) {
                    Toggle("登录时启动", isOn: Binding(
                        get: { login.systemState.isEnabled }, set: login.setEnabled))
                        .toggleStyle(.switch).labelsHidden()
                }
                if let error = login.lastActionError {
                    ORXNotice(text: error, tone: .warning, symbol: "exclamationmark.triangle")
                }
            }
            settingsGroup("通知") {
                settingsRow("启用异常通知", detail: authorizationText) {
                    Toggle("启用异常通知", isOn: notificationBinding)
                        .toggleStyle(.switch).labelsHidden()
                }
                Text("相同异常只提醒一次。点击系统通知可打开对应执行。")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                if model.settings.notificationsEnabled && model.notificationAuth == .notDetermined {
                    Button("申请通知权限") { Task { await enableNotifications() } }
                        .buttonStyle(ORXButtonStyle())
                }
            }
        }
    }

    private var panel: some View {
        VStack(alignment: .leading, spacing: ORXSpace.lg) {
            settingsRow("卡片信息量", detail: "详细模式会增加已观测用量的覆盖说明。") {
                Picker("卡片信息量", selection: Binding(get: { model.settings.panelDensity }, set: model.setPanelDensity)) {
                    Text("极简扫读").tag(PanelDensity.compact)
                    Text("详细").tag(PanelDensity.detailed)
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
            }
            settingsGroup("固定入口") {
                ForEach(PanelShortcut.allCases) { shortcut in
                    Toggle(isOn: Binding(
                        get: { model.settings.panelShortcuts.contains(shortcut) },
                        set: { model.setPanelShortcut(shortcut, enabled: $0) }
                    )) {
                        Label(shortcut.title, systemImage: shortcut.symbol)
                    }
                    .toggleStyle(.checkbox)
                }
            }
        }
    }

    private var data: some View {
        VStack(alignment: .leading, spacing: ORXSpace.lg) {
            settingsGroup("数据目录") {
                Text(model.settings.resolvedDataDirectory())
                    .font(ORXType.code).textSelection(.enabled)
                HStack(spacing: ORXSpace.sm) {
                    Button("选择数据目录…", action: chooseDataDirectory)
                    if model.settings.dataDirectoryOverride != nil {
                        Button("恢复默认") {
                            model.setDataDirectoryOverride(nil)
                            directoryChanged = true
                        }
                    }
                }
                .buttonStyle(ORXButtonStyle())
                Text("应用与 CLI 共享 Analytics 数据目录。可由 ORX_ANALYTICS_HOME 覆盖默认路径。")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                if directoryChanged {
                    ORXNotice(text: "数据目录更改将在下次启动时生效。")
                }
            }
            settingsRow("刷新间隔", detail: "面板和 Run 详情共用此设置。手动刷新立即执行。") {
                Picker("刷新间隔", selection: Binding(
                    get: { model.settings.panelRefreshInterval }, set: model.setPanelRefreshInterval
                )) {
                    ForEach([5, 10, 15, 30, 60], id: \.self) { seconds in
                        Text(seconds == 30 ? "30 秒（默认）" : "\(seconds) 秒").tag(TimeInterval(seconds))
                    }
                }
                .labelsHidden().fixedSize()
            }
            settingsRow("显示缓存", detail: "清理后重新读取显示数据。项目和 Analytics 采集记录保留。") {
                Button("清理显示缓存…") { confirmingClear = true }
                    .buttonStyle(ORXButtonStyle(variant: .destructive))
            }
        }
    }

    private func settingsRow<Control: View>(_ title: String, detail: String,
                                            @ViewBuilder control: () -> Control) -> some View {
        HStack(alignment: .center, spacing: ORXSpace.lg) {
            VStack(alignment: .leading, spacing: ORXSpace.xs) {
                Text(title).font(ORXType.section)
                Text(detail).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            Spacer(minLength: ORXSpace.sm)
            control()
        }
        .orxCard()
    }

    private func settingsGroup<Content: View>(_ title: String,
                                              @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            Text(title).font(ORXType.title).accessibilityAddTraits(.isHeader)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var notificationBinding: Binding<Bool> {
        Binding(get: { model.settings.notificationsEnabled }, set: { on in
            Task { @MainActor in
                if on {
                    await enableNotifications()
                } else {
                    model.disableNotifications()
                }
            }
        })
    }

    private func enableNotifications() async {
        await model.enableNotifications()
        if model.settings.notificationsEnabled {
            RuntimeCoordinator.shared.activateNotificationRouting()
        }
    }

    private var authorizationText: String {
        switch model.notificationAuth {
        case .notDetermined:
            return model.settings.notificationsEnabled ? "等待系统授权" : "尚未申请系统权限"
        case .granted: return "系统已授权"
        case .denied: return "系统已拒绝。可前往「系统设置 → 通知」允许 ORX。"
        }
    }

    private func chooseDataDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "选择包含 engine.sock 的 Analytics 数据目录"
        if panel.runModal() == .OK, let url = panel.url {
            model.setDataDirectoryOverride(url.path)
            directoryChanged = true
        }
    }
}
