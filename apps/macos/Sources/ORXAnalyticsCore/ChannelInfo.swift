import Foundation

// Distribution-channel identity of the running app (G011 T005).
//
// The channel, its audience and its Gatekeeper limits are written into
// the app's Info.plist by scripts/build_app.sh so they are visible IN
// the app itself (Finder Get Info, diagnostics view), not only in
// build logs. The two distribution channels never blur:
// - internal-preview: ad-hoc signed, NOT notarized — an explicitly
//   limited internal artifact, never presented as a formal installer;
// - official: Developer ID Application signed, hardened runtime,
//   notarized and stapled.
// This type only READS and presents those facts; it never infers or
// upgrades a channel.

/// The channel identity block read from an Info.plist.
public struct AppChannelInfo: Equatable {
    public static let channelKey = "ORXAnalyticsChannel"
    public static let audienceKey = "ORXAnalyticsChannelAudience"
    public static let gatekeeperKey = "ORXAnalyticsGatekeeperNotice"
    public static let commitKey = "ORXAnalyticsSourceCommit"

    /// "internal-preview", "official", or "development" (swift run).
    /// An unrecognized value is kept verbatim — unknown stays unknown.
    public let channel: String
    public let audience: String?
    public let gatekeeperNotice: String?
    /// The COMPLETE (40-hex) source commit of the analytics code in
    /// the bundle; nil when absent (development builds).
    public let sourceCommit: String?

    public init(channel: String, audience: String? = nil,
                gatekeeperNotice: String? = nil, sourceCommit: String? = nil) {
        self.channel = channel
        self.audience = audience
        self.gatekeeperNotice = gatekeeperNotice
        self.sourceCommit = sourceCommit
    }

    /// Read the channel block from an Info dictionary (e.g.
    /// `Bundle.main.infoDictionary`). Returns nil when the channel key
    /// is absent entirely — the caller must present that honestly as
    /// "unlabeled", never guess a channel.
    public static func from(infoDictionary: [String: Any]) -> AppChannelInfo? {
        guard let raw = infoDictionary[channelKey] as? String,
              !raw.isEmpty else { return nil }
        func optional(_ key: String) -> String? {
            guard let value = infoDictionary[key] as? String, !value.isEmpty else {
                return nil
            }
            return value
        }
        return AppChannelInfo(channel: raw,
                              audience: optional(audienceKey),
                              gatekeeperNotice: optional(gatekeeperKey),
                              sourceCommit: optional(commitKey))
    }

    public var isInternalPreview: Bool { channel == "internal-preview" }
    public var isOfficial: Bool { channel == "official" }

    /// Whether the recorded source commit is the complete 40-hex sha
    /// (a truncated or missing commit is displayed as incomplete, not
    /// silently accepted as full).
    public var hasCompleteSourceCommit: Bool {
        guard let commit = sourceCommit else { return false }
        return commit.count == 40 && commit.allSatisfy(\.isHexDigit)
    }

    /// One-line honest summary for the diagnostics view. Unknown
    /// channels are treated conservatively (like a limited preview),
    /// never promoted.
    public var summaryText: String {
        switch channel {
        case "internal-preview":
            return "内部预览渠道（仅内部测试）· ad-hoc 签名、未公证"
        case "official":
            return "正式分发渠道 · Developer ID 签名并已公证"
        case "development":
            return "开发运行（非分发产物）"
        default:
            return "未知渠道（\(channel)）——按受限预览对待，不视为正式分发"
        }
    }
}
