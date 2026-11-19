import Foundation

// MARK: - history query presentation (design §3 history part)

/// Sort keys the UI offers, mapped onto the engine's frozen SORT_KEYS.
/// The engine sorts (and appends the composite identity as secondary
/// key); this layer only names the options — it never re-sorts.
public enum HistorySortKey: String, CaseIterable, Equatable, Identifiable {
    case startedAt = "started_at"
    case completedAt = "completed_at"
    case observedTokens = "observed_tokens"

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .startedAt: return "开始时间"
        case .completedAt: return "完成时间"
        case .observedTokens: return "已观测消耗"
        }
    }
}

/// Time-range presets (design §3: 时间范围过滤). Only `since` bounds are
/// offered in v1; the ISO string uses the same `+00:00` offset style
/// the engine's lexical comparisons assume.
public enum HistoryTimeRange: Equatable, CaseIterable, Identifiable {
    case all
    case last24h
    case last7d
    case last30d

    public var id: String { label }

    public var label: String {
        switch self {
        case .all: return "全部时间"
        case .last24h: return "最近 24 小时"
        case .last7d: return "最近 7 天"
        case .last30d: return "最近 30 天"
        }
    }

    private var hoursBack: Int? {
        switch self {
        case .all: return nil
        case .last24h: return 24
        case .last7d: return 7 * 24
        case .last30d: return 30 * 24
        }
    }

    /// The engine compares ISO strings lexically, so the emitted bound
    /// must share the run timestamps' `YYYY-MM-DDTHH:MM:SS+00:00` shape
    /// (an ICU "X" timezone pattern would emit "Z" for UTC and misorder
    /// against "+00:00" at equal seconds — the offset is a literal
    /// because the formatter is pinned to UTC).
    public func sinceISO(now: Date = Date()) -> String? {
        guard let hoursBack else { return nil }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'+00:00'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter.string(from: now.addingTimeInterval(
            TimeInterval(-hoursBack * 3600)))
    }
}

/// RAW run-status options for the status filter — exactly the display
/// contract's known run statuses (display.py RUN_DISPLAY). The filter
/// always sends the raw value; unknown source statuses are never turned
/// into filter options.
public struct HistoryStatusOption: Equatable, Identifiable {
    public let raw: String
    public let label: String

    public var id: String { raw }

    public init(raw: String, label: String) {
        self.raw = raw
        self.label = label
    }

    public static let known: [HistoryStatusOption] = [
        HistoryStatusOption(raw: "planning", label: "规划中"),
        HistoryStatusOption(raw: "running", label: "执行中"),
        HistoryStatusOption(raw: "blocked", label: "受阻"),
        HistoryStatusOption(raw: "done", label: "完成"),
    ]
}

// MARK: - one history result row

/// Pure presentation row for one run of a `runs.list` page.
///
/// Frozen rules (design §3 + §5 + metrics freeze):
/// - Consumption and its COVERAGE always travel together — a row built
///   under the consumption sort carries the coverage text by
///   construction (`coverageText` is non-empty for every row).
/// - Unknown consumption renders "—" plus an explicit note that the
///   engine sorts unknowns LAST — a run without usage data can never
///   look like the cheapest one. Only an engine-recorded zero renders
///   "0".
/// - The consumption shown is the run's ALL-REVISION total; current
///   plan progress is a separate expression and never mixed in.
/// - Lifecycle layering (G011): `goalControlText` exposes the Goal
///   CONTROL layer (已暂停/取消中/已取消/已完成) beside the execution
///   status — two dimensions on every row. Nil when the control layer
///   is absent or unknown (never guessed); a cancelled Goal's history
///   rows stay queryable with their control layer visible.
public struct HistoryRunRow: Equatable, Identifiable {
    public let run: RunFacts
    public let sortKey: String
    /// "已观测 token 1,200（全部版本）" / "已观测 token —（全部版本）"
    public let consumptionText: String
    /// Coverage honesty line, ALWAYS present next to the number:
    /// "已观测 2/3 次尝试".
    public let coverageText: String
    /// True when the engine recorded usage (a real total, even 0).
    public let consumptionKnown: Bool
    /// Honesty note for the consumption sort: unknown usage is sorted
    /// LAST by the engine and is not a small number.
    public let unknownNote: String?
    /// Goal control-layer label (已暂停/取消中/已取消/已完成) or nil when
    /// the control layer is absent/unknown — the raw statuses are never
    /// rewritten and an unknown value is never guessed terminal.
    public let goalControlText: String?

    public var id: String { run.id }

    public static func build(_ run: RunFacts, sortKey: String) -> HistoryRunRow {
        let known = run.consumption?.observedTotal != nil
        let number = MetricsFormat.tokenText(run.consumption?.observedTotal)
        let unknownNote: String?
        if known {
            unknownNote = nil
        } else if sortKey == HistorySortKey.observedTokens.rawValue {
            unknownNote = "用量未知：排序时排在最后，不表示消耗最少"
        } else {
            unknownNote = "用量未知"
        }
        let control = run.goal?.status
        let controlText: String?
        if let control, control.known {
            controlText = control.display
        } else {
            controlText = nil
        }
        return HistoryRunRow(
            run: run,
            sortKey: sortKey,
            consumptionText: "已观测 token \(number)（全部版本）",
            coverageText: MetricsFormat.coverageText(run.consumption?.coverage),
            consumptionKnown: known,
            unknownNote: unknownNote,
            goalControlText: controlText)
    }
}
