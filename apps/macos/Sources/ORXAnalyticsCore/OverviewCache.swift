import Foundation

/// The last successfully displayed overview, as persisted by the app.
public struct CachedOverview: Equatable {
    public let envelope: OverviewEnvelope
    public let cachedAt: Date

    public init(envelope: OverviewEnvelope, cachedAt: Date) {
        self.envelope = envelope
        self.cachedAt = cachedAt
    }

    /// Seconds since this snapshot was cached (drives "数据更新时间"
    /// staleness text, distinct from the run's own data_as_of).
    public func ageSeconds(now: Date = Date()) -> TimeInterval {
        max(0, now.timeIntervalSince(cachedAt))
    }
}

/// Cache-first persistence for the quick panel (design §3/§8).
///
/// - Written after every SUCCESSFUL overview fetch — never after a
///   failure, so a failed round keeps the last good data plus the
///   failure reason, exactly like the engine's per-project policy.
/// - Read synchronously at open: the panel renders the cache first and
///   only then kicks off the async refresh. The UI never waits for
///   collection to finish before showing something.
/// - The cache is the APP's display cache — clearing it is a separate,
///   explicit operation (`clear`) and never touches ORX project data or
///   the analytics data-dir (design §9: 退出或卸载不删除项目数据).
public final class OverviewCacheStore {
    private let fileURL: URL
    private let ioQueue = DispatchQueue(label: "org.orxanalytics.cache-io",
                                        qos: .utility)

    public init(directory: URL) {
        self.fileURL = directory.appendingPathComponent("overview-cache.json")
    }

    /// Default location: the app's Application Support directory.
    public static func defaultDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("ORXAnalytics", isDirectory: true)
    }

    public static func makeDefault() -> OverviewCacheStore {
        OverviewCacheStore(directory: defaultDirectory())
    }

    private struct Wrapper: Codable {
        let cachedAt: Date
        let overview: OverviewEnvelope
    }

    /// Load the cached overview. A missing, unreadable or corrupt file
    /// returns nil — an unknown cache is isolated, never a crash.
    public func load() -> CachedOverview? {
        ioQueue.sync { _load() }
    }

    private func _load() -> CachedOverview? {
        guard let data = try? Data(contentsOf: fileURL) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let wrapper = try? decoder.decode(Wrapper.self, from: data) else {
            return nil
        }
        return CachedOverview(envelope: wrapper.overview, cachedAt: wrapper.cachedAt)
    }

    /// Persist atomically (tmp file + rename), user-private (0600).
    public func save(_ cached: CachedOverview) {
        ioQueue.sync { _save(cached) }
    }

    private func _save(_ cached: CachedOverview) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(Wrapper(cachedAt: cached.cachedAt,
                                                     overview: cached.envelope)) else {
            return
        }
        let dir = fileURL.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let tmp = fileURL.appendingPathExtension("tmp-\(UUID().uuidString)")
            try data.write(to: tmp, options: [.atomic])
            try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                                   ofItemAtPath: tmp.path)
            // POSIX rename(2) atomically replaces an existing cache on
            // the same volume — no window with a missing or torn file.
            if rename(tmp.path, fileURL.path) != 0 {
                throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                              userInfo: [NSLocalizedDescriptionKey:
                                         "rename failed: \(String(cString: strerror(errno)))"])
            }
        } catch {
            // Cache write failures are non-fatal: the panel still shows
            // the in-memory data for this session.
            try? FileManager.default.removeItem(at: fileURL.appendingPathExtension("tmp"))
        }
    }

    /// The SEPARATE, explicit cache-clean operation (never automatic,
    /// never part of quit/uninstall of ORX project data).
    public func clear() {
        ioQueue.sync {
            try? FileManager.default.removeItem(at: fileURL)
        }
    }
}
