import Foundation

/// User-facing runtime preferences (design §8/§9).
///
/// Every default is the conservative choice:
/// - notifications OFF — system permission is requested only after the
///   user opts in (design §4: 通知默认关闭，用户开启后再申请系统权限);
/// - no data-directory override — the app shares the existing analytics
///   data-dir with the CLI instead of keeping a second copy (design §8);
/// - panel-open refresh at ~30s (G012 用户反馈 4: lowered from the
///   original ~5s default; the engine's matching tiers are 30/60/300s).
public struct AppSettings: Equatable {
    public static let defaultPanelRefreshInterval: TimeInterval = 30
    /// The pre-G012 default. Persisted values equal to it are treated as
    /// the OLD default (not an explicit choice) unless the choice marker
    /// says otherwise — 0.4.2 wrote it incidentally on every settings save.
    public static let legacyDefaultPanelRefreshInterval: TimeInterval = 5

    public var notificationsEnabled: Bool
    /// Explicit data-directory choice (设置页显式选择); nil = the shared
    /// default resolution (ORX_ANALYTICS_HOME env, else ~/.orx-analytics).
    /// Applies on the next app launch — changing it never moves data.
    public var dataDirectoryOverride: String?
    /// Panel/detail-open refresh tier in seconds (design §8; G012 lowered
    /// the default to 30s — 面板或详情打开 ~30s).
    public var panelRefreshInterval: TimeInterval
    /// True only when the user explicitly chose the refresh preference
    /// (设置页). Unmarked persisted values are migration candidates, not
    /// guaranteed preferences.
    public var panelRefreshIntervalChosen: Bool

    public static let `default` = AppSettings(notificationsEnabled: false,
                                              dataDirectoryOverride: nil,
                                              panelRefreshInterval: defaultPanelRefreshInterval,
                                              panelRefreshIntervalChosen: false)

    public init(notificationsEnabled: Bool = false,
                dataDirectoryOverride: String? = nil,
                panelRefreshInterval: TimeInterval = AppSettings.defaultPanelRefreshInterval,
                panelRefreshIntervalChosen: Bool = false) {
        self.notificationsEnabled = notificationsEnabled
        self.dataDirectoryOverride = dataDirectoryOverride
        self.panelRefreshInterval = panelRefreshInterval
        self.panelRefreshIntervalChosen = panelRefreshIntervalChosen
    }

    /// The engine socket path honoring the resolution order: explicit
    /// user override > ORX_ANALYTICS_HOME environment > default home.
    public func resolvedSocketPath(environment: [String: String] =
                                    ProcessInfo.processInfo.environment) -> String {
        if let dir = dataDirectoryOverride, !dir.isEmpty {
            return dir + "/engine.sock"
        }
        return EngineSocketClient.defaultSocketPath(environment: environment)
    }

    /// The data directory itself (for display in settings).
    public func resolvedDataDirectory(environment: [String: String] =
                                        ProcessInfo.processInfo.environment) -> String {
        let socket = resolvedSocketPath(environment: environment)
        return (socket as NSString).deletingLastPathComponent
    }
}

/// Persistence surface for settings — injectable so tests (and the
/// smoke self-check) never touch the real user defaults domain.
public protocol AppSettingsStore: AnyObject {
    func load() -> AppSettings
    func save(_ settings: AppSettings)
}

/// UserDefaults-backed store. Unreadable/corrupt values fall back to the
/// conservative default rather than crashing or silently enabling
/// anything.
public final class UserDefaultsAppSettingsStore: AppSettingsStore {
    private static let notificationsKey = "notifications.enabled"
    private static let dataDirectoryKey = "data.directory.override"
    private static let intervalKey = "panel.refresh.interval"
    private static let intervalChosenKey = "panel.refresh.interval.chosen"

    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> AppSettings {
        // bool(forKey:) is NO for a missing OR non-boolean value — an
        // unparseable value keeps notifications off, never on.
        let enabled = defaults.bool(forKey: Self.notificationsKey)
        let override = defaults.string(forKey: Self.dataDirectoryKey)
        let interval = defaults.double(forKey: Self.intervalKey)
        let chosen = defaults.bool(forKey: Self.intervalChosenKey)
        // G012 migration: 新安装和未显式选择刷新偏好的升级用户采用新默认
        // 值；已明确保存的有效偏好保留. A persisted value is an explicit
        // preference when the choice marker is set, or when it differs
        // from the legacy default (pre-marker versions only ever wrote 5
        // incidentally — any other value descends from a deliberate pick).
        let effectiveInterval: TimeInterval
        let effectiveChosen: Bool
        if interval > 0 && (chosen || interval != AppSettings.legacyDefaultPanelRefreshInterval) {
            effectiveInterval = interval
            effectiveChosen = chosen
        } else {
            effectiveInterval = AppSettings.defaultPanelRefreshInterval
            effectiveChosen = false
        }
        return AppSettings(notificationsEnabled: enabled,
                           dataDirectoryOverride: (override?.isEmpty ?? true) ? nil : override,
                           panelRefreshInterval: effectiveInterval,
                           panelRefreshIntervalChosen: effectiveChosen)
    }

    public func save(_ settings: AppSettings) {
        defaults.set(settings.notificationsEnabled, forKey: Self.notificationsKey)
        defaults.set(settings.dataDirectoryOverride ?? "", forKey: Self.dataDirectoryKey)
        defaults.set(settings.panelRefreshInterval, forKey: Self.intervalKey)
        defaults.set(settings.panelRefreshIntervalChosen, forKey: Self.intervalChosenKey)
    }
}

/// Non-persisting store for tests and the smoke self-check.
public final class InMemoryAppSettingsStore: AppSettingsStore {
    private var settings: AppSettings
    private let lock = NSLock()

    public init(initial: AppSettings = .default) {
        self.settings = initial
    }

    public func load() -> AppSettings {
        lock.lock(); defer { lock.unlock() }
        return settings
    }

    public func save(_ settings: AppSettings) {
        lock.lock(); defer { lock.unlock() }
        self.settings = settings
    }
}
