import Foundation

/// Presentation rules for token numbers — these are FROZEN CONTRACTS,
/// not styling choices (design §5 + docs/metrics.md):
///
/// 1. 完全未知显示 "—"。Only a source that explicitly recorded zero may
///    show "0". Unknown must never be zero-filled.
/// 2. "ORX 已记录用量" and "原生会话预览" are two INDEPENDENT calibers.
///    They may describe the same tokens, so they are displayed side by
///    side, never added, and no mixed total exists anywhere in this layer.
/// 3. There is no "今日消耗" derivation here — adding one requires a new
///    contract entry plus regression tests first.
/// 4. G012: every token and count renders with FIXED 3-digit comma
///    grouping ("1,234,567"), computed arithmetically so no system
///    locale can alter the separator or the group size. Identifiers,
///    versions and times never pass through this formatter.
public enum MetricsFormat {

    /// nil -> "—" (unknown); 0 -> "0" (explicitly recorded zero).
    public static func tokenText(_ value: Int?) -> String {
        guard let value else { return "—" }
        return value == 0 ? "0" : format(value)
    }

    /// Same rule for any counted quantity (attempts, fields, ...).
    public static func countText(_ value: Int?) -> String {
        tokenText(value)
    }

    /// FIXED 3-digit comma grouping, locale-independent by construction:
    /// the groups are computed arithmetically (no ICU formatter, no
    /// locale lookup), so de_DE cannot turn "1,200" into "1.200" and
    /// hi_IN cannot regroup into "12,34,567".
    private static func format(_ value: Int) -> String {
        let sign = value < 0 ? "-" : ""
        var digits = String(value.magnitude)
        guard digits.count > 3 else { return sign + digits }
        var groups: [Substring] = [digits.suffix(3)]
        digits.removeLast(3)
        while digits.count > 3 {
            groups.insert(digits.suffix(3), at: 0)
            digits.removeLast(3)
        }
        groups.insert(digits[...], at: 0)
        return sign + groups.joined(separator: ",")
    }

    /// "已完成任务数 / 当前计划任务数" — a fraction of the CURRENT plan
    /// only; never presented as a work-percentage (design §3). Both the
    /// numerator and the denominator use the unified grouping (G012).
    public static func progressText(_ progress: ProgressBlock?) -> String {
        guard let progress else { return "—" }
        return "\(format(progress.completed))/\(format(progress.total))"
    }

    /// Summary line for one run's observed consumption with its coverage
    /// honesty ("已观测 N；覆盖 a/b 次尝试" style), caliber-tagged.
    public static func consumptionText(_ consumption: ConsumptionBlock?) -> String {
        guard let consumption else { return "已观测 token —" }
        let caliber = consumption.caliber == "orx_recorded" ? "ORX 已记录" : (consumption.caliber ?? "未知口径")
        return "已观测 token \(tokenText(consumption.observedTotal))（\(caliber)）"
    }

    /// Coverage honesty line from an engine coverage block:
    /// "已观测 a/b 次尝试". Unknown coverage renders dashes. Numerator
    /// and denominator use the unified grouping (G012).
    public static func coverageText(_ coverage: CoverageBlock?) -> String {
        guard let coverage else { return "已观测 —/— 次尝试" }
        return "已观测 \(format(coverage.observed))/\(format(coverage.attempts)) 次尝试"
    }

    /// Missing-field explanation from the engine's per-field missing
    /// counts (input_tokens/output_tokens/cached_input). A count of 0
    /// IS shown as absence of misses only when the dict exists; a nil
    /// dict means the engine reported nothing — render "—". Row counts
    /// render through the unified formatter (G012).
    public static func missingText(_ missing: [String: Int]?) -> String {
        guard let missing else { return "字段缺失 —" }
        let parts = missing.filter { $0.value > 0 }
            .sorted { $0.key < $1.key }
            .map { "\($0.key) 缺 \(countText($0.value)) 行" }
        return parts.isEmpty ? "无缺失记录" : parts.joined(separator: "，")
    }

    /// Partial-sum suffix for the normalized processing metric, appended
    /// only when the engine flagged the sum partial: "（部分和，缺 N 行）".
    /// A partial sum whose rowsMissing is nil is UNKNOWN — it renders
    /// "缺 — 行", never a zero-filled "缺 0 行" (contract §1); an
    /// explicitly recorded 0 stays "0". Row counts use the unified
    /// grouping (G012).
    public static func partialSuffix(_ normalized: NormalizedProcessing) -> String {
        guard normalized.partial == true else { return "" }
        return "（部分和，缺 \(countText(normalized.rowsMissing)) 行）"
    }
}

/// The two calibers the UI may show for the same run. Constructed with
/// explicit fields and rendered with explicit labels — there is no API
/// here that combines them, by design (metrics freeze).
public struct UsageCalibers: Equatable {
    /// ORX 已记录用量 — the primary statistic, always shown with its
    /// coverage.
    public let recordedTotal: Int?
    /// 原生会话预览 — shown SEPARATELY with source and update time when
    /// a source supports it; nil means not provided.
    public let nativePreviewTotal: Int?

    public init(recordedTotal: Int?, nativePreviewTotal: Int? = nil) {
        self.recordedTotal = recordedTotal
        self.nativePreviewTotal = nativePreviewTotal
    }

    /// Two independent rows — never one merged number.
    public var rows: [(label: String, text: String)] {
        [("ORX 已记录", MetricsFormat.tokenText(recordedTotal)),
         ("原生会话预览", MetricsFormat.tokenText(nativePreviewTotal))]
    }
}
