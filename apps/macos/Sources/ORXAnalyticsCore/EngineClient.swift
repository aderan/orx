import Foundation

/// What a successful `refresh.request` returns (`refresh.accepted`).
public struct RefreshAccepted: Codable, Equatable {
    public let jobID: String?
    public let mergedWith: String?
    /// Projects the engine refused (not registered / unreadable / ...) —
    /// surfaced as warnings, never silently dropped.
    public let rejected: [RejectedProject]

    public struct RejectedProject: Codable, Equatable {
        public let project: String
        public let reason: String
    }

    enum CodingKeys: String, CodingKey {
        case rejected
        case jobID = "job_id"
        case mergedWith = "merged_with"
    }

    public init(jobID: String? = nil, mergedWith: String? = nil,
                rejected: [RejectedProject] = []) {
        self.jobID = jobID
        self.mergedWith = mergedWith
        self.rejected = rejected
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        jobID = try c.decodeIfPresent(String.self, forKey: .jobID)
        mergedWith = try c.decodeIfPresent(String.self, forKey: .mergedWith)
        rejected = try c.decodeIfPresent([RejectedProject].self, forKey: .rejected) ?? []
    }
}

/// The surface AppModel talks to — injectable for tests.
public protocol EngineClientProtocol: AnyObject {
    /// Probe `health` and compare protocol versions. Cheap; called on
    /// connect and whenever the app needs to re-verify compatibility.
    func negotiate() -> NegotiationOutcome
    /// Fetch the quick-panel payload (`overview`).
    func fetchOverview() throws -> OverviewEnvelope
    /// Ask for a refresh (`refresh.request`); returns the accepted job.
    func requestRefresh(reason: String) throws -> RefreshAccepted
    /// Fetch one run's detail (`runs.get`): facts + tasks + BOTH usage
    /// calibers, from the engine's query layer only.
    func fetchRunDetail(project: String, runID: String) throws -> RunDetailEnvelope
    /// Query the cross-project run history (`runs.list`): filters,
    /// sorting and paging all execute engine-side — the app never
    /// re-sorts or re-pages locally.
    func fetchRunsList(query: RunsListQuery) throws -> RunsListEnvelope
    /// Registered projects (`projects.list`) — the option source for
    /// the history window's project filter.
    func fetchProjectsList(limit: Int) throws -> ProjectsListEnvelope
    /// Fetch one task's drill-down (`tasks.get`): attempts, session
    /// links, verifications, retry summary.
    func fetchTaskDetail(project: String, runID: String, revisionID: Int,
                         taskID: String) throws -> TaskDetailEnvelope
}

/// Async wrappers so callers stay off the main thread.
extension EngineClientProtocol {
    public func negotiateAsync() async -> NegotiationOutcome {
        await runOffMain { self.negotiate() }
    }

    public func fetchOverviewAsync() async throws -> OverviewEnvelope {
        try await runOffMainThrows { try self.fetchOverview() }
    }

    public func requestRefreshAsync(reason: String) async throws -> RefreshAccepted {
        try await runOffMainThrows { try self.requestRefresh(reason: reason) }
    }

    public func fetchRunDetailAsync(project: String, runID: String) async throws -> RunDetailEnvelope {
        try await runOffMainThrows { try self.fetchRunDetail(project: project, runID: runID) }
    }

    public func fetchRunsListAsync(query: RunsListQuery) async throws -> RunsListEnvelope {
        try await runOffMainThrows { try self.fetchRunsList(query: query) }
    }

    public func fetchProjectsListAsync(limit: Int) async throws -> ProjectsListEnvelope {
        try await runOffMainThrows { try self.fetchProjectsList(limit: limit) }
    }

    public func fetchTaskDetailAsync(project: String, runID: String,
                                     revisionID: Int, taskID: String) async throws -> TaskDetailEnvelope {
        try await runOffMainThrows {
            try self.fetchTaskDetail(project: project, runID: runID,
                                     revisionID: revisionID, taskID: taskID)
        }
    }
}

/// Hop to a background queue. The refresh path must never run POSIX IO
/// on the main actor (design: 刷新不阻塞主线程).
private let engineIOQueue = DispatchQueue(label: "org.orxanalytics.engine-io",
                                          qos: .userInitiated)

func runOffMain<T>(_ work: @escaping () -> T) async -> T {
    await withCheckedContinuation { cont in
        engineIOQueue.async { cont.resume(returning: work()) }
    }
}

func runOffMainThrows<T>(_ work: @escaping () throws -> T) async throws -> T {
    try await withCheckedThrowingContinuation { cont in
        engineIOQueue.async {
            do { cont.resume(returning: try work()) }
            catch { cont.resume(throwing: error) }
        }
    }
}

/// Newline-JSON client for the coordination engine's user-private Unix
/// socket (design §7 进程与 CLI 协作).
///
/// Behavior contract:
/// - Every request carries `protocol_version` (the engine checks each
///   one, not just the first).
/// - `unsupported_protocol` becomes `EngineFailure.incompatible` and the
///   client REMEMBERS it: further calls fail fast with the same report
///   instead of hammering the engine. A new `negotiate()` re-probes.
/// - The connection is persistent: per-connection collection demand
///   (`refresh.request` reasons) lives exactly as long as this client
///   stays connected. Closing the client releases only THIS client's
///   demand, never a CLI watcher's.
/// - Transport failures surface as `.unreachable`; the next call retries
///   the connection (the engine may have been started meanwhile).
public final class EngineSocketClient: EngineClientProtocol {
    private let socketPath: String
    private let timeout: TimeInterval
    private let ioQueue = DispatchQueue(label: "org.orxanalytics.engine-client",
                                        qos: .userInitiated)
    private var transport: UnixSocketLineTransport?
    private var lastOutcome: NegotiationOutcome?

    public init(socketPath: String, timeout: TimeInterval = 5.0) {
        self.socketPath = socketPath
        self.timeout = timeout
    }

    deinit {
        transport?.close()
    }

    /// Default socket location: `<data-dir>/engine.sock` where data-dir
    /// follows the CLI convention (`ORX_ANALYTICS_HOME` env override,
    /// else `~/.orx-analytics`). Design §8: the app reuses the existing
    /// analytics data-dir, it does not keep a second copy.
    public static func defaultSocketPath(environment: [String: String] =
                                            ProcessInfo.processInfo.environment) -> String {
        let dataDir: String
        if let home = environment["ORX_ANALYTICS_HOME"], !home.isEmpty {
            dataDir = home
        } else {
            dataDir = NSHomeDirectory() + "/.orx-analytics"
        }
        return dataDir + "/engine.sock"
    }

    // MARK: - EngineClientProtocol

    public func negotiate() -> NegotiationOutcome {
        ioQueue.sync { _negotiateLocked() }
    }

    public func fetchOverview() throws -> OverviewEnvelope {
        try ioQueue.sync {
            let data = try roundTrip(op: "overview", params: [:])
            return try Self.decode(OverviewEnvelope.self, from: data)
        }
    }

    public func requestRefresh(reason: String) throws -> RefreshAccepted {
        try ioQueue.sync {
            let data = try roundTrip(op: "refresh.request",
                                     params: ["reason": reason])
            // ok=true is guaranteed by roundTrip here; the op name differs.
            return try Self.decode(RefreshAccepted.self, from: data)
        }
    }

    public func fetchRunDetail(project: String, runID: String) throws -> RunDetailEnvelope {
        try ioQueue.sync {
            let data = try roundTrip(op: "runs.get",
                                     params: ["project": project,
                                              "run_id": runID])
            return try Self.decode(RunDetailEnvelope.self, from: data)
        }
    }

    public func fetchRunsList(query: RunsListQuery) throws -> RunsListEnvelope {
        try ioQueue.sync {
            let data = try roundTrip(op: "runs.list",
                                     params: query.wireParams())
            return try Self.decode(RunsListEnvelope.self, from: data)
        }
    }

    public func fetchProjectsList(limit: Int) throws -> ProjectsListEnvelope {
        try ioQueue.sync {
            let data = try roundTrip(op: "projects.list",
                                     params: ["limit": limit])
            return try Self.decode(ProjectsListEnvelope.self, from: data)
        }
    }

    public func fetchTaskDetail(project: String, runID: String, revisionID: Int,
                                taskID: String) throws -> TaskDetailEnvelope {
        try ioQueue.sync {
            let data = try roundTrip(op: "tasks.get",
                                     params: ["project": project,
                                              "run_id": runID,
                                              "revision_id": revisionID,
                                              "task_id": taskID])
            return try Self.decode(TaskDetailEnvelope.self, from: data)
        }
    }

    /// Drop the connection (releases this client's collection demand only).
    public func disconnect() {
        ioQueue.sync {
            transport?.close()
            transport = nil
        }
    }

    // MARK: - Internals (ioQueue only)

    private func _negotiateLocked() -> NegotiationOutcome {
        if let outcome = lastOutcome, case .incompatible = outcome {
            return outcome          // fail fast until re-negotiated
        }
        do {
            let data = try roundTrip(op: "health", params: [:])
            let header = try Self.decode(EngineEnvelopeHeader.self, from: data)
            if !header.ok {
                if header.error?.code == "unsupported_protocol",
                   let supported = header.error?.supportedVersion {
                    let outcome = NegotiationOutcome.incompatible(
                        requested: header.error?.requestedVersion ?? EngineProtocol.version,
                        supported: supported)
                    lastOutcome = outcome
                    return outcome
                }
                lastOutcome = .engineNotRunning
                return .engineNotRunning
            }
            let engineVersion = header.protocolVersion
            if engineVersion == EngineProtocol.version {
                let outcome = NegotiationOutcome.compatible(engineVersion: engineVersion)
                lastOutcome = outcome
                return outcome
            }
            // Engine answered a mismatched request with ok — still a
            // mismatch; report it rather than proceed on faith.
            let outcome = NegotiationOutcome.incompatible(
                requested: EngineProtocol.version, supported: engineVersion)
            lastOutcome = outcome
            return outcome
        } catch TransportError.unreachable, TransportError.timeout {
            transport?.close()
            transport = nil
            lastOutcome = .engineNotRunning
            return .engineNotRunning
        } catch let failure as EngineFailure {
            if case let .incompatible(requested, supported, _) = failure {
                let outcome = NegotiationOutcome.incompatible(requested: requested,
                                                              supported: supported)
                lastOutcome = outcome
                return outcome
            }
            lastOutcome = .engineNotRunning
            return .engineNotRunning
        } catch {
            transport?.close()
            transport = nil
            lastOutcome = .engineNotRunning
            return .engineNotRunning
        }
    }

    /// One request/response exchange; (re)connects on demand and stamps
    /// `protocol_version` on every payload. A non-ok response is mapped
    /// to its `EngineFailure` here so no caller tries to decode an error
    /// envelope as a payload.
    private func roundTrip(op: String, params: [String: Any]) throws -> Data {
        if case let .incompatible(requested, supported) = lastOutcome {
            // An earlier negotiation already proved the engine speaks a
            // different version — fail fast, do not hammer the socket.
            throw EngineFailure.incompatible(requested: requested,
                                             supported: supported,
                                             message: "protocol version mismatch already reported")
        }
        var payload = params
        payload["op"] = op
        payload["protocol_version"] = EngineProtocol.version
        let line = try Self.encode(payload)
        do {
            if transport == nil {
                let t = UnixSocketLineTransport(path: socketPath, timeout: timeout)
                try t.connect()
                transport = t
            }
            try transport?.sendLine(line)
            guard let response = try transport?.receiveLine(), !response.isEmpty else {
                throw TransportError.closedByPeer
            }
            let header = try Self.decode(EngineEnvelopeHeader.self, from: response)
            if !header.ok {
                let failure = Self.failure(for: header)
                if case let .incompatible(requested, supported, _) = failure {
                    // Remember: later calls fail fast with the same
                    // report instead of re-hammering the engine.
                    lastOutcome = .incompatible(requested: requested,
                                                supported: supported)
                }
                throw failure
            }
            return response
        } catch let e as TransportError {
            // A broken connection is retried on the NEXT call, not here:
            // retrying inside the same call could double-submit a
            // refresh request.
            transport?.close()
            transport = nil
            throw EngineFailure.unreachable("\(e)")
        }
    }

    // MARK: - codec

    private static func encode(_ payload: [String: Any]) throws -> Data {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: [.sortedKeys]) else {
            throw EngineFailure.invalidResponse("cannot encode request \(payload)")
        }
        return data
    }

    private static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw EngineFailure.invalidResponse("\(type) decode failed: \(error)")
        }
    }

    private static func failure(for header: EngineEnvelopeHeader) -> EngineFailure {
        guard let err = header.error else {
            return .invalidResponse("ok=false without an error object")
        }
        if err.code == "unsupported_protocol" {
            return .incompatible(requested: err.requestedVersion ?? EngineProtocol.version,
                                 supported: err.supportedVersion ?? EngineProtocol.version,
                                 message: err.message)
        }
        return .refused(code: err.code, message: err.message)
    }
}
