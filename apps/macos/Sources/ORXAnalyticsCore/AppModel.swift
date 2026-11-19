import Foundation
import Combine

/// Engine connection state as shown in the panel header. Distinct from
/// run status and data health (design §4: three independent dimensions).
public enum EngineConnectionState: Equatable {
    case idle
    case connecting
    case ready(engineVersion: Int)
    /// Protocol negotiation failed — reported explicitly, queries stopped.
    case incompatible(requested: Int, supported: Int)
    /// Socket unreachable — engine probably not running yet.
    case notRunning(String)

    public var isFatal: Bool {
        if case .incompatible = self { return true }
        return false
    }
}

/// Cache-first view state for the quick panel (design §3):
/// `bootstrap()` publishes the cached overview SYNCHRONOUSLY (if any),
/// then refreshes asynchronously. The panel is never blank while a
/// refresh is in flight, and the main thread is never blocked by IO.
@MainActor
public final class AppModel: ObservableObject {
    @Published public private(set) var cachedOverview: CachedOverview?
    @Published public private(set) var connection: EngineConnectionState = .idle
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var lastFailureText: String?
    /// When the last SUCCESSFUL network refresh landed (nil = cache only).
    @Published public private(set) var lastRefreshedAt: Date?
    /// The run whose detail window is open (nil = the all-runs entry
    /// list). Set before opening the window so the view keeps the
    /// project + run context (design §3: 保留项目和执行上下文).
    @Published public private(set) var selectedRun: RunIdentity?

    // T011 (design §4/§8/§9): settings, notifications, diagnostics,
    // sleep/wake resync.
    /// Current effective preferences (loaded from the store at init;
    /// conservative defaults when no store is injected — tests and the
    /// smoke path never touch the real defaults domain).
    @Published public private(set) var settings: AppSettings = .default
    /// The OS verdict for notification authorization; only meaningful
    /// after the user opted in (default: never asked).
    @Published public private(set) var notificationAuth: NotificationAuthState = .notDetermined
    /// Engine-round failure bookkeeping driving the diagnostics view.
    @Published public private(set) var diagnostics = DiagnosticsState()
    /// When the system last woke from sleep (design §8: 睡眠期间不判定
    /// 停摆；唤醒后重新同步).
    @Published public private(set) var wokeAt: Date?

    public let client: EngineClientProtocol
    public let cache: OverviewCacheStore
    /// Engine lifecycle owner (design §9: 引擎由 App 启动管理). nil in
    /// tests and the smoke self-check — those use simulators, never a
    /// real engine process.
    public let engineLauncher: EngineProcessManaging?
    /// Where preferences persist (nil = in-memory defaults).
    public let settingsStore: AppSettingsStore?
    /// The history query window's model. Owned here (not per-view) so
    /// results and filter state survive navigating into a run detail
    /// and back — the window keeps its context (design §3).
    public let historyModel: HistoryModel

    /// Serializes refresh rounds; multiple concurrent `refresh()` calls
    /// coalesce into the in-flight one (design §8: 多个刷新请求合并).
    private var refreshTask: Task<Void, Never>?
    private var autoRefreshTask: Task<Void, Never>?
    private let notifier: NotificationPosting?
    private var notificationPolicy = NotificationPolicy()

    public init(client: EngineClientProtocol, cache: OverviewCacheStore,
                settingsStore: AppSettingsStore? = nil,
                notifications: NotificationPosting? = nil,
                engineLauncher: EngineProcessManaging? = nil) {
        self.client = client
        self.cache = cache
        self.settingsStore = settingsStore
        self.engineLauncher = engineLauncher
        self.notifier = notifications
        self.historyModel = HistoryModel(client: client)
        if let settingsStore {
            settings = settingsStore.load()
        }
    }

    // MARK: - Badge (menu bar)

    /// The menu-bar number: in-progress Run count ONLY. It never switches
    /// to task counts, session counts or token sums (design §3), and it
    /// reads from whatever data is currently displayed — cache counts
    /// until a fresher fetch lands.
    public var activeRunCount: Int {
        cachedOverview?.envelope.summary.activeRuns ?? 0
    }

    /// Independent attention marker (text+icon, never color alone).
    public var attentionCount: Int {
        cachedOverview?.envelope.summary.attention ?? 0
    }

    // MARK: - Lifecycle

    /// Open path: publish cache first, refresh in the background. Never
    /// downgrades: once fresher in-memory data exists, re-opening the
    /// panel does not clobber it with an older cache file.
    ///
    /// G012: 重复打开面板不再重复立即刷新 — when a successful round landed
    /// within the refresh interval the data is fresh enough and the
    /// immediate round is skipped (the auto loop refreshes on cadence).
    /// Manual refresh (`refreshNow`) and wake resync stay immediate.
    public func bootstrap() {
        if cachedOverview == nil {
            cachedOverview = cache.load()
        }
        if let last = lastRefreshedAt,
           Date().timeIntervalSince(last) < settings.panelRefreshInterval {
            return
        }
        Task { await self.refresh(reason: "panel-open") }
    }

    /// User-visible refresh (button) — same coalescing.
    public func refreshNow() async {
        await refresh(reason: "manual")
    }

    // MARK: - detail navigation

    /// Point the detail window at one run and let the caller open it.
    public func openRunDetail(project: String, runID: String) {
        selectedRun = RunIdentity(project: project, runID: runID)
    }

    /// Show the all-runs entry list instead of a specific run.
    public func openAllRuns() {
        selectedRun = nil
    }

    /// One refresh round: negotiate -> request collection -> fetch the
    /// read model. Runs entirely off the main actor (the client's async
    /// wrappers hop to the engine IO queue), publishes on main.
    ///
    /// Coalescing: if a round is already in flight this call returns when
    /// THAT round returns — no second request is issued.
    public func refresh(reason: String) async {
        if let existing = refreshTask, !existing.isCancelled {
            _ = await existing.value
            return
        }
        let task = Task { [weak self] () -> Void in
            guard let self else { return }
            await self.runRefreshRound(reason: reason)
        }
        refreshTask = task
        _ = await task.value
        refreshTask = nil
    }

    private func runRefreshRound(reason: String) async {
        guard !connection.isFatal else { return }   // incompatible: stop querying
        isRefreshing = true
        defer { isRefreshing = false }
        connection = .connecting

        var outcome = await client.negotiateAsync()
        var launchFailure: String?
        if case .engineNotRunning = outcome, let launcher = engineLauncher {
            // The app owns the engine's lifecycle for its data dir
            // (design §9): launch the bundled runtime's engine, then
            // retry the negotiation once against it. Other outcomes
            // (incompatible) are never retried into a takeover.
            let ensure = await launcher.ensureRunning(
                socketPath: settings.resolvedSocketPath(),
                dataDir: settings.resolvedDataDirectory())
            switch ensure {
            case .started:
                outcome = await client.negotiateAsync()
            case let .failed(reason), let .unavailable(reason):
                launchFailure = reason
            case .alreadyRunning, .backingOff:
                break   // probe raced or a recent attempt died; reported below
            }
        }
        switch outcome {
        case let .compatible(engineVersion):
            connection = .ready(engineVersion: engineVersion)
        case let .incompatible(requested, supported):
            // Explicit downgrade report; no further queries this session
            // until the user retries explicitly.
            connection = .incompatible(requested: requested, supported: supported)
            lastFailureText = EngineFailure.incompatible(requested: requested,
                                                         supported: supported,
                                                         message: "").localizedDescription
            diagnostics.recordFailure(lastFailureText ?? "协议不兼容")
            return
        case .engineNotRunning:
            // A launch attempt that failed for a concrete reason (e.g.
            // the upgrade backup) is worth reporting verbatim; a plain
            // absent engine is not an error worth alarming about.
            connection = .notRunning(launchFailure
                ?? "引擎未运行（socket 无监听）。缓存数据继续展示。")
            lastFailureText = launchFailure
            // Still counted for diagnostics: a repeatedly absent engine
            // deserves the diagnostics entry (with 旧数据时间) too.
            diagnostics.recordFailure(launchFailure ?? "引擎未运行（socket 无监听）")
            return
        }

        do {
            // Ask the engine to collect, then read whatever is already in
            // the read model. The refresh job itself completes later; the
            // panel never waits for it (cache-first, §8).
            _ = try await client.requestRefreshAsync(reason: reason)
            let envelope = try await client.fetchOverviewAsync()
            let cached = CachedOverview(envelope: envelope, cachedAt: Date())
            cache.save(cached)
            cachedOverview = cached
            lastRefreshedAt = cached.cachedAt
            lastFailureText = nil
            diagnostics.recordSuccess(at: cached.cachedAt)
            await emitNotifications(for: envelope.attention)
        } catch let failure as EngineFailure {
            lastFailureText = failure.localizedDescription
            diagnostics.recordFailure(failure.localizedDescription)
            if case let .incompatible(requested, supported, _) = failure {
                connection = .incompatible(requested: requested, supported: supported)
            } else if case .unreachable = failure {
                connection = .notRunning("引擎连接中断。缓存数据继续展示。")
            }
            // A failed round keeps the last successful data displayed —
            // cachedOverview is untouched here.
        } catch {
            lastFailureText = "刷新失败：\(error)"
            diagnostics.recordFailure("刷新失败：\(error)")
        }
    }

    /// Feed one round's attention facts through the notification policy
    /// (design §4). The dedup state advances on EVERY round — including
    /// rounds while notifications are disabled — so enabling them later
    /// never bursts already-known anomalies. Posting happens only when
    /// the user opted in AND the OS granted authorization.
    private func emitNotifications(for attention: [AttentionItem]) async {
        let emit = settings.notificationsEnabled
            && notificationAuth == .granted
            && notifier != nil
        guard let payload = notificationPolicy.processRound(attention, emit: emit) else {
            return
        }
        await notifier?.post(payload)
    }

    // MARK: - Auto refresh (panel-open tier; G012 default 30s)

    /// While the panel is open the app holds a "panel-open" demand and
    /// re-reads the read model about every `panelRefreshInterval` seconds
    /// (default 30s, G012 用户反馈 4). This is a STATUS cadence — it does
    /// not mean token numbers change every round.
    ///
    /// Sleep-first: the ONE immediate round on open comes from
    /// `bootstrap()`; starting the loop never fires a second one (G012:
    /// 打开面板不重复立即刷新), and this is also the only loop that
    /// submits `refresh.request` — detail-window loads are pure reads, so
    /// panel + detail open together never duplicate collection demand.
    public func startAutoRefresh(interval: TimeInterval? = nil) {
        stopAutoRefresh()
        let effective = interval ?? settings.panelRefreshInterval
        autoRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(effective * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self?.refresh(reason: "panel-open")
            }
        }
    }

    /// Apply a changed refresh preference immediately when the panel's
    /// auto-refresh loop is already running.
    public func applyPanelRefreshPreference() {
        guard autoRefreshTask != nil else { return }
        startAutoRefresh()
    }

    public func stopAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
    }

    // MARK: - Sleep / wake (design §8: 睡眠期间不判定停摆，唤醒后重新同步)

    /// The system woke from sleep: re-sync immediately with a dedicated
    /// round. While asleep no round ran and no staleness was judged; the
    /// just-woke gap is a RESYNC state, not a failure — until the first
    /// successful round lands, the panel annotates the still-displayed
    /// data as "正在重新同步" instead of treating its age as an anomaly.
    public func handleWake() async {
        wokeAt = Date()
        await refresh(reason: "wake-resync")
    }

    /// True between wake and the first successful refresh after it.
    public var isResyncingAfterWake: Bool {
        guard let wokeAt else { return false }
        if let ok = lastRefreshedAt, ok >= wokeAt { return false }
        return true
    }

    // MARK: - Settings (design §8/§9: 设置页)

    private func updateSettings(_ change: (inout AppSettings) -> Void) {
        var next = settings
        change(&next)
        settingsStore?.save(next)
        settings = next
    }

    /// Refresh-preference setter (panel-open interval, seconds). An
    /// explicit choice from the settings page — marked as chosen so later
    /// upgrades keep it instead of migrating to a new default.
    public func setPanelRefreshInterval(_ interval: TimeInterval) {
        updateSettings {
            $0.panelRefreshInterval = interval
            $0.panelRefreshIntervalChosen = true
        }
        applyPanelRefreshPreference()
    }

    /// Explicit data-directory choice; nil restores the shared default.
    /// Applies on the next app launch — never moves or copies data.
    public func setDataDirectoryOverride(_ path: String?) {
        let cleaned = path.map { $0.trimmingCharacters(in: .whitespaces) }
        updateSettings { $0.dataDirectoryOverride = (cleaned?.isEmpty ?? true) ? nil : cleaned }
    }

    // MARK: - Notifications (design §4: 通知默认关闭，用户开启后再申请系统权限)

    /// The user opted IN: this is the ONLY place authorization is
    /// requested. A denial keeps notifications off (and the OS remembers
    /// the denial; re-enabling then requires System Settings).
    public func enableNotifications() async {
        guard let notifier else { return }
        let granted = await notifier.requestAuthorization()
        notificationAuth = granted ? .granted : .denied
        updateSettings { $0.notificationsEnabled = granted }
    }

    /// The user opted OUT: stop posting immediately. The OS permission
    /// itself is left as-is (revoking it is the user's system choice).
    public func disableNotifications() {
        updateSettings { $0.notificationsEnabled = false }
    }

    /// Re-read the OS verdict (for the settings page display).
    public func refreshNotificationAuthorization() async {
        guard let notifier else { return }
        notificationAuth = await notifier.systemAuthorizationStatus()
    }

    /// Whether the notification pipeline may currently post.
    public var notificationsActive: Bool {
        settings.notificationsEnabled && notificationAuth == .granted
    }

    // MARK: - Diagnostics (design §8: 引擎重复失败显示诊断入口)

    /// The diagnostics entry point is offered when engine rounds fail
    /// repeatedly OR the connection is fatally incompatible (both need
    /// user action, and both keep the last successful data displayed).
    public var showDiagnosticsEntry: Bool {
        diagnostics.needsDiagnosticsEntry || connection.isFatal
    }

    /// App termination: release THIS client's collection demand only.
    /// A CLI watcher's demand lives on its own connection (design §7).
    public func shutdown() {
        stopAutoRefresh()
        refreshTask?.cancel()
        if let socketClient = client as? EngineSocketClient {
            socketClient.disconnect()
        }
    }

    /// The separate, explicit cache-clean action (never automatic).
    public func clearDisplayCache() {
        cache.clear()
        cachedOverview = nil
    }
}
