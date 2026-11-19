import SwiftUI
import ORXAnalyticsCore

/// The one model instance for the whole process. SwiftUI may construct
/// the App value more than once; routing every construction through the
/// same instance keeps the runtime coordinator (wake observer,
/// notification click router) and the UI bound to the same object.
@MainActor
private enum AppRuntime {
    static let settingsStore = UserDefaultsAppSettingsStore()

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
        RuntimeCoordinator.shared.start(model: model)
        // App start (not just the first panel open) ensures the engine
        // and primes the menu-bar badge. Still cache-first: nothing
        // waits for this round; the panel renders cache whenever it
        // opens (design §3/§9).
        Task { await model.refresh(reason: "app-start") }
        return model
    }()
}

/// ORX Analytics menu-bar app (design §3/§7/§9).
///
/// - `MenuBarExtra` is the persistent entry: icon + the in-progress Run
///   count (the number is FIXED to that meaning — never task/session/
///   token counts). An attention marker appears beside it when
///   attention > 0, explained in text inside the panel (not color only).
/// - `LSUIElement` (Resources/Info.plist) makes it a menu-bar-only app:
///   no Dock icon, no main window; closing windows keeps it resident.
/// - All data flows through the engine's socket protocol
///   (`ORXAnalyticsCore`); the Swift layer writes no business SQL and
///   implements no token math of its own.
@main
struct ORXAnalyticsApp: App {
    @StateObject private var model: AppModel

    init() {
        // Xcode-free smoke hook (scripts/smoke_macos_app.sh): verify the
        // protocol client, cache layer and frozen formatting rules
        // against a local simulator, then exit before any UI starts.
        if CommandLine.arguments.contains("--smoke-test") {
            let ok = SmokeRunner.runAndReport()
            exit(ok ? 0 : 1)
        }
        // AFTER the smoke early-exit so the self-check never touches the
        // real defaults domain, the notification center or the workspace.
        // SwiftUI constructs the App on the main thread, so the isolated
        // model bootstrap is safe to assume here.
        _model = StateObject(wrappedValue: MainActor.assumeIsolated { AppRuntime.model })
    }

    var body: some Scene {
        MenuBarExtra {
            QuickPanelView()
                .environmentObject(model)
                .frame(width: 360)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: model.attentionCount > 0
                      ? "exclamationmark.octagon.fill"
                      : "circle.grid.2x2")
                // The number is ALWAYS the in-progress Run count (§3),
                // rendered through the unified grouping formatter (G012).
                if model.activeRunCount > 0 {
                    Text(MetricsFormat.countText(model.activeRunCount))
                }
            }
            // The menu-bar label lives as long as the app does, so it
            // can donate its `openWindow` action for notification-click
            // routing (通知点击进入对应执行).
            .background(OpenWindowDonation())
        }
        .menuBarExtraStyle(.window)

        // T009/T010: the same window hosts the history query (查看全部
        // 执行) and the four-zone run detail of a selected run. Keeping
        // it a separate scene preserves "关闭窗口后继续驻留" — only the
        // panel's 退出 ends the app.
        Window("历史查询 / 执行详情", id: "run-detail") {
            RunDetailHostView()
                .environmentObject(model)
        }
        .windowResizability(.contentSize)

        // T011: the diagnostics window (引擎重复失败 / 单源失败入口).
        Window("诊断", id: "diagnostics") {
            DiagnosticsView()
                .environmentObject(model)
        }
        .windowResizability(.contentSize)

        Settings {
            SettingsView()
                .environmentObject(model)
        }
    }
}

/// Invisible helper that captures the label's `openWindow` environment
/// action into the runtime coordinator's box.
private struct OpenWindowDonation: View {
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .onAppear {
                RuntimeCoordinator.shared.windowActions.openRunDetail = {
                    // Notification deep links reuse the SAME fronting
                    // routing as panel card clicks (G012 用户反馈 2):
                    // activate + open/reuse + make key and front.
                    DetailWindowRouting.openRunDetailFrontmost(openWindow)
                }
            }
    }
}
