import Foundation

/// Display-only time formatting for engine timestamps (ISO-8601 with
/// offset). Formatting only — no business logic lives here, and an
/// unparseable value is shown raw rather than guessed.
enum DisplayTime {
    private static let parser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plainParser = ISO8601DateFormatter()

    /// Parse an engine timestamp, or nil.
    static func parse(_ iso: String?) -> Date? {
        guard let iso, !iso.isEmpty else { return nil }
        return parser.date(from: iso) ?? plainParser.date(from: iso)
    }

    /// "09:59:30" for today, "10-15 12:00" otherwise; nil if unparseable.
    static func short(_ iso: String?) -> String? {
        guard let date = parse(iso) else { return nil }
        let f = DateFormatter()
        if Calendar.current.isDateInToday(date) {
            f.dateFormat = "HH:mm:ss"
        } else {
            f.dateFormat = "MM-dd HH:mm"
        }
        return f.string(from: date)
    }

    /// "2026-10-16 09:59:30" (date + time); nil if unparseable.
    static func full(_ iso: String?) -> String? {
        guard let date = parse(iso) else { return nil }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.string(from: date)
    }

    /// Elapsed between two engine timestamps rendered as
    /// "3小时25分" / "45秒" — a duration FORMAT, not a derived metric.
    static func elapsed(from start: String?, to end: String?, now: Date = Date()) -> String? {
        guard let startDate = parse(start) else { return nil }
        let endDate = parse(end) ?? now
        var seconds = max(0, endDate.timeIntervalSince(startDate))
        let hours = Int(seconds) / 3600
        seconds -= TimeInterval(hours * 3600)
        let minutes = Int(seconds) / 60
        if hours > 0 {
            return "\(hours)小时\(minutes)分"
        }
        if minutes > 0 {
            return "\(minutes)分"
        }
        return "\(Int(seconds))秒"
    }
}
