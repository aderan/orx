import Foundation

/// Engine-round failure tracking for the diagnostics view (design §8:
/// 失败时保留最后成功数据，附失败原因及旧数据时间；引擎重复失败显示
/// 诊断入口).
///
/// A value type owned by the app model:
/// - every failed refresh round increments `consecutiveFailures` and
///   records the reason;
/// - every successful round resets the counter and stamps
///   `lastSuccessAt` — the 旧数据时间 of the still-displayed data while
///   later rounds fail;
/// - an entry point into the diagnostics view is offered once failures
///   repeat `failureThreshold` times (occasional failures don't nag).
public struct DiagnosticsState: Equatable {
    public static let failureThreshold = 3

    public private(set) var consecutiveFailures: Int = 0
    public private(set) var lastFailureText: String?
    public private(set) var lastFailureAt: Date?
    public private(set) var lastSuccessAt: Date?

    public init() {}

    /// Repeated failures show the diagnostics entry.
    public var needsDiagnosticsEntry: Bool {
        consecutiveFailures >= Self.failureThreshold
    }

    public mutating func recordSuccess(at date: Date = Date()) {
        consecutiveFailures = 0
        lastSuccessAt = date
        // lastFailure* deliberately kept: the recent failure stays
        // explainable after recovery (恢复后可解释).
    }

    public mutating func recordFailure(_ text: String, at date: Date = Date()) {
        consecutiveFailures += 1
        lastFailureText = text
        lastFailureAt = date
    }
}

/// One per-source failure row of the diagnostics view. `takenAt` is the
/// source's last successful snapshot — its 旧数据时间.
public struct SourceStatusRow: Equatable, Identifiable {
    public let project: String
    /// The engine's data-health classification (来源不可读/版本不支持/
    /// 更新延迟…), nil when only a warning exists.
    public let health: String?
    public let takenAt: String?
    /// The engine's warning (code + detail) as the failure reason.
    public let warning: String?

    public var id: String { project }

    public init(project: String, health: String?, takenAt: String?,
                warning: String?) {
        self.project = project
        self.health = health
        self.takenAt = takenAt
        self.warning = warning
    }
}

/// Pure builder of the diagnostics view's per-source section from the
/// last successful overview (design §8: 一个项目失败不影响其他项目).
///
/// The overview response itself is the proof that 单源失败不拖垮整体:
/// the engine answers for the healthy projects while flagging the
/// failed ones. This builder just surfaces those flags — unhealthy
/// sources are listed with reason + 旧数据时间, healthy ones summarized
/// by count. An UNCLASSIFIED source (health == nil, no warning) is
/// unknown, not a failure — never asserted as one.
public enum DiagnosticsBuilder {

    public static func sourceRows(from envelope: OverviewEnvelope)
        -> (unhealthy: [SourceStatusRow], healthyCount: Int) {
        var warningsByProject: [String: [String]] = [:]
        for warning in envelope.warnings {
            guard let project = warning.project else { continue }
            var text = warning.code
            if let detail = warning.detail, !detail.isEmpty {
                text += "：\(detail)"
            }
            warningsByProject[project, default: []].append(text)
        }

        var unhealthy: [SourceStatusRow] = []
        var healthy = 0
        for row in envelope.projectHealth {
            let health = row.dataHealth?.health
            if health == "正常" {
                healthy += 1
                continue
            }
            guard let health else { continue }   // unknown ≠ failure
            unhealthy.append(SourceStatusRow(
                project: row.project,
                health: health,
                takenAt: row.takenAt,
                warning: warningsByProject[row.project]?.joined(separator: "；")))
        }

        // Warnings for projects without a health row (e.g. a registered
        // project whose directory disappeared) still get their row.
        let known = Set(envelope.projectHealth.map(\.project))
        for (project, texts) in warningsByProject where !known.contains(project) {
            unhealthy.append(SourceStatusRow(project: project, health: nil,
                                             takenAt: nil,
                                             warning: texts.joined(separator: "；")))
        }

        unhealthy.sort { $0.project < $1.project }
        return (unhealthy, healthy)
    }
}
