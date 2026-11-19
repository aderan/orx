import AppKit
import Foundation
import ORXAnalyticsCore
import UserNotifications

/// The REAL UserNotifications adapter — the only place the app touches
/// the system notification center. Lives in the app target so no test
/// path can ever fire a real notification (CONTRIBUTING: 不弹真实系统
/// 通知). Authorization is requested exclusively when the user toggles
/// notifications ON (design §4: 通知默认关闭，用户开启后再申请系统权限).
final class SystemNotificationCenter: NotificationPosting {

    func systemAuthorizationStatus() async -> NotificationAuthState {
        let settings = await center?.notificationSettings()
        guard let settings else { return .notDetermined }
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            return .granted
        case .denied:
            return .denied
        default:
            return .notDetermined
        }
    }

    func requestAuthorization() async -> Bool {
        guard let center else { return false }
        return (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
    }

    func post(_ notification: AttentionNotification) async {
        // Never prompts: only delivers when authorization was already
        // granted; anything else is silently dropped by the OS anyway.
        guard await systemAuthorizationStatus() == .granted else { return }
        let content = UNMutableNotificationContent()
        content.title = notification.title
        content.body = notification.body
        content.sound = .default
        content.threadIdentifier = "org.orxanalytics.attention"
        // Deep-link payload: 通知点击进入对应执行 (design §4).
        content.userInfo = ["project": notification.project,
                            "run_id": notification.runID]
        let request = UNNotificationRequest(
            identifier: "orx-attention-\(UUID().uuidString)",
            content: content, trigger: nil)
        try? await center?.add(request)
    }

    /// UNUserNotificationCenter requires a bundled app context; the
    /// embedded Info.plist provides one for SwiftPM runs, but stay
    /// defensive — a missing bundle identifier must not crash startup.
    private var center: UNUserNotificationCenter? {
        guard Bundle.main.bundleIdentifier != nil else { return nil }
        return UNUserNotificationCenter.current()
    }
}

/// App-runtime glue that must live exactly once per process:
/// - the sleep/wake observer (design §8: 睡眠期间不判定停摆，唤醒后
///   重新同步) — on wake the model re-syncs immediately;
/// - the notification click router (通知点击进入对应执行): a click sets
///   the app's run selection and opens the detail window;
/// - the captured SwiftUI `openWindow` action (the menu-bar label is
///   always alive, so it can safely donate its environment action).
final class RuntimeCoordinator: NSObject, UNUserNotificationCenterDelegate {
    static let shared = RuntimeCoordinator()

    /// The captured window-opening action, donated by the menu-bar label.
    final class WindowActionBox {
        var openRunDetail: (() -> Void)?
    }
    let windowActions = WindowActionBox()

    private weak var model: AppModel?

    private override init() {
        super.init()
    }

    /// Idempotent; called from the app entry point with the one model
    /// instance (AppRuntime keeps it identical across App re-inits).
    @MainActor
    func start(model: AppModel) {
        self.model = model
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(systemDidWake(_:)),
            name: NSWorkspace.didWakeNotification, object: nil)
        // Delegate routing is only wired once notifications were enabled
        // at least once — the UN center is never touched otherwise.
        if model.settings.notificationsEnabled {
            activateNotificationRouting()
        }
    }

    /// Wire click-through routing. Called after the user enabled
    /// notifications (and once at start when they were already enabled).
    func activateNotificationRouting() {
        guard Bundle.main.bundleIdentifier != nil else { return }
        UNUserNotificationCenter.current().delegate = self
    }

    @objc private func systemDidWake(_ note: Notification) {
        Task { @MainActor [weak self] in
            await self?.model?.handleWake()
        }
    }

    // MARK: UNUserNotificationCenterDelegate

    /// Show the banner even when the app is frontmost.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    /// 通知点击进入对应执行: open the run's detail window.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler:
                                @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let project = info["project"] as? String
        let runID = info["run_id"] as? String
        Task { @MainActor [weak self] in
            defer { completionHandler() }
            guard let self, let project, let runID else { return }
            self.model?.openRunDetail(project: project, runID: runID)
            NSApp.activate(ignoringOtherApps: true)
            self.windowActions.openRunDetail?()
        }
    }
}
