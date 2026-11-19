import Foundation

// MARK: - shared envelope blocks

/// One `warnings[]` entry the engine attaches to every query response.
public struct QueryWarning: Codable, Equatable {
    public let code: String
    public let project: String?
    public let detail: String?

    public init(code: String, project: String? = nil, detail: String? = nil) {
        self.code = code
        self.project = project
        self.detail = detail
    }
}

/// The `snapshots` map entry: which successful snapshot served this
/// project, and whether its schema passed the version gate.
public struct SnapshotView: Codable, Equatable {
    public let snapshotID: Int?
    public let takenAt: String?
    public let schemaVersion: String?
    public let supported: Bool?
    public let sourcePath: String?

    enum CodingKeys: String, CodingKey {
        case supported
        case snapshotID = "snapshot_id"
        case takenAt = "taken_at"
        case schemaVersion = "schema_version"
        case sourcePath = "source_path"
    }
}

/// Coverage honesty block carried by every usage book (design §5):
/// observed / attempts / missing_recorded / unassessed.
public struct CoverageBlock: Codable, Equatable {
    public let observed: Int
    public let attempts: Int
    public let missingRecorded: Int
    public let unassessed: Int

    enum CodingKeys: String, CodingKey {
        case observed, attempts
        case missingRecorded = "missing_recorded"
        case unassessed = "unassessed"
    }
}

/// The engine's own data-health classification (正常/更新延迟/来源不可读/
/// 版本不支持/未知) with its underlying facts. None of the facts is a
/// guess — unknown stays nil (design §4).
public struct DataHealthBlock: Codable, Equatable {
    public let health: String?
    public let sourceReadable: Bool?
    public let schemaSupported: Bool?
    public let stale: Bool?

    public init(health: String?, sourceReadable: Bool? = nil,
                schemaSupported: Bool? = nil, stale: Bool? = nil) {
        self.health = health
        self.sourceReadable = sourceReadable
        self.schemaSupported = schemaSupported
        self.stale = stale
    }

    enum CodingKeys: String, CodingKey {
        case health, stale
        case sourceReadable = "source_readable"
        case schemaSupported = "schema_supported"
    }
}

/// One row of `overview.project_health`.
public struct ProjectHealthRow: Codable, Equatable, Identifiable {
    public let project: String
    public let dataHealth: DataHealthBlock?
    public let snapshotID: Int?
    public let takenAt: String?

    public var id: String { project }

    public init(project: String, dataHealth: DataHealthBlock? = nil,
                snapshotID: Int? = nil, takenAt: String? = nil) {
        self.project = project
        self.dataHealth = dataHealth
        self.snapshotID = snapshotID
        self.takenAt = takenAt
    }

    enum CodingKeys: String, CodingKey {
        case project
        case dataHealth = "data_health"
        case snapshotID = "snapshot_id"
        case takenAt = "taken_at"
    }
}

// MARK: - usage books (usage_readmodel)

/// One ledger from `usage_readmodel._book`: which attempts, coverage,
/// partial token sums (per-field, source semantics preserved), missing
/// field counts, accuracy distribution and the as-of stamp.
public struct UsageBook: Codable, Equatable {
    public let attempts: Int
    public let attemptIDs: [Int]
    public let revisionIDs: [Int?]
    public let coverage: CoverageBlock?
    public let tokens: TokenSums?
    public let missingFields: [String: Int]?
    public let accuracy: [String: Int]?
    public let asOf: String?
    public let note: String?

    enum CodingKeys: String, CodingKey {
        case attempts, tokens, accuracy, note
        case attemptIDs = "attempt_ids"
        case revisionIDs = "revision_ids"
        case coverage = "coverage"
        case missingFields = "missing_fields"
        case asOf = "as_of"
    }
}

/// Versioned derived "标准化处理量" — displayed as its own metric, never
/// merged into the raw token sums (metrics freeze).
public struct NormalizedProcessing: Codable, Equatable {
    public let version: Int
    public let value: Int?
    public let partial: Bool?
    public let rowsDerived: Int?
    public let rowsMissing: Int?
    public let cacheSemantics: [String: Int]?
    public let note: String?

    enum CodingKeys: String, CodingKey {
        case version, value, partial, note
        case rowsDerived = "rows_derived"
        case rowsMissing = "rows_missing"
        case cacheSemantics = "cache_semantics"
    }
}

public struct AttributionRule: Codable, Equatable {
    public let rule: String
}

/// A book flattened together with its (revision, task) identity — the
/// `per_task[]` / `per_revision[]` entries of `run_usage`.
public struct TaskBookEntry: Codable, Equatable, Identifiable {
    public let book: UsageBook
    public let revisionID: Int?
    public let revision: Int?
    public let taskID: String?

    public var id: String { "\(revisionID ?? -1)#\(taskID ?? "-")" }

    enum CodingKeys: String, CodingKey {
        case revision, book
        case revisionID = "revision_id"
        case taskID = "task_id"
    }

    public init(book: UsageBook, revisionID: Int?, revision: Int?, taskID: String?) {
        self.book = book
        self.revisionID = revisionID
        self.revision = revision
        self.taskID = taskID
    }

    public init(from decoder: Decoder) throws {
        book = try UsageBook(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        revisionID = WireSupport.flexInt(c, .revisionID)
        revision = WireSupport.flexInt(c, .revision)
        taskID = try c.decodeIfPresent(String.self, forKey: .taskID)
    }

    public func encode(to encoder: Encoder) throws {
        try book.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(revisionID, forKey: .revisionID)
        try c.encodeIfPresent(revision, forKey: .revision)
        try c.encodeIfPresent(taskID, forKey: .taskID)
    }
}

/// Canonical observation of one attempt: source + accuracy + per-field
/// tokens (all fields may be nil = unknown, never zero-filled).
public struct ObservationBlock: Codable, Equatable {
    public let source: String?
    public let accuracy: String?
    public let tokens: TokenSums?
}

/// The session (or codex thread) an attempt was linked to, with the
/// evidence strength of that link.
public struct SessionLink: Codable, Equatable {
    public let sessionID: String?
    public let authority: String?
    public let confidence: String?

    enum CodingKeys: String, CodingKey {
        case authority, confidence
        case sessionID = "session_id"
    }
}

/// One attempt row of `task_usage.attempts[]` — result, usage, session
/// association, failure reason and missing-usage reason (the evidence
/// the 尝试与证据 zone shows).
public struct AttemptDetail: Codable, Equatable, Identifiable {
    public let attemptID: Int
    public let role: String?
    public let revisionID: Int?
    public let taskID: String?
    public let result: String?
    public let failureReason: String?
    public let startedAt: String?
    public let endedAt: String?
    public let harness: String?
    public let model: String?
    public let usageMissingReason: String?
    public let observation: ObservationBlock?
    public let sessionLink: SessionLink?

    public var id: Int { attemptID }

    enum CodingKeys: String, CodingKey {
        case role, result, harness, model
        case attemptID = "attempt_id"
        case revisionID = "revision_id"
        case taskID = "task_id"
        case failureReason = "failure_reason"
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case usageMissingReason = "usage_missing_reason"
        case observation = "observation"
        case sessionLink = "session_link"
    }
}

// MARK: - run usage (usage_readmodel.run_usage)

public struct RunBooks: Codable, Equatable {
    public let tasks: UsageBook?
    public let planner: UsageBook?
    public let controller: UsageBook?
    public let unattributedWithinRun: UsageBook?

    enum CodingKeys: String, CodingKey {
        case tasks, planner, controller
        case unattributedWithinRun = "unattributed_within_run"
    }
}

public struct RunUsage: Codable, Equatable {
    public let caliber: String?
    public let project: String?
    public let runID: String?
    public let revisionScope: String?
    public let snapshotID: Int?
    public let asOf: String?
    public let tokens: TokenSums?
    public let missingFields: [String: Int]?
    public let accuracy: [String: Int]?
    public let coverage: CoverageBlock?
    public let books: RunBooks?
    public let perTask: [TaskBookEntry]?
    public let perRevision: [TaskBookEntry]?
    public let normalizedProcessing: NormalizedProcessing?
    public let attribution: AttributionRule?

    enum CodingKeys: String, CodingKey {
        case caliber, tokens, accuracy, coverage, books, attribution
        case project = "project"
        case runID = "run_id"
        case revisionScope = "revision_scope"
        case snapshotID = "snapshot_id"
        case asOf = "as_of"
        case missingFields = "missing_fields"
        case perTask = "per_task"
        case perRevision = "per_revision"
        case normalizedProcessing = "normalized_processing"
    }
}

// MARK: - task usage (usage_readmodel.task_usage)

public struct TaskBooks: Codable, Equatable {
    public let worker: UsageBook?
    public let verifier: UsageBook?
    public let otherRoles: UsageBook?

    enum CodingKeys: String, CodingKey {
        case worker, verifier
        case otherRoles = "other_roles"
    }
}

public struct TaskUsage: Codable, Equatable {
    public let caliber: String?
    public let project: String?
    public let runID: String?
    public let revisionID: Int?
    public let taskID: String?
    public let snapshotID: Int?
    public let asOf: String?
    public let tokens: TokenSums?
    public let missingFields: [String: Int]?
    public let accuracy: [String: Int]?
    public let coverage: CoverageBlock?
    public let books: TaskBooks?
    public let normalizedProcessing: NormalizedProcessing?
    public let attempts: [AttemptDetail]
    public let attribution: AttributionRule?

    enum CodingKeys: String, CodingKey {
        case caliber, tokens, accuracy, coverage, books, attribution, attempts
        case project = "project"
        case runID = "run_id"
        case revisionID = "revision_id"
        case taskID = "task_id"
        case snapshotID = "snapshot_id"
        case asOf = "as_of"
        case missingFields = "missing_fields"
        case normalizedProcessing = "normalized_processing"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        caliber = try c.decodeIfPresent(String.self, forKey: .caliber)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        runID = try c.decodeIfPresent(String.self, forKey: .runID)
        revisionID = WireSupport.flexInt(c, .revisionID)
        taskID = try c.decodeIfPresent(String.self, forKey: .taskID)
        snapshotID = WireSupport.flexInt(c, .snapshotID)
        asOf = try c.decodeIfPresent(String.self, forKey: .asOf)
        tokens = try c.decodeIfPresent(TokenSums.self, forKey: .tokens)
        missingFields = try c.decodeIfPresent([String: Int].self, forKey: .missingFields)
        accuracy = try c.decodeIfPresent([String: Int].self, forKey: .accuracy)
        coverage = try c.decodeIfPresent(CoverageBlock.self, forKey: .coverage)
        books = try c.decodeIfPresent(TaskBooks.self, forKey: .books)
        normalizedProcessing = try c.decodeIfPresent(NormalizedProcessing.self,
                                                     forKey: .normalizedProcessing)
        attempts = try c.decodeIfPresent([AttemptDetail].self, forKey: .attempts) ?? []
        attribution = try c.decodeIfPresent(AttributionRule.self, forKey: .attribution)
    }
}

// MARK: - native session preview caliber

/// `_turn_totals` / tree turn sums of the native caliber. Independent of
/// `TokenSums`: different fields, never mixed.
public struct TurnTotals: Codable, Equatable {
    public let turns: Int?
    public let inputTokens: Int?
    public let outputTokens: Int?
    public let reasoningTokens: Int?
    public let cacheRead: Int?
    public let cacheCreation: Int?

    enum CodingKeys: String, CodingKey {
        case turns
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case reasoningTokens = "reasoning_tokens"
        case cacheRead = "cache_read"
        case cacheCreation = "cache_creation"
    }
}

/// `correlate.zcode_tree_totals` for one linked zcode session tree.
public struct TreeTotals: Codable, Equatable {
    public let root: String?
    public let sessionsInTree: Int?
    public let turns: Int?
    public let inputTokens: Int?
    public let outputTokens: Int?
    public let reasoningTokens: Int?
    public let cacheRead: Int?
    public let cacheCreation: Int?

    enum CodingKeys: String, CodingKey {
        case root, turns
        case sessionsInTree = "sessions_in_tree"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case reasoningTokens = "reasoning_tokens"
        case cacheRead = "cache_read"
        case cacheCreation = "cache_creation"
    }
}

/// `correlate.codex_thread_totals` for one linked codex thread.
public struct ThreadTotals: Codable, Equatable {
    public let threadID: String?
    public let usageRows: Int?
    public let turnsWithUsage: Int?
    public let inputTokens: Int?
    public let cachedInput: Int?
    public let cacheWrite: Int?
    public let outputTokens: Int?
    public let reasoningOutput: Int?

    enum CodingKeys: String, CodingKey {
        case threadID = "thread_id"
        case usageRows = "usage_rows"
        case turnsWithUsage = "turns_with_usage"
        case inputTokens = "input_tokens"
        case cachedInput = "cached_input"
        case cacheWrite = "cache_write"
        case outputTokens = "output_tokens"
        case reasoningOutput = "reasoning_output"
    }
}

/// One `linked_attempts[]` entry of the native preview.
public struct NativeLinkedAttempt: Codable, Equatable, Identifiable {
    public let attemptID: Int
    public let role: String?
    public let revisionID: Int?
    public let taskID: String?
    public let harness: String?
    public let authority: String?
    public let confidence: String?
    public let sessionID: String?
    public let threadID: String?
    public let candidates: [String]?
    public let note: String?
    public let treeTotals: TreeTotals?
    public let threadTotals: ThreadTotals?

    public var id: Int { attemptID }

    enum CodingKeys: String, CodingKey {
        case role, harness, authority, confidence, candidates, note
        case attemptID = "attempt_id"
        case revisionID = "revision_id"
        case taskID = "task_id"
        case sessionID = "session_id"
        case threadID = "thread_id"
        case treeTotals = "tree_totals"
        case threadTotals = "thread_totals"
    }
}

public struct NativeUnion: Codable, Equatable {
    public let sessions: [String]?
    public let tokens: TurnTotals?
}

public struct SessionGroupRef: Codable, Equatable {
    public let sessionIDs: [String]?
    public let attemptIDs: [Int]?

    enum CodingKeys: String, CodingKey {
        case sessionIDs = "session_ids"
        case attemptIDs = "attempt_ids"
    }
}

public struct ControllerAttribution: Codable, Equatable {
    public let attributed: Bool?
    public let runs: [String]?
    public let reason: String?
    public let basis: String?

    enum CodingKeys: String, CodingKey {
        case attributed, runs, reason, basis
    }
}

/// One child-session anchor behind a controller attribution: the linked
/// subagent session, the attempt it carried, and the authority (stored
/// session_ref / ORX v11 nonce) that tied them.
public struct ControllerChildEvidence: Codable, Equatable {
    public let sessionID: String?
    public let attemptID: Int?
    public let role: String?
    public let authority: String?
    public let confidence: String?
    public let nonce: String?

    enum CodingKeys: String, CodingKey {
        case role, authority, confidence, nonce
        case sessionID = "session_id"
        case attemptID = "attempt_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionID = try c.decodeIfPresent(String.self, forKey: .sessionID)
        attemptID = WireSupport.flexInt(c, .attemptID)
        role = try c.decodeIfPresent(String.self, forKey: .role)
        authority = try c.decodeIfPresent(String.self, forKey: .authority)
        confidence = try c.decodeIfPresent(String.self, forKey: .confidence)
        nonce = try c.decodeIfPresent(String.self, forKey: .nonce)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(sessionID, forKey: .sessionID)
        try c.encodeIfPresent(attemptID, forKey: .attemptID)
        try c.encodeIfPresent(role, forKey: .role)
        try c.encodeIfPresent(authority, forKey: .authority)
        try c.encodeIfPresent(confidence, forKey: .confidence)
        try c.encodeIfPresent(nonce, forKey: .nonce)
    }
}

/// The controller's OWN usage in the zcode `model_usage` request caliber
/// (`usage_readmodel.request_totals`): one request is
/// (session_id, logical_request_id, attempt_index) and is counted exactly
/// once; `cacheRead` is a SUBSET of input, so `processedTokens` is
/// input + output and never adds cache on top; `turn_usage` is a different
/// caliber and never appears here. Unknown fields stay nil — an error row
/// with explicit zeros is flagged via `errorRowsZeroTokens`, never a free
/// or hidden request.
public struct RequestUsage: Codable, Equatable {
    public let caliber: String?
    public let source: String?
    public let requests: Int?
    public let inputTokens: Int?
    public let outputTokens: Int?
    public let reasoningTokens: Int?
    public let cacheRead: Int?
    public let cacheCreation: Int?
    public let providerTotal: Int?
    public let computedTotal: Int?
    public let processedTokens: Int?
    public let processedRule: String?
    public let statusCounts: [String: Int]?
    public let settlingRequestRows: Int?
    public let errorRowsZeroTokens: Int?
    public let missingFields: [String: Int]?
    public let rowsMissingProcessed: Int?
    public let lastActivityMS: Int?

    enum CodingKeys: String, CodingKey {
        case caliber, source, requests
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case reasoningTokens = "reasoning_tokens"
        case cacheRead = "cache_read"
        case cacheCreation = "cache_creation"
        case providerTotal = "provider_total"
        case computedTotal = "computed_total"
        case processedTokens = "processed_tokens"
        case processedRule = "processed_rule"
        case statusCounts = "status_counts"
        case settlingRequestRows = "settling_request_rows"
        case errorRowsZeroTokens = "error_rows_zero_tokens"
        case missingFields = "missing_fields"
        case rowsMissingProcessed = "rows_missing_processed"
        case lastActivityMS = "last_activity_ms"
    }
}

/// A controller (host) session: the root ancestor of the run's linked
/// subagent sessions, carrying its OWN request-caliber usage only —
/// worker/verifier descendants belong to their attempts' books.
public struct ControllerSession: Codable, Equatable, Identifiable {
    public let sessionID: String?
    public let directory: String?
    public let directoryMatchesProject: Bool?
    public let createdMS: Int?
    public let updatedMS: Int?
    public let ownUsage: RequestUsage?
    public let runAttribution: ControllerAttribution?

    public var id: String { sessionID ?? "?" }

    enum CodingKeys: String, CodingKey {
        case directory
        case sessionID = "session_id"
        case directoryMatchesProject = "directory_matches_project"
        case createdMS = "created_ms"
        case updatedMS = "updated_ms"
        case ownUsage = "own_usage"
        case runAttribution = "run_attribution"
    }
}

/// The per-run verdict for the controller preview: which controller
/// session(s) this run can claim on unique evidence, or the honest reason
/// it cannot (`host_session_unbound`, shared across runs, source not
/// imported, ...). Carries ids and reasons only — never token numbers.
public struct ControllerForRun: Codable, Equatable {
    public let runID: String?
    public let status: String?
    public let sessionIDs: [String]?
    public let reason: String?
    public let note: String?

    enum CodingKeys: String, CodingKey {
        case status, reason, note
        case runID = "run_id"
        case sessionIDs = "session_ids"
    }
}

/// The 原生会话预览 caliber (design §5). The engine structurally keeps
/// it separate from the ORX-recorded caliber — `neverSumWith` names the
/// caliber it must never be added to, and there is no combined-total
/// field anywhere in this payload.
public struct NativePreview: Codable, Equatable {
    public let caliber: String?
    public let project: String?
    public let runID: String?
    public let snapshotID: Int?
    public let asOf: String?
    public let asOfSessionsMS: Int?
    public let linkedAttempts: [NativeLinkedAttempt]?
    public let union: NativeUnion?
    public let sharedSessionTrees: [SessionGroupRef]?
    public let subtreeConflicts: [SessionGroupRef]?
    public let controllerSessions: [ControllerSession]?
    public let controllerForRun: ControllerForRun?
    public let neverSumWith: String?
    public let note: String?

    enum CodingKeys: String, CodingKey {
        case caliber, project, union, note
        case runID = "run_id"
        case snapshotID = "snapshot_id"
        case asOf = "as_of"
        case asOfSessionsMS = "as_of_sessions_ms"
        case linkedAttempts = "linked_attempts"
        case sharedSessionTrees = "shared_session_trees"
        case subtreeConflicts = "subtree_conflicts"
        case controllerSessions = "controller_sessions"
        case controllerForRun = "controller_for_run"
        case neverSumWith = "never_sum_with"
    }
}

// MARK: - runs.get

/// The full `_run_item` payload (richer than the overview card): all
/// timestamps, time basis, progress of the current plan and consumption
/// over ALL revisions.
public struct RunFacts: Codable, Equatable, Identifiable {
    public let project: String
    public let runID: String
    public let goal: GoalBlock?
    public let status: DisplayStatus
    public let startedAt: String?
    public let completedAt: String?
    public let updatedAt: String?
    public let createdAt: String?
    public let timeBasis: TimeBasis?
    public let progress: ProgressBlock?
    public let consumption: ConsumptionBlock?
    public let dataAsOf: String?
    public let schemaVersion: String?

    public var id: String { "\(project)#\(runID)" }

    enum CodingKeys: String, CodingKey {
        case project, goal, status, progress, consumption
        case runID = "run_id"
        case startedAt = "started_at"
        case completedAt = "completed_at"
        case updatedAt = "updated_at"
        case createdAt = "created_at"
        case timeBasis = "time_basis"
        case dataAsOf = "data_as_of"
        case schemaVersion = "schema_version"
    }
}

public struct TimeBasis: Codable, Equatable {
    public let field: String?
    public let value: String?
}

public struct RevisionSummary: Codable, Equatable, Identifiable {
    public let revisionID: Int
    public let revision: Int?
    public let status: String?
    public let createdAt: String?
    public let taskCount: Int

    public var id: Int { revisionID }

    enum CodingKeys: String, CodingKey {
        case revision, status
        case revisionID = "revision_id"
        case createdAt = "created_at"
        case taskCount = "task_count"
    }
}

/// One task of the run's task list (all revisions; the UI groups them
/// under their revision and keeps history collapsed).
public struct TaskListItem: Codable, Equatable, Identifiable {
    public let revisionID: Int
    public let revision: Int?
    public let taskID: String
    public let objective: String?
    public let status: DisplayStatus
    public let failureReason: String?
    public let attemptCount: Int
    public let consumption: ConsumptionBlock?

    public var id: String { "\(revisionID)#\(taskID)" }

    enum CodingKeys: String, CodingKey {
        case revision, objective, status, consumption
        case revisionID = "revision_id"
        case taskID = "task_id"
        case failureReason = "failure_reason"
        case attemptCount = "attempt_count"
    }
}

/// The `runs.get` response: header + found/reason + run facts +
/// revisions + tasks + BOTH calibers (usage, native_preview) as
/// siblings — structurally impossible to add by accident.
public struct RunDetailEnvelope: Codable, Equatable {
    public let header: EngineEnvelopeHeader
    public let found: Bool
    public let reason: String?
    public let project: String?
    public let runID: String?
    public let run: RunFacts?
    public let revisions: [RevisionSummary]
    public let tasks: [TaskListItem]
    public let usage: RunUsage?
    public let nativePreview: NativePreview?
    public let snapshots: [String: SnapshotView]
    public let warnings: [QueryWarning]

    enum CodingKeys: String, CodingKey {
        case found, reason, project, run, revisions, tasks, usage
        case runID = "run_id"
        case nativePreview = "native_preview"
        case snapshots = "snapshots"
        case warnings = "warnings"
    }

    public init(from decoder: Decoder) throws {
        header = try EngineEnvelopeHeader(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        found = try c.decodeIfPresent(Bool.self, forKey: .found) ?? false
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        runID = try c.decodeIfPresent(String.self, forKey: .runID)
        run = try c.decodeIfPresent(RunFacts.self, forKey: .run)
        revisions = try c.decodeIfPresent([RevisionSummary].self, forKey: .revisions) ?? []
        tasks = try c.decodeIfPresent([TaskListItem].self, forKey: .tasks) ?? []
        usage = try c.decodeIfPresent(RunUsage.self, forKey: .usage)
        nativePreview = try c.decodeIfPresent(NativePreview.self, forKey: .nativePreview)
        snapshots = try c.decodeIfPresent([String: SnapshotView].self, forKey: .snapshots) ?? [:]
        warnings = try c.decodeIfPresent([QueryWarning].self, forKey: .warnings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try header.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(found, forKey: .found)
        try c.encodeIfPresent(reason, forKey: .reason)
        try c.encodeIfPresent(project, forKey: .project)
        try c.encodeIfPresent(runID, forKey: .runID)
        try c.encodeIfPresent(run, forKey: .run)
        try c.encode(revisions, forKey: .revisions)
        try c.encode(tasks, forKey: .tasks)
        try c.encodeIfPresent(usage, forKey: .usage)
        try c.encodeIfPresent(nativePreview, forKey: .nativePreview)
        try c.encode(snapshots, forKey: .snapshots)
        try c.encode(warnings, forKey: .warnings)
    }
}

// MARK: - tasks.get

public struct TaskFacts: Codable, Equatable {
    public let objective: String?
    public let status: DisplayStatus?
    public let failureReason: String?
    public let createdAt: String?
    public let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case objective, status
        case failureReason = "failure_reason"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

/// One `orx_verifications` row (evidence). `passed` arrives as a SQLite
/// INTEGER (0/1) — `flexBool` maps it without inventing values.
public struct VerificationRow: Codable, Equatable, Identifiable {
    public let rowID: Int?
    public let attemptID: Int?
    public let kind: String?
    public let command: String?
    public let exitCode: Int?
    public let passed: Bool?
    public let createdAt: String?

    public var id: Int? { rowID }

    enum CodingKeys: String, CodingKey {
        case kind, command
        case rowID = "id"
        case attemptID = "attempt_id"
        case exitCode = "exit_code"
        case passed = "passed"
        case createdAt = "created_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rowID = WireSupport.flexInt(c, .rowID)
        attemptID = WireSupport.flexInt(c, .attemptID)
        kind = try c.decodeIfPresent(String.self, forKey: .kind)
        command = try c.decodeIfPresent(String.self, forKey: .command)
        exitCode = WireSupport.flexInt(c, .exitCode)
        passed = WireSupport.flexBool(c, .passed)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(rowID, forKey: .rowID)
        try c.encodeIfPresent(attemptID, forKey: .attemptID)
        try c.encodeIfPresent(kind, forKey: .kind)
        try c.encodeIfPresent(command, forKey: .command)
        try c.encodeIfPresent(exitCode, forKey: .exitCode)
        try c.encodeIfPresent(passed, forKey: .passed)
        try c.encodeIfPresent(createdAt, forKey: .createdAt)
    }
}

public struct RetrySummary: Codable, Equatable {
    public let recovered: Bool?
    public let failingAttemptIDs: [Int]?
    public let successorAttemptIDs: [Int]?

    enum CodingKeys: String, CodingKey {
        case recovered
        case failingAttemptIDs = "failing_attempt_ids"
        case successorAttemptIDs = "successor_attempt_ids"
    }
}

/// The `tasks.get` response: task facts + usage books with attempts and
/// session links + verification evidence + retry summary.
public struct TaskDetailEnvelope: Codable, Equatable {
    public let header: EngineEnvelopeHeader
    public let found: Bool
    public let reason: String?
    public let project: String?
    public let runID: String?
    public let revisionID: Int?
    public let taskID: String?
    public let task: TaskFacts?
    public let usage: TaskUsage?
    public let verifications: [VerificationRow]
    public let retry: RetrySummary?
    public let snapshots: [String: SnapshotView]
    public let warnings: [QueryWarning]

    enum CodingKeys: String, CodingKey {
        case found, reason, project, task, usage, verifications, retry
        case runID = "run_id"
        case revisionID = "revision_id"
        case taskID = "task_id"
        case snapshots = "snapshots"
        case warnings = "warnings"
    }

    public init(from decoder: Decoder) throws {
        header = try EngineEnvelopeHeader(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        found = try c.decodeIfPresent(Bool.self, forKey: .found) ?? false
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        runID = try c.decodeIfPresent(String.self, forKey: .runID)
        revisionID = WireSupport.flexInt(c, .revisionID)
        taskID = try c.decodeIfPresent(String.self, forKey: .taskID)
        task = try c.decodeIfPresent(TaskFacts.self, forKey: .task)
        usage = try c.decodeIfPresent(TaskUsage.self, forKey: .usage)
        verifications = try c.decodeIfPresent([VerificationRow].self, forKey: .verifications) ?? []
        retry = try c.decodeIfPresent(RetrySummary.self, forKey: .retry)
        snapshots = try c.decodeIfPresent([String: SnapshotView].self, forKey: .snapshots) ?? [:]
        warnings = try c.decodeIfPresent([QueryWarning].self, forKey: .warnings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try header.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(found, forKey: .found)
        try c.encodeIfPresent(reason, forKey: .reason)
        try c.encodeIfPresent(project, forKey: .project)
        try c.encodeIfPresent(runID, forKey: .runID)
        try c.encodeIfPresent(revisionID, forKey: .revisionID)
        try c.encodeIfPresent(taskID, forKey: .taskID)
        try c.encodeIfPresent(task, forKey: .task)
        try c.encodeIfPresent(usage, forKey: .usage)
        try c.encode(verifications, forKey: .verifications)
        try c.encodeIfPresent(retry, forKey: .retry)
        try c.encode(snapshots, forKey: .snapshots)
        try c.encode(warnings, forKey: .warnings)
    }
}
