import AppKit
import Combine
import CoreServices
import SwiftUI
import ORXAnalyticsCore

/// The installed app and its SwiftUI views share one runtime.
@MainActor
private enum AppRuntime {
    static let settingsStore: UserDefaultsAppSettingsStore = {
        let bundle = Bundle.main
        if let current = bundle.bundleIdentifier,
           let previous = bundle.object(forInfoDictionaryKey: "ORXAnalyticsPreviousBundleIdentifier") as? String {
            UserDefaultsAppSettingsStore.migrateBundlePreferences(from: previous, to: current)
        }
        return UserDefaultsAppSettingsStore()
    }()

    static let model: AppModel = {
        // Honor the user's explicit data-directory choice (settings) —
        // resolution order: explicit override > ORX_ANALYTICS_HOME >
        // default home (design §8).
        let initial = settingsStore.load()
        let client = EngineSocketClient(socketPath: initial.resolvedSocketPath())
        // The app launches and manages the engine through the bundled
        // standalone Python runtime (design §9); in `swift run` without
        // a bundle the documented dev override env vars apply.
        let launcher = EngineProcessManager(bundleURL: Bundle.main.bundleURL)
        let model = AppModel(client: client,
                             cache: OverviewCacheStore.makeDefault(),
                             settingsStore: settingsStore,
                             notifications: SystemNotificationCenter(),
                             engineLauncher: launcher)
        // App start (not just the first panel open) ensures the engine
        // and primes the menu-bar badge. Still cache-first: nothing
        // waits for this round; the panel renders cache whenever it
        // opens (design §3/§9).
        Task { await model.refresh(reason: "app-start") }
        return model
    }()
}

@main
struct ORXAnalyticsApp {
    @MainActor
    static func main() {
        if CommandLine.arguments.contains("--smoke-test") {
            exit(SmokeRunner.runAndReport() ? 0 : 1)
        }

        let app = NSApplication.shared
        let delegate = ORXApplicationDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor
private final class ORXApplicationDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private lazy var model = AppRuntime.model
    private lazy var windows = AppWindowController(model: model)
    private let popover = NSPopover()
    private var statusItem: NSStatusItem?
    private var badgeObservation: AnyCancellable?

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMainMenu()
        RuntimeCoordinator.shared.start(model: model, openRunDetail: { [weak self] in
            self?.windows.showMain()
        })
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Control Center owns visibility. AppKit's item can be hidden
        // without terminating the application or closing its windows.
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        item.button?.target = self
        item.button?.action = #selector(toggleQuickPanel)
        item.button?.imagePosition = .imageLeading
        item.button?.setAccessibilityLabel("ORX")
        popover.behavior = .transient
        popover.delegate = self
        badgeObservation = model.$cachedOverview.sink { [weak self] value in
            self?.updateBadge(value)
        }
    }

    func applicationShouldOpenUntitledFile(_ sender: NSApplication) -> Bool {
        let event = NSAppleEventManager.shared().currentAppleEvent
        let launchType = event?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue
        return launchType != OSType(keyAELaunchedAsLogInItem)
    }

    func applicationOpenUntitledFile(_ sender: NSApplication) -> Bool {
        windows.showMain()
        return true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        windows.showMain()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.shutdown()
    }

    private func updateBadge(_ overview: CachedOverview?) {
        guard let button = statusItem?.button else { return }
        let summary = overview?.envelope.summary
        let active = summary?.activeRuns ?? 0
        let attention = summary?.attention ?? 0
        button.image = NSImage(systemSymbolName: attention > 0
                               ? "exclamationmark.octagon.fill" : "circle.grid.2x2",
                               accessibilityDescription: "ORX")
        button.image?.isTemplate = true
        button.title = active > 0 ? MetricsFormat.countText(active) : ""
        button.toolTip = "ORX · 进行中 \(active) · 需要关注 \(attention)"
    }

    @objc private func toggleQuickPanel() {
        guard let button = statusItem?.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        let panel = QuickPanelView(
            openRunWindow: { [weak self] in
                self?.popover.performClose(nil)
                self?.windows.showMain()
            },
            openDiagnostics: { [weak self] in
                self?.popover.performClose(nil)
                self?.windows.showDiagnostics()
            },
            openSettings: { [weak self] in
                self?.popover.performClose(nil)
                self?.windows.showSettings()
            }
        )
        .environmentObject(model)
        .frame(width: 360)
        popover.contentViewController = NSHostingController(rootView: panel)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    func popoverDidClose(_ notification: Notification) {
        popover.contentViewController = nil
    }

    @objc private func showMain() { windows.showMain() }

    @objc private func showSettings() { windows.showSettings() }

    @objc private func undo() { NSApp.keyWindow?.firstResponder?.undoManager?.undo() }

    @objc private func redo() { NSApp.keyWindow?.firstResponder?.undoManager?.redo() }

    private func installMainMenu() {
        let mainMenu = NSMenu()
        let application = NSMenu(title: "ORX")
        application.addItem(withTitle: "打开 ORX", action: #selector(showMain), keyEquivalent: "o")
            .target = self
        application.addItem(withTitle: "设置…", action: #selector(showSettings), keyEquivalent: ",")
            .target = self
        application.addItem(.separator())
        application.addItem(withTitle: "退出 ORX", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let applicationItem = NSMenuItem()
        applicationItem.submenu = application
        mainMenu.addItem(applicationItem)

        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: #selector(undo), keyEquivalent: "z").target = self
        edit.addItem(withTitle: "重做", action: #selector(redo), keyEquivalent: "Z").target = self
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editItem = NSMenuItem()
        editItem.submenu = edit
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }
}
