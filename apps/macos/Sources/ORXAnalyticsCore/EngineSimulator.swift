import Foundation

/// A minimal in-process fake engine speaking the REAL wire protocol
/// (newline JSON, `unsupported_protocol` semantics) over a real Unix
/// domain socket. Test and smoke-only: it exists so the client, transport
/// and cache layers can be exercised without touching a live engine,
/// real projects or real agent sessions (CONTRIBUTING: 测试用合成来源).
///
/// Not compiled into any UI code path; used by XCTest and the
/// `--smoke-test` self-check.
public final class EngineSimulator {
    /// What the simulator should answer for a request line.
    public typealias Handler = (_ request: Data) -> Data

    public private(set) var socketPath: String = ""
    public private(set) var receivedRequests: [Data] = []

    private var handler: Handler
    private var serverFD: Int32 = -1
    private var running = false
    private let stateLock = NSLock()
    private let thread = Thread.self

    public init(handler: @escaping Handler) {
        self.handler = handler
    }

    /// Bind and start serving on a fresh temp socket path.
    public func start() throws {
        // sockaddr_un.sun_path holds 104 bytes; a UUID-named path under
        // the long per-user temp dir overflows it, so keep it short.
        let short = "orxs-\(UUID().uuidString.prefix(8))"
        let dir = URL(fileURLWithPath: "/tmp/\(short)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        socketPath = dir.appendingPathComponent("e.sock").path

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw TransportError.io("simulator socket() failed")
        }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8)
        withUnsafeMutableBytes(of: &addr.sun_path) { $0.copyBytes(from: bytes) }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let rc = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.bind(fd, sa, len)
            }
        }
        guard rc == 0, listen(fd, 8) == 0 else {
            Darwin.close(fd)
            throw TransportError.io("simulator bind/listen failed: \(String(cString: strerror(errno)))")
        }
        serverFD = fd
        running = true
        Thread.detachNewThread { [weak self] in self?.serveLoop() }
    }

    public func stop() {
        stateLock.lock()
        running = false
        let fd = serverFD
        serverFD = -1
        stateLock.unlock()
        if fd >= 0 { Darwin.close(fd) }
        if !socketPath.isEmpty {
            // remove the whole short-lived dir (socket + parent)
            try? FileManager.default.removeItem(
                at: URL(fileURLWithPath: socketPath).deletingLastPathComponent())
        }
    }

    deinit { stop() }

    private func serveLoop() {
        while true {
            stateLock.lock()
            let alive = running
            stateLock.unlock()
            guard alive else { return }

            // poll() so stop() is honored even while idle in accept().
            var pfd = pollfd(fd: serverFD, events: Int16(POLLIN), revents: 0)
            let ready = poll(&pfd, 1, 150)
            if ready == 0 { continue }              // timeout: re-check `running`
            if ready < 0 {
                if errno == EINTR { continue }
                return
            }
            let clientFD = accept(serverFD, nil, nil)
            guard clientFD >= 0 else { continue }
            Thread.detachNewThread { [weak self] in self?.serveClient(clientFD) }
        }
    }

    private func serveClient(_ fd: Int32) {
        defer { Darwin.close(fd) }
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65_536)
        connectionLoop: while true {
            let n = chunk.withUnsafeMutableBufferPointer { buf in
                read(fd, buf.baseAddress, buf.count)
            }
            if n <= 0 { break }
            buffer.append(contentsOf: chunk[0..<n])
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = Data(buffer[buffer.startIndex..<nl])
                buffer.removeSubrange(buffer.startIndex...nl)
                stateLock.lock()
                receivedRequests.append(line)
                let response = handler(line)
                stateLock.unlock()
                var out = response
                out.append(0x0A)
                if !_writeAll(fd, out) { break connectionLoop }
            }
        }
    }

    private func _writeAll(_ fd: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var sent = 0
            while sent < raw.count {
                let n = write(fd, raw.baseAddress!.advanced(by: sent),
                              raw.count - sent)
                if n <= 0 {
                    if n < 0 && errno == EINTR { continue }
                    return false
                }
                sent += n
            }
            return true
        }
    }

    // MARK: - Canned behaviors

    /// A valid v1 `overview` response with the given counts. Field
    /// names/shapes mirror the Python query layer exactly
    /// (`cached_input`, per-field `missing_fields` dicts, INTEGER
    /// revision ids, coverage blocks) so client tests exercise the real
    /// wire contract.
    public static func overviewResponse(activeRuns: Int, attention: Int,
                                        recentCompleted: Int = 0) -> [String: Any] {
        [
            "op": "overview",
            "ok": true,
            "protocol_version": 1,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "summary": [
                "active_runs": activeRuns,
                "attention": attention,
                "recent_completed": recentCompleted,
                "projects": ["considered": 1, "queried": 1, "excluded": []],
            ],
            "active": (0..<activeRuns).map { i in
                [
                    "project": "/tmp/proj\(i)",
                    "run_id": "R00\(i + 1)",
                    "goal": ["id": "G001", "objective": "目标 \(i)",
                             "status": ["raw": "active", "known": true,
                                        "display": "进行中"]] as [String: Any],
                    "status": ["raw": "running", "known": true,
                               "display": "执行中"],
                    "started_at": "2026-10-16T00:00:00+00:00",
                    "progress": ["completed": 1, "total": 3,
                                 "revision_id": 101, "revision": 1,
                                 "basis": "current revision only"],
                    "consumption": [
                        "caliber": "orx_recorded",
                        "tokens": ["input_tokens": 1000 + i,
                                   "output_tokens": 200,
                                   "cached_input": NSNull()] as [String: Any],
                        "observed_total": 1200 + i,
                        "missing_fields": ["input_tokens": 0,
                                           "output_tokens": 0,
                                           "cached_input": 1],
                        "accuracy": ["exact": 2, "estimated": 0,
                                     "unknown": 0],
                        "coverage": ["observed": 2, "attempts": 3,
                                     "missing_recorded": 1, "unassessed": 0],
                    ],
                    "data_as_of": "2026-10-16T00:00:00+00:00",
                ] as [String: Any]
            },
            "attention": (0..<attention).map { i in
                [
                    "kind": "failed_task",
                    "project": "/tmp/proj\(i)",
                    "run_id": "R001",
                    "task_id": "T00\(i + 1)",
                    "revision_id": 101,
                    "detail": "task failed",
                ] as [String: Any]
            },
            "recent_completed": [],
            "project_health": [],
            "snapshots": [:],
            "coverage": [:],
            "warnings": [],
        ]
    }

    /// A valid v1 `runs.get` response mirroring `query_model.runs_get`
    /// + `usage_readmodel.run_usage` + `native_session_preview`,
    /// including the `never_sum_with` caliber marker.
    public static func runDetailResponse(project: String = "/tmp/proj0",
                                         runID: String = "R001") -> [String: Any] {
        let consumption: [String: Any] = [
            "caliber": "orx_recorded",
            "tokens": ["input_tokens": 1000, "output_tokens": 200,
                       "cached_input": NSNull()],
            "observed_total": 1200,
            "missing_fields": ["input_tokens": 0, "output_tokens": 0,
                               "cached_input": 1],
            "accuracy": ["exact": 2, "estimated": 0, "unknown": 0],
            "coverage": ["observed": 2, "attempts": 3,
                         "missing_recorded": 1, "unassessed": 0],
        ]
        let book: [String: Any] = [
            "attempts": 2, "attempt_ids": [1, 2], "revision_ids": [101],
            "coverage": ["observed": 2, "attempts": 2,
                         "missing_recorded": 0, "unassessed": 0],
            "tokens": ["input_tokens": 800, "output_tokens": 150,
                       "cached_input": NSNull()],
            "missing_fields": ["input_tokens": 0, "output_tokens": 0,
                               "cached_input": 1],
            "accuracy": ["exact": 2, "estimated": 0, "unknown": 0],
            "as_of": "2026-10-16T00:00:00+00:00",
        ]
        let withIdentity: ([String: Any]) -> [String: Any] = { extra in
            book.merging(extra) { _, new in new }
        }
        return [
            "op": "runs.get", "ok": true, "protocol_version": 1,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "project": project, "run_id": runID, "found": true,
            "run": [
                "project": project, "run_id": runID,
                "identity": ["project": project, "run_id": runID],
                "goal": ["id": "G001", "objective": "模拟目标",
                         "status": ["raw": "active", "known": true,
                                    "display": "进行中"]] as [String: Any],
                "status": ["raw": "running", "known": true,
                           "display": "执行中"],
                "started_at": "2026-10-16T00:00:00+00:00",
                "completed_at": NSNull(),
                "updated_at": "2026-10-16T00:00:00+00:00",
                "created_at": "2026-10-16T00:00:00+00:00",
                "time_basis": ["field": "started_at",
                               "value": "2026-10-16T00:00:00+00:00"],
                "progress": ["completed": 1, "total": 2, "revision_id": 101,
                             "revision": 1, "basis": "current revision only"],
                "consumption": consumption,
                "data_as_of": "2026-10-16T00:00:00+00:00",
                "schema_version": "11",
            ] as [String: Any],
            "revisions": [
                ["revision_id": 101, "revision": 1, "status": "active",
                 "created_at": "2026-10-16T00:00:00+00:00", "task_count": 2],
            ],
            "tasks": [
                ["project": project, "run_id": runID,
                 "identity": ["project": project, "run_id": runID,
                              "revision_id": 101, "task_id": "T001"],
                 "revision_id": 101, "revision": 1, "task_id": "T001",
                 "objective": "模拟任务",
                 "status": ["raw": "running", "known": true,
                            "display": "执行中"],
                 "failure_reason": NSNull(), "attempt_count": 1,
                 "consumption": consumption] as [String: Any],
            ],
            "usage": [
                "caliber": "orx_recorded", "project": project,
                "run_id": runID,
                "revision_scope": "all revisions via attempts.run_id",
                "snapshot_id": 1, "as_of": "2026-10-16T00:00:00+00:00",
                "tokens": ["input_tokens": 1000, "output_tokens": 200,
                           "cached_input": NSNull()],
                "missing_fields": ["input_tokens": 0, "output_tokens": 0,
                                   "cached_input": 1],
                "accuracy": ["exact": 2, "estimated": 0, "unknown": 0],
                "coverage": ["observed": 2, "attempts": 3,
                             "missing_recorded": 1, "unassessed": 0],
                "books": [
                    "tasks": book,
                    "planner": ["attempts": 1, "attempt_ids": [3],
                                "revision_ids": [NSNull()],
                                "coverage": ["observed": 1, "attempts": 1,
                                             "missing_recorded": 0,
                                             "unassessed": 0],
                                "tokens": ["input_tokens": 200,
                                           "output_tokens": 50,
                                           "cached_input": NSNull()],
                                "missing_fields": ["input_tokens": 0,
                                                   "output_tokens": 0,
                                                   "cached_input": 1],
                                "accuracy": ["exact": 1, "estimated": 0,
                                             "unknown": 0],
                                "as_of": "2026-10-16T00:00:00+00:00"],
                    "controller": ["attempts": 0, "attempt_ids": [],
                                   "revision_ids": [],
                                   "coverage": ["observed": 0, "attempts": 0,
                                                "missing_recorded": 0,
                                                "unassessed": 0],
                                   "tokens": ["input_tokens": NSNull(),
                                              "output_tokens": NSNull(),
                                              "cached_input": NSNull()],
                                   "missing_fields": ["input_tokens": 0,
                                                      "output_tokens": 0,
                                                      "cached_input": 0],
                                   "accuracy": ["exact": 0, "estimated": 0,
                                                "unknown": 0],
                                   "as_of": "2026-10-16T00:00:00+00:00",
                                   "note": "ORX-recorded controller only"],
                    "unattributed_within_run": book,
                ],
                "per_task": [withIdentity([
                    "revision_id": 101, "revision": 1, "task_id": "T001"])],
                "per_revision": [withIdentity([
                    "revision_id": 101, "revision": 1])],
                "normalized_processing": [
                    "version": 1, "value": 1200, "partial": true,
                    "rows_derived": 2, "rows_missing": 1,
                    "cache_semantics": ["cache_subset": 2,
                                        "cache_separate": 0, "unknown": 1],
                    "note": "separate versioned derived volume"],
                "attribution": ["rule": "run attribution via attempts.run_id"],
            ] as [String: Any],
            "native_preview": [
                "caliber": "native_session_preview", "project": project,
                "run_id": runID, "snapshot_id": 1,
                "as_of": "2026-10-16T00:00:00+00:00",
                "as_of_sessions_ms": 1_790_000_000_000,
                "linked_attempts": [
                    ["attempt_id": 1, "role": "worker", "revision_id": 101,
                     "task_id": "T001", "harness": "zcode",
                     "authority": "session_metadata", "confidence": "high",
                     "session_id": "sess-1",
                     "tree_totals": ["root": "sess-1", "sessions_in_tree": 1,
                                     "turns": 4, "input_tokens": 500,
                                     "output_tokens": 100,
                                     "reasoning_tokens": NSNull(),
                                     "cache_read": 20,
                                     "cache_creation": 2]] as [String: Any],
                ],
                "union": ["sessions": ["sess-1"],
                          "tokens": ["turns": 4, "input_tokens": 500,
                                     "output_tokens": 100,
                                     "reasoning_tokens": NSNull(),
                                     "cache_read": 20, "cache_creation": 2]],
                "shared_session_trees": [], "subtree_conflicts": [],
                "controller_sessions": [],
                "never_sum_with": "orx_recorded",
                "note": "cumulative native-session numbers, a separate caliber",
            ] as [String: Any],
            "snapshots": [project: ["snapshot_id": 1,
                                    "taken_at": "2026-10-16T00:00:00+00:00",
                                    "schema_version": "11",
                                    "supported": true,
                                    "source_path": "\(project)/.orx/state.db"]],
            "coverage": ["project": project, "found": true, "snapshot_id": 1],
            "warnings": [],
        ]
    }

    /// A valid v1 `tasks.get` response mirroring
    /// `query_model.tasks_get` + `task_usage`.
    public static func taskDetailResponse(project: String = "/tmp/proj0",
                                          runID: String = "R001",
                                          revisionID: Int = 101,
                                          taskID: String = "T001") -> [String: Any] {
        [
            "op": "tasks.get", "ok": true, "protocol_version": 1,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "project": project, "run_id": runID,
            "revision_id": revisionID, "task_id": taskID, "found": true,
            "task": [
                "identity": ["project": project, "run_id": runID,
                             "revision_id": revisionID, "task_id": taskID],
                "objective": "模拟任务",
                "status": ["raw": "running", "known": true,
                           "display": "执行中"],
                "failure_reason": NSNull(),
                "created_at": "2026-10-16T00:00:00+00:00",
                "updated_at": "2026-10-16T00:00:00+00:00",
            ] as [String: Any],
            "usage": [
                "caliber": "orx_recorded", "project": project,
                "run_id": runID, "revision_id": revisionID,
                "task_id": taskID, "snapshot_id": 1,
                "as_of": "2026-10-16T00:00:00+00:00",
                "tokens": ["input_tokens": 800, "output_tokens": 150,
                           "cached_input": NSNull()],
                "missing_fields": ["input_tokens": 0, "output_tokens": 0,
                                   "cached_input": 1],
                "accuracy": ["exact": 2, "estimated": 0, "unknown": 0],
                "coverage": ["observed": 2, "attempts": 2,
                             "missing_recorded": 0, "unassessed": 0],
                "books": [
                    "worker": ["attempts": 1, "attempt_ids": [1],
                               "revision_ids": [revisionID],
                               "coverage": ["observed": 1, "attempts": 1,
                                            "missing_recorded": 0,
                                            "unassessed": 0],
                               "tokens": ["input_tokens": 800,
                                          "output_tokens": 150,
                                          "cached_input": NSNull()],
                               "missing_fields": ["input_tokens": 0,
                                                  "output_tokens": 0,
                                                  "cached_input": 1],
                               "accuracy": ["exact": 1, "estimated": 0,
                                            "unknown": 0],
                               "as_of": "2026-10-16T00:00:00+00:00"],
                    "verifier": ["attempts": 1, "attempt_ids": [2],
                                 "revision_ids": [revisionID],
                                 "coverage": ["observed": 1, "attempts": 1,
                                              "missing_recorded": 0,
                                              "unassessed": 0],
                                 "tokens": ["input_tokens": 100,
                                            "output_tokens": 20,
                                            "cached_input": NSNull()],
                                 "missing_fields": ["input_tokens": 0,
                                                    "output_tokens": 0,
                                                    "cached_input": 1],
                                 "accuracy": ["exact": 1, "estimated": 0,
                                              "unknown": 0],
                                 "as_of": "2026-10-16T00:00:00+00:00"],
                ],
                "normalized_processing": [
                    "version": 1, "value": 950, "partial": false,
                    "rows_derived": 2, "rows_missing": 0,
                    "cache_semantics": ["cache_subset": 2,
                                        "cache_separate": 0, "unknown": 0],
                    "note": "separate versioned derived volume"],
                "attempts": [
                    ["attempt_id": 1, "role": "worker",
                     "revision_id": revisionID, "task_id": taskID,
                     "result": NSNull(), "failure_reason": NSNull(),
                     "started_at": "2026-10-16T00:00:00+00:00",
                     "ended_at": NSNull(), "harness": "zcode",
                     "model": "glm-4.7", "usage_missing_reason": NSNull(),
                     "observation": ["source": "zcode_db",
                                     "accuracy": "exact",
                                     "tokens": ["input_tokens": 800,
                                                "output_tokens": 150,
                                                "cached_input": NSNull()]],
                     "session_link": ["session_id": "sess-1",
                                      "authority": "session_metadata",
                                      "confidence": "high"]] as [String: Any],
                    ["attempt_id": 2, "role": "verifier",
                     "revision_id": revisionID, "task_id": taskID,
                     "result": "completed", "failure_reason": NSNull(),
                     "started_at": "2026-10-16T00:00:00+00:00",
                     "ended_at": "2026-10-16T00:00:10+00:00",
                     "harness": "zcode", "model": "glm-4.7-air",
                     "usage_missing_reason": NSNull(),
                     "observation": ["source": "native_cli",
                                     "accuracy": "exact",
                                     "tokens": ["input_tokens": 100,
                                                "output_tokens": 20,
                                                "cached_input": 0]],
                     "session_link": ["session_id": "sess-2",
                                      "authority": "session_metadata",
                                      "confidence": "high"]] as [String: Any],
                ],
                "attribution": ["rule": "task consumption rule"],
            ] as [String: Any],
            "verifications": [
                ["id": 1, "attempt_id": 2, "kind": "command",
                 "command": "pytest -q", "exit_code": 0, "passed": 1,
                 "created_at": "2026-10-16T00:00:10+00:00"],
            ],
            "retry": ["recovered": false, "failing_attempt_ids": [],
                      "successor_attempt_ids": []],
            "snapshots": [project: ["snapshot_id": 1,
                                    "taken_at": "2026-10-16T00:00:00+00:00",
                                    "schema_version": "11",
                                    "supported": true,
                                    "source_path": "\(project)/.orx/state.db"]],
            "coverage": ["project": project, "found": true, "snapshot_id": 1],
            "warnings": [],
        ]
    }

    /// A valid v1 `runs.list` response mirroring `query_model.runs_list`:
    /// query echo (filters + sort with the frozen secondary key), the
    /// page of `_run_item` rows (same shape as runs.get's `run`), and
    /// the page meta. `runs` entries give (tokens, observed, attempts)
    /// — nil tokens mean NO canonical observation (unknown, not zero).
    public static func runsListResponse(
            projects: [String] = ["/tmp/projA", "/tmp/projB"],
            observedTokens: [Int?] = [1200, nil, 350, 0],
            status: String = "done",
            sortKey: String = "observed_tokens",
            order: String = "desc",
            limit: Int = 50,
            offset: Int = 0) -> [String: Any] {
        let runs: [[String: Any]] = observedTokens.enumerated().map { i, total in
            [
                "project": projects[i % projects.count],
                "run_id": String(format: "R%03d", i + 1),
                "identity": [:],
                "goal": ["id": "G001", "objective": "历史目标 \(i)",
                         "status": ["raw": "done", "known": true,
                                    "display": "已完成"]] as [String: Any],
                "status": ["raw": status, "known": true,
                           "display": "完成"] as [String: Any],
                "started_at": "2026-10-\(String(format: "%02d", i + 1))T09:00:00+00:00",
                "completed_at": "2026-10-\(String(format: "%02d", i + 1))T10:00:00+00:00",
                "updated_at": "2026-10-16T00:00:00+00:00",
                "created_at": "2026-10-01T00:00:00+00:00",
                "time_basis": ["field": "started_at",
                               "value": "2026-10-\(String(format: "%02d", i + 1))T09:00:00+00:00"],
                "progress": ["completed": 2, "total": 3,
                             "revision_id": 101, "revision": 2,
                             "basis": "current revision only"],
                "consumption": [
                    "caliber": "orx_recorded",
                    "tokens": total == nil
                        ? ["input_tokens": NSNull(), "output_tokens": NSNull(),
                           "cached_input": NSNull()] as [String: Any]
                        : ["input_tokens": total == 0 ? 0 : total! - 200,
                           "output_tokens": total == 0 ? 0 : 200,
                           "cached_input": NSNull()],
                    "observed_total": total ?? NSNull(),
                    "missing_fields": ["input_tokens": 0, "output_tokens": 0,
                                       "cached_input": 1],
                    "accuracy": ["exact": 2, "estimated": 0, "unknown": 0],
                    "coverage": ["observed": 2, "attempts": 3,
                                 "missing_recorded": 1, "unassessed": 0],
                ] as [String: Any],
                "data_as_of": "2026-10-16T00:00:00+00:00",
                "schema_version": "11",
            ] as [String: Any]
        }
        return [
            "op": "runs.list", "ok": true, "protocol_version": 1,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "query": [
                "filters": ["status": status],
                "sort": ["key": sortKey, "order": order,
                         "secondary": "identity(project, run_id)"],
            ] as [String: Any],
            "runs": runs,
            "page": ["limit": limit, "offset": offset,
                     "total_matched": offset + observedTokens.count + 3,
                     "returned": observedTokens.count],
            "snapshots": [:],
            "coverage": ["projects": ["considered": 2, "queried": 2,
                                      "excluded": []],
                         "runs": ["matched": offset + observedTokens.count + 3]],
            "warnings": [],
        ]
    }

    /// A valid v1 `projects.list` response mirroring
    /// `query_model.projects_list`.
    public static func projectsListResponse(
            projects: [(key: String, name: String, runs: Int)] = [
                ("/tmp/projA", "ProjA", 12), ("/tmp/projB", "ProjBORX", 3),
            ]) -> [String: Any] {
        [
            "op": "projects.list", "ok": true, "protocol_version": 1,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "projects": projects.map { p in
                [
                    "project": p.key, "name": p.name,
                    "registered_at": "2026-10-01T00:00:00+00:00",
                    "orx_enabled": 1, "state_db_present": 1,
                    "runs_known": p.runs,
                    "last_snapshot": ["snapshot_id": 1,
                                      "taken_at": "2026-10-16T00:00:00+00:00",
                                      "schema_version": "11",
                                      "supported": true],
                    "data_health": ["health": "正常", "source_readable": true,
                                    "schema_supported": true, "stale": false],
                ] as [String: Any]
            },
            "page": ["limit": 200, "offset": 0,
                     "total_matched": projects.count,
                     "returned": projects.count],
            "snapshots": [:],
            "coverage": ["registered": projects.count,
                         "returned": projects.count],
            "warnings": [],
        ]
    }

    /// A valid v1 `health` response.
    public static func healthResponse(version: Int = 1) -> [String: Any] {
        [
            "op": "health",
            "ok": true,
            "protocol_version": version,
            "generated_at": "2026-10-16T00:00:00+00:00",
            "engine": ["implemented": true, "protocol_version": version],
        ]
    }

    /// The engine's version-mismatch refusal (engine.py `unsupported_protocol`).
    public static func unsupportedProtocolResponse(requested: Int,
                                                   supported: Int,
                                                   op: String? = nil) -> [String: Any] {
        [
            "op": op ?? "health",
            "ok": false,
            "protocol_version": supported,
            "error": [
                "code": "unsupported_protocol",
                "message": "protocol version \(requested) is not supported by "
                    + "this engine (supported: \(supported)); the request is "
                    + "refused, not taken over",
                "requested": requested,
                "supported": supported,
            ] as [String: Any],
        ]
    }

    public static func encode(_ object: [String: Any]) -> Data {
        (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    }
}
