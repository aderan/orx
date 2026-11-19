import Foundation

/// System-notification authorization as the app tracks it (the actual
/// verdict always belongs to the OS).
public enum NotificationAuthState: Equatable {
    case notDetermined
    case granted
    case denied
}

/// The notification surface. The REAL UserNotifications adapter lives in
/// the app target only — Core (and therefore every test) can never fire
/// a real system notification (CONTRIBUTING: 不弹真实系统通知).
public protocol NotificationPosting: AnyObject {
    /// The system's current verdict without prompting.
    func systemAuthorizationStatus() async -> NotificationAuthState
    /// Prompt ONCE for authorization (called only from the user's opt-in
    /// action — never automatically at launch).
    func requestAuthorization() async -> Bool
    /// Deliver one already-decided notification.
    func post(_ notification: AttentionNotification) async
}

/// One merged notification: a whole refresh round's NEW fact-based
/// anomalies in a SINGLE user notification (design §4: 同一轮多个异常
/// 合并通知), carrying the deep-link target for 通知点击进入对应执行.
public struct AttentionNotification: Equatable {
    public let title: String
    public let body: String
    /// Deep-link target: the execution of the first new anomaly.
    public let project: String
    public let runID: String
    /// How many new anomalies this one notification covers.
    public let itemCount: Int

    public init(title: String, body: String, project: String,
                runID: String, itemCount: Int) {
        self.title = title
        self.body = body
        self.project = project
        self.runID = runID
        self.itemCount = itemCount
    }
}

/// How trustworthy an attention kind is for alerting.
public enum AttentionConfidence: Equatable {
    /// Backed by recorded facts (a failed attempt, a blocked run).
    case factBased
    /// Speculative/unknown — panel display only (低置信度推测默认只在
    /// 面板中显示).
    case lowConfidence
}

/// Pure anomaly-to-notification decision logic (design §4 通知).
///
/// Rules, all unit-testable without any UI or real notification center:
/// - The FIRST round is a baseline: anomalies already present when the
///   app starts are panel content, not a notification burst.
/// - 相同异常去重: a persisting anomaly never re-alarms. Identity is the
///   attention item's stable id (project#run#task#kind).
/// - 恢复后不反复报警: an anomaly that disappears (a successor attempt
///   took over) leaves the seen-set and stays silent. If the same
///   anomaly occurs again later that is a fresh occurrence and notifies
///   exactly once.
/// - 同一轮合并: all of one round's new anomalies become ONE
///   notification.
/// - Low-confidence kinds (anything not in `factBasedKinds`) are never
///   notified — unknown future kinds may be speculative, and unknown
///   values are never guessed into alerts.
/// - Rounds processed while disabled still update the seen-set, so
///   enabling notifications later does not burst known anomalies.
public struct NotificationPolicy {
    /// Attention kinds the engine derives from recorded facts. Anything
    /// else — present or future — is treated as low-confidence until
    /// proven fact-based.
    public static let factBasedKinds: Set<String> = ["failed_task", "blocked_run"]

    private var seen: Set<String> = []
    private var started = false

    public init() {}

    public static func confidence(forKind kind: String) -> AttentionConfidence {
        factBasedKinds.contains(kind) ? .factBased : .lowConfidence
    }

    /// Display label for a kind; unknown kinds keep their raw string.
    public static func label(forKind kind: String) -> String {
        switch kind {
        case "failed_task": return "任务失败"
        case "blocked_run": return "执行阻塞"
        default: return kind
        }
    }

    /// Process one refresh round. Always updates the dedup state; emits
    /// only when `emit`. Returns the merged notification for the round's
    /// NEW fact-based anomalies, or nil.
    public mutating func processRound(_ items: [AttentionItem],
                                      emit: Bool) -> AttentionNotification? {
        let factBased = items.filter {
            Self.confidence(forKind: $0.kind) == .factBased
        }
        let currentIds = Set(factBased.map(\.id))
        let fresh = started ? factBased.filter { !seen.contains($0.id) } : []
        seen = currentIds
        started = true
        guard emit, !fresh.isEmpty else { return nil }
        return Self.merge(fresh)
    }

    /// Build the single merged notification for a round's fresh items.
    static func merge(_ fresh: [AttentionItem]) -> AttentionNotification {
        let cap = 5
        let lines = fresh.prefix(cap).map { item -> String in
            let task = item.taskID.map { " \($0)" } ?? ""
            let project = (item.project as NSString).lastPathComponent
            return "\(label(forKind: item.kind)) \(project)/\(item.runID)\(task)：\(item.detail)"
        }
        var body = lines.joined(separator: "\n")
        if fresh.count > cap {
            body += "\n…等 \(MetricsFormat.countText(fresh.count)) 项"
        }
        let title = fresh.count == 1
            ? "ORX Analytics：需要关注"
            : "ORX Analytics：\(MetricsFormat.countText(fresh.count)) 项需要关注"
        return AttentionNotification(title: title, body: body,
                                     project: fresh[0].project,
                                     runID: fresh[0].runID,
                                     itemCount: fresh.count)
    }
}
