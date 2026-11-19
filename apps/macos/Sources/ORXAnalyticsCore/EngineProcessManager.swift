import Foundation

// Engine process management (design §7/§9, task T012): the app is the
// engine's lifecycle owner for its data dir. When no engine answers on
// the socket, the app launches the BUNDLED standalone Python runtime
// with the bundled analytics code — a clean Mac needs nothing
// preinstalled. The engine process itself stays independent (its own
// single-instance lock arbitrates races); the app never kills it on
// quit, because a CLI watcher's demand must survive the app (§7).

// MARK: - Runtime location

/// Where to find a Python runtime able to serve the engine.
public struct EngineRuntimeLocation: Equatable {
    public let pythonExecutable: URL
    public let serveScript: URL
    /// Added to the child's PYTHONPATH (contains `orxanalytics`).
    public let moduleSearchPath: URL?
    /// "bundled" or "dev-override" — surfaced in diagnostics.
    public let origin: String

    public init(pythonExecutable: URL, serveScript: URL,
                moduleSearchPath: URL?, origin: String) {
        self.pythonExecutable = pythonExecutable
        self.serveScript = serveScript
        self.moduleSearchPath = moduleSearchPath
        self.origin = origin
    }
}

/// Resolution: bundled app resources first; the documented dev
/// override (`ORX_ANALYTICS_ENGINE_PYTHON`) second; nil when neither
/// exists (e.g. `swift run` without a bundle — use build_app.sh dev).
public enum EngineRuntimeResolver {
    public static let pythonOverrideEnv = "ORX_ANALYTICS_ENGINE_PYTHON"
    public static let serveOverrideEnv = "ORX_ANALYTICS_ENGINE_SERVE"
    public static let pythonPathOverrideEnv = "ORX_ANALYTICS_ENGINE_PYTHONPATH"

    public static func bundled(bundleURL: URL,
                               fileManager: FileManager = .default) -> EngineRuntimeLocation? {
        let resources = bundleURL.appendingPathComponent("Contents/Resources")
        let python = resources.appendingPathComponent("python/bin/python3")
        let serve = resources.appendingPathComponent("orx_engine_serve.py")
        let package = resources.appendingPathComponent("orxanalytics/__init__.py")
        guard fileManager.isExecutableFile(atPath: python.path),
              fileManager.fileExists(atPath: serve.path),
              fileManager.fileExists(atPath: package.path) else { return nil }
        return EngineRuntimeLocation(pythonExecutable: python,
                                     serveScript: serve,
                                     moduleSearchPath: resources,
                                     origin: "bundled")
    }

    /// Dev override for running the app from a checkout: point at a
    /// Python that has the repo's `orxanalytics` importable. The serve
    /// script comes from the env var, or a copy beside/above the
    /// interpreter (e.g. the repo root or the venv root).
    public static func devOverride(environment: [String: String],
                                   fileManager: FileManager = .default) -> EngineRuntimeLocation? {
        guard let pythonPath = environment[pythonOverrideEnv]?.trimmingCharacters(in: .whitespaces),
              !pythonPath.isEmpty else { return nil }
        let python = URL(fileURLWithPath: pythonPath)
        var serveURL: URL?
        if let s = environment[serveOverrideEnv]?.trimmingCharacters(in: .whitespaces), !s.isEmpty {
            serveURL = URL(fileURLWithPath: s)
        } else {
            let dir = python.deletingLastPathComponent()
            for candidate in [dir.appendingPathComponent("orx_engine_serve.py"),
                              dir.deletingLastPathComponent().appendingPathComponent("orx_engine_serve.py")] {
                if fileManager.fileExists(atPath: candidate.path) {
                    serveURL = candidate
                    break
                }
            }
        }
        guard let serve = serveURL, fileManager.fileExists(atPath: serve.path) else { return nil }
        let searchPath = environment[pythonPathOverrideEnv]?.trimmingCharacters(in: .whitespaces)
        return EngineRuntimeLocation(pythonExecutable: python,
                                     serveScript: serve,
                                     moduleSearchPath: (searchPath?.isEmpty ?? true)
                                         ? nil : URL(fileURLWithPath: searchPath!),
                                     origin: "dev-override")
    }
}

// MARK: - Socket probe

public enum EngineSocketProbe {
    /// Is something accepting connections on this Unix socket? A bare
    /// connect/close is enough; the engine treats a request-less
    /// connection as transient (its demand tracking releases on close).
    public static func isServing(path: String, timeout: TimeInterval = 1.0) -> Bool {
        let transport = UnixSocketLineTransport(path: path, timeout: timeout)
        do {
            try transport.connect()
            transport.close()
            return true
        } catch {
            return false
        }
    }
}

// MARK: - Upgrade guard (backup before a new engine version opens the DB)

/// Upgrade protection for the analytics-owned database (design §9:
/// 升级保护 analytics 自有数据库——先备份再迁移).
///
/// Before the FIRST launch of each engine version over an existing
/// `analytics.db`, the database is copied into `<data-dir>/backups/`
/// and only then may the engine (and any store migration it performs)
/// open it. Rolling back to an incompatible older version is refused
/// by the store itself (`store_version` check); the backups make a
/// manual restore possible without ever touching ORX project data.
public struct EngineUpgradeGuard {
    public static let backupPrefix = "analytics-pre-"
    public static let keepBackups = 3

    public let dataDir: URL
    public let fileManager: FileManager
    /// Injectable so tests never touch real time.
    public var now: () -> Date

    public init(dataDir: URL, fileManager: FileManager = .default, now: @escaping () -> Date = Date.init) {
        self.dataDir = dataDir
        self.fileManager = fileManager
        self.now = now
    }

    public static func databaseURL(dataDir: URL) -> URL {
        dataDir.appendingPathComponent("analytics.db")
    }

    public static func stampURL(dataDir: URL) -> URL {
        dataDir.appendingPathComponent("app/last-launched-engine-version")
    }

    public static func backupsDir(dataDir: URL) -> URL {
        dataDir.appendingPathComponent("backups")
    }

    /// Pure decision: a backup is needed only when a database exists
    /// AND the last engine version that opened it differs from the
    /// version about to run (same-version restarts do not pile up
    /// backups; a missing stamp is treated as "unknown, protect it").
    public static func needsBackup(databaseExists: Bool,
                                   stampContents: String?,
                                   engineVersion: String) -> Bool {
        guard databaseExists else { return false }
        let stamped = stampContents?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return stamped != engineVersion
    }

    /// Backup file name embeds the version and a sortable timestamp.
    public static func backupFileName(engineVersion: String, at date: Date) -> String {
        let safeVersion = engineVersion.replacingOccurrences(of: "/", with: "-")
        return backupPrefix + safeVersion + "-" + timestamp(date) + ".db"
    }

    static func timestamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone.current
        return formatter.string(from: date)
    }

    /// Read the recorded version of the last engine that opened this
    /// data dir (nil when never recorded / unreadable).
    public func lastLaunchedVersion() -> String? {
        try? String(contentsOf: Self.stampURL(dataDir: dataDir), encoding: .utf8)
    }

    /// Copy `analytics.db` into `backups/` when needed, prune old
    /// backups beyond `keepBackups`, and stamp the version. Returns
    /// the backup URL, or nil when no backup was needed. Throws on IO
    /// failure — the caller MUST then NOT launch a different-version
    /// engine over an unbacked database.
    @discardableResult
    public func backupIfNeeded(engineVersion: String) throws -> URL? {
        let db = Self.databaseURL(dataDir: dataDir)
        let exists = fileManager.fileExists(atPath: db.path)
        if !Self.needsBackup(databaseExists: exists,
                             stampContents: lastLaunchedVersion(),
                             engineVersion: engineVersion) {
            try writeStamp(engineVersion)
            return nil
        }
        guard exists else {
            try writeStamp(engineVersion)
            return nil
        }
        let dir = Self.backupsDir(dataDir: dataDir)
        try fileManager.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(
            Self.backupFileName(engineVersion: engineVersion, at: now()))
        try fileManager.copyItem(at: db, to: dest)
        pruneBackups(dir: dir)
        try writeStamp(engineVersion)
        return dest
    }

    /// Keep only the newest `keepBackups` backups (names sort by
    /// embedded timestamp). Never deletes anything outside backups/.
    func pruneBackups(dir: URL) {
        let names = ((try? fileManager.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasPrefix(Self.backupPrefix) && $0.hasSuffix(".db") }
            .sorted()
        guard names.count > Self.keepBackups else { return }
        for name in names.prefix(names.count - Self.keepBackups) {
            try? fileManager.removeItem(at: dir.appendingPathComponent(name))
        }
    }

    func writeStamp(_ version: String) throws {
        let stamp = Self.stampURL(dataDir: dataDir)
        try fileManager.createDirectory(at: stamp.deletingLastPathComponent(),
                                        withIntermediateDirectories: true)
        try version.write(to: stamp, atomically: true, encoding: .utf8)
    }
}

// MARK: - Child process abstraction (injectable for tests)

public protocol EngineChildProcess {
    /// Launched and not yet exited.
    var isAlive: Bool { get }
    func launch() throws
}

/// Real child: `Process` running the bundled Python.
public final class PosixEngineChildProcess: EngineChildProcess {
    private let process: Process
    public private(set) var didLaunch = false

    public init(executableURL: URL, arguments: [String], environment: [String: String],
                standardError: FileHandle?) {
        process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        if let standardError {
            process.standardError = standardError
            process.standardOutput = standardError
        }
    }

    public var isAlive: Bool {
        didLaunch && process.isRunning
    }

    public func launch() throws {
        try process.run()
        didLaunch = true
    }
}

// MARK: - Manager

public enum EngineEnsureOutcome: Equatable {
    /// An engine already serves the socket — nothing to do.
    case alreadyRunning
    /// We launched a child and its socket came up.
    case started
    /// The engine died right after a recent spawn attempt; waiting
    /// before trying again (bounded respawn, design §8 引擎崩溃有限重启).
    case backingOff(seconds: Double)
    /// Spawned (or tried to) but the socket never came up.
    case failed(String)
    /// No usable Python runtime (no bundle, no dev override).
    case unavailable(String)
}

public protocol EngineProcessManaging: AnyObject {
    func ensureRunning(socketPath: String, dataDir: String) async -> EngineEnsureOutcome
}

/// Owns the engine child for the app's data dir.
///
/// Behavior contract:
/// - Coalesced: concurrent `ensureRunning` calls share one attempt.
/// - Refuses to spawn a different engine version over an existing
///   `analytics.db` until the upgrade backup succeeded (§9 先备份再迁移).
/// - Bounded respawn: at most one spawn attempt per `respawnBackoff`.
/// - The child is detached from the app's lifecycle by design: quitting
///   the app must NOT stop an engine a CLI watcher still needs (§7).
///   The engine exits on its own when its lock is taken over, or at
///   logout/shutdown; the next app launch re-ensures it.
public final class EngineProcessManager: EngineProcessManaging {
    public static let respawnBackoff: TimeInterval = 30
    public static let socketWaitTimeout: TimeInterval = 12
    public static let engineLogByteCap = 1_000_000

    public let location: EngineRuntimeLocation?
    public let engineVersion: String
    private let fileManager: FileManager
    private let now: () -> Date
    private let probe: (String) -> Bool
    private let childFactory: (EngineRuntimeLocation, String) throws -> EngineChildProcess
    private let sleep: (TimeInterval) async -> Void

    private final class Attempt {
        let task: Task<EngineEnsureOutcome, Never>
        init(task: Task<EngineEnsureOutcome, Never>) { self.task = task }
    }

    private let lock = NSLock()
    private var lastSpawnAttemptAt: Date?
    private var inFlight: Attempt?

    public convenience init(bundleURL: URL,
                            environment: [String: String] = ProcessInfo.processInfo.environment,
                            infoPlistVersion: String? = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String,
                            fileManager: FileManager = .default) {
        let location = EngineRuntimeResolver.bundled(bundleURL: bundleURL,
                                                     fileManager: fileManager)
            ?? EngineRuntimeResolver.devOverride(environment: environment,
                                                 fileManager: fileManager)
        let version = Self.resolveEngineVersion(bundleURL: bundleURL,
                                                location: location,
                                                infoPlistVersion: infoPlistVersion)
        self.init(location: location, engineVersion: version,
                  fileManager: fileManager, now: Date.init,
                  probe: { EngineSocketProbe.isServing(path: $0) },
                  childFactory: { location, dataDir in
                      try Self.makeChild(location: location, dataDir: dataDir,
                                         fileManager: fileManager)
                  },
                  sleep: { try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) })
    }

    public init(location: EngineRuntimeLocation?,
                engineVersion: String,
                fileManager: FileManager = .default,
                now: @escaping () -> Date = Date.init,
                probe: @escaping (String) -> Bool,
                childFactory: @escaping (EngineRuntimeLocation, String) throws -> EngineChildProcess,
                sleep: @escaping (TimeInterval) async -> Void) {
        self.location = location
        self.engineVersion = engineVersion
        self.fileManager = fileManager
        self.now = now
        self.probe = probe
        self.childFactory = childFactory
        self.sleep = sleep
    }

    static func resolveEngineVersion(bundleURL: URL,
                                     location: EngineRuntimeLocation?,
                                     infoPlistVersion: String?) -> String {
        let versionFile = bundleURL
            .appendingPathComponent("Contents/Resources/orxanalytics-version.txt")
        if let contents = try? String(contentsOf: versionFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), !contents.isEmpty {
            return contents
        }
        if let v = infoPlistVersion?.trimmingCharacters(in: .whitespaces), !v.isEmpty {
            return v
        }
        return "unknown"
    }

    /// Build the real child process for a data dir, with the engine's
    /// launch log (size-capped, §8 本地日志有容量限制).
    ///
    /// `environment` is injectable so the real-child seal regression can
    /// supply a hostile parent environment (e.g. PYTHONDONTWRITEBYTECODE=0);
    /// production always uses the app's own `ProcessInfo` environment.
    static func makeChild(location: EngineRuntimeLocation, dataDir: String,
                          fileManager: FileManager,
                          environment parentEnvironment: [String: String] =
                              ProcessInfo.processInfo.environment) throws -> EngineChildProcess {
        var environment = parentEnvironment
        // Runtime seal fix (disclosed v0.4.2 defect, G012 T006): force
        // the engine child to never write bytecode, overriding whatever
        // the parent environment carries. An operator-launched app has
        // no such flag (and CPython also writes bytecode for the values
        // "0" and ""), so the first engine start used to drop 73
        // __pycache__/*.pyc entries INTO the signed .app and flip
        // `codesign --verify --strict` from 0 to 1 — only harness-
        // launched engines (whose env already carried the flag) stayed
        // sealed. The flag must be forced here in the app itself: the
        // bundled runtime's stdlib and analytics code live inside the
        // sealed bundle, and any pyc written there is a seal break.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if let searchPath = location.moduleSearchPath {
            let existing = environment["PYTHONPATH"]
            environment["PYTHONPATH"] = (existing?.isEmpty ?? true)
                ? searchPath.path : searchPath.path + ":" + existing!
        }
        // The serve wrapper receives the data dir explicitly; keep the
        // env consistent for anything the engine subprocess resolves.
        environment["ORX_ANALYTICS_HOME"] = dataDir

        let dir = URL(fileURLWithPath: dataDir)
        try fileManager.createDirectory(at: dir.appendingPathComponent("logs"),
                                        withIntermediateDirectories: true)
        let logURL = dir.appendingPathComponent("logs/engine-launch.log")
        if let size = (try? fileManager.attributesOfItem(atPath: logURL.path)[.size]) as? Int,
           size > Self.engineLogByteCap {
            try? fileManager.removeItem(at: logURL)
        }
        // FileHandle(forWritingAtPath:) never CREATES the file: on a
        // fresh install (or right after the cap rotation removed it)
        // the documented launch log did not exist, the open returned
        // nil, and the engine's stderr silently went to nullDevice —
        // the log never appeared. Create it empty so the handle (and
        // thus the real-launch evidence the post-install seal check
        // requires, G012 T006) always attaches.
        if !fileManager.fileExists(atPath: logURL.path) {
            _ = fileManager.createFile(atPath: logURL.path, contents: nil)
        }
        let opened = FileHandle(forWritingAtPath: logURL.path)
        if let opened {
            _ = try? opened.seekToEnd()
        }
        let handle = opened ?? FileHandle.nullDevice
        return PosixEngineChildProcess(executableURL: location.pythonExecutable,
                                       arguments: [location.serveScript.path, dataDir],
                                       environment: environment,
                                       standardError: handle)
    }

    private func underLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    public func ensureRunning(socketPath: String, dataDir: String) async -> EngineEnsureOutcome {
        // Coalesced: concurrent callers join the in-flight attempt.
        let attempt = underLock {
            if let existing = inFlight { return existing }
            let t = Task<EngineEnsureOutcome, Never> { [weak self] in
                await self?._ensure(socketPath: socketPath, dataDir: dataDir)
                    ?? .failed("manager released")
            }
            let attempt = Attempt(task: t)
            inFlight = attempt
            return attempt
        }
        let outcome = await attempt.task.value
        underLock {
            if inFlight === attempt { inFlight = nil }
        }
        return outcome
    }

    func _ensure(socketPath: String, dataDir: String) async -> EngineEnsureOutcome {
        // Probe first: a serving engine needs no runtime at all (an
        // engine started by the CLI counts as "running" for the app).
        if probe(socketPath) {
            return .alreadyRunning
        }
        guard let location else {
            return .unavailable("未找到内置 Python 运行时（开发运行请用 scripts/build_app.sh dev，"
                                + "或设置 ORX_ANALYTICS_ENGINE_PYTHON）")
        }
        if let last = lastSpawnAttemptAt,
           now().timeIntervalSince(last) < Self.respawnBackoff {
            return .backingOff(seconds: Self.respawnBackoff - now().timeIntervalSince(last))
        }
        lastSpawnAttemptAt = now()

        // Upgrade protection FIRST: never open an existing analytics.db
        // with a different engine version before the backup succeeded.
        let guard_ = EngineUpgradeGuard(dataDir: URL(fileURLWithPath: dataDir),
                                        fileManager: fileManager, now: now)
        do {
            try guard_.backupIfNeeded(engineVersion: engineVersion)
        } catch {
            return .failed("升级备份失败，已拒绝启动新引擎（先备份再迁移）：\(error)")
        }

        let child: EngineChildProcess
        do {
            child = try childFactory(location, dataDir)
            try child.launch()
        } catch {
            return .failed("引擎进程启动失败：\(error)")
        }

        let deadline = now().addingTimeInterval(Self.socketWaitTimeout)
        while now() < deadline {
            if probe(socketPath) {
                return .started
            }
            if !child.isAlive {
                let log = URL(fileURLWithPath: dataDir)
                    .appendingPathComponent("logs/engine-launch.log")
                let tail = ((try? String(contentsOf: log, encoding: .utf8)) ?? "")
                    .split(separator: "\n").suffix(3).joined(separator: " | ")
                return .failed("引擎进程提前退出\(tail.isEmpty ? "" : "：\(tail)")")
            }
            await sleep(0.3)
        }
        return .failed("引擎在 \(Int(Self.socketWaitTimeout))s 内未开始监听 socket")
    }
}
