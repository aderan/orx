import Foundation

// MARK: - runs.list wire contract (query_model.runs_list, design §3/§7)

/// The Swift-side query for the engine's `runs.list` op. Field names
/// mirror the engine's `_RUNS_LIST_PARAMS` exactly; the wire params are
/// built here so no view ever assembles a request by hand.
///
/// Normalization rule: an empty/whitespace filter string means "no
/// filter" and is NOT sent — the engine treats a present-but-empty
/// value as a real filter, and sending `""` would silently narrow the
/// query. `nil` stays nil (absent from the payload).
public struct RunsListQuery: Equatable {
    /// Canonical project key (resolved path), or nil for all projects.
    public var project: String?
    /// Free text over goal and task objectives (casefolded by engine).
    public var text: String?
    public var runID: String?
    public var taskID: String?
    /// RAW run status ("running", "done", ...) — the engine compares
    /// raw values; display labels never travel as filters.
    public var status: String?
    public var model: String?
    /// ISO-8601 bounds (inclusive); nil = unbounded.
    public var since: String?
    public var until: String?
    /// One of the engine's SORT_KEYS: started_at | completed_at |
    /// observed_tokens.
    public var sortKey: String
    /// asc | desc. The engine always appends the composite identity as
    /// the secondary key, so paging is stable in both orders.
    public var order: String
    public var limit: Int
    public var offset: Int

    public init(project: String? = nil, text: String? = nil, runID: String? = nil,
                taskID: String? = nil, status: String? = nil, model: String? = nil,
                since: String? = nil, until: String? = nil,
                sortKey: String = "started_at", order: String = "desc",
                limit: Int = 50, offset: Int = 0) {
        self.project = project
        self.text = text
        self.runID = runID
        self.taskID = taskID
        self.status = status
        self.model = model
        self.since = since
        self.until = until
        self.sortKey = sortKey
        self.order = order
        self.limit = limit
        self.offset = offset
    }

    private static func trimmed(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// The exact JSON params for the socket request. Empty strings are
    /// dropped here (they mean "no filter" in the UI), everything else
    /// is sent verbatim for the engine to validate.
    public func wireParams() -> [String: Any] {
        var params: [String: Any] = [
            "sort": sortKey,
            "order": order,
            "limit": limit,
            "offset": offset,
        ]
        for (key, value) in [("project", Self.trimmed(project)),
                             ("text", Self.trimmed(text)),
                             ("run_id", Self.trimmed(runID)),
                             ("task_id", Self.trimmed(taskID)),
                             ("status", Self.trimmed(status)),
                             ("model", Self.trimmed(model)),
                             ("since", Self.trimmed(since)),
                             ("until", Self.trimmed(until))] {
            if let value { params[key] = value }
        }
        return params
    }
}

/// The engine's `page` block: total matched, window position. Paging
/// stability comes from the server-side identity secondary key, so a
/// changing offset over the same query is deterministic.
public struct RunsListPageMeta: Codable, Equatable {
    public let limit: Int
    public let offset: Int
    public let totalMatched: Int
    public let returned: Int

    enum CodingKeys: String, CodingKey {
        case limit, offset, returned
        case totalMatched = "total_matched"
    }
}

/// Echo of the server-side sort, including the frozen secondary key.
public struct RunsListSortEcho: Codable, Equatable {
    public let key: String
    public let order: String
    public let secondary: String?
}

/// The `runs.list` response: header + query echo + the page of run
/// items + per-project snapshots (each project keeps its own update
/// time). `runs` rows are the SAME `_run_item` shape as `runs.get`'s
/// `run` block, so `RunFacts` decodes them — one wire type, one
/// contract.
public struct RunsListEnvelope: Codable, Equatable {
    public struct QueryEcho: Codable, Equatable {
        public let filters: [String: String]?
        public let sort: RunsListSortEcho?
    }

    public let header: EngineEnvelopeHeader
    public let query: QueryEcho?
    public let runs: [RunFacts]
    public let page: RunsListPageMeta?
    public let snapshots: [String: SnapshotView]
    public let warnings: [QueryWarning]

    enum CodingKeys: String, CodingKey {
        case query, runs, page, snapshots, warnings
    }

    public init(header: EngineEnvelopeHeader, query: QueryEcho?,
                runs: [RunFacts], page: RunsListPageMeta?,
                snapshots: [String: SnapshotView] = [:],
                warnings: [QueryWarning] = []) {
        self.header = header
        self.query = query
        self.runs = runs
        self.page = page
        self.snapshots = snapshots
        self.warnings = warnings
    }

    public init(from decoder: Decoder) throws {
        header = try EngineEnvelopeHeader(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        query = try c.decodeIfPresent(QueryEcho.self, forKey: .query)
        runs = try c.decodeIfPresent([RunFacts].self, forKey: .runs) ?? []
        page = try c.decodeIfPresent(RunsListPageMeta.self, forKey: .page)
        snapshots = try c.decodeIfPresent([String: SnapshotView].self,
                                          forKey: .snapshots) ?? [:]
        warnings = try c.decodeIfPresent([QueryWarning].self,
                                         forKey: .warnings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try header.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(query, forKey: .query)
        try c.encode(runs, forKey: .runs)
        try c.encodeIfPresent(page, forKey: .page)
        try c.encode(snapshots, forKey: .snapshots)
        try c.encode(warnings, forKey: .warnings)
    }
}

// MARK: - projects.list (the project filter's option source)

/// One `projects.list` row — enough for the history filter's project
/// picker: canonical key + display name. `runsKnown` doubles as the
/// "has history at all" hint. Health fields stay in the payload but are
/// not reclassified here.
public struct ProjectListRow: Codable, Equatable, Identifiable {
    public let project: String
    public let name: String?
    public let orxEnabled: Bool?
    public let stateDBPresent: Bool?
    public let runsKnown: Int?
    public let dataHealth: DataHealthBlock?

    public var id: String { project }
    public var displayName: String { name?.isEmpty == false ? name! : project }

    enum CodingKeys: String, CodingKey {
        case project, name
        case orxEnabled = "orx_enabled"
        case stateDBPresent = "state_db_present"
        case runsKnown = "runs_known"
        case dataHealth = "data_health"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        project = try c.decode(String.self, forKey: .project)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        if let b = try? c.decodeIfPresent(Bool.self, forKey: .orxEnabled) {
            orxEnabled = b
        } else if let i = (try? c.decodeIfPresent(Int.self, forKey: .orxEnabled)) ?? nil {
            orxEnabled = i != 0
        } else {
            orxEnabled = nil
        }
        if let b = try? c.decodeIfPresent(Bool.self, forKey: .stateDBPresent) {
            stateDBPresent = b
        } else if let i = (try? c.decodeIfPresent(Int.self, forKey: .stateDBPresent)) ?? nil {
            stateDBPresent = i != 0
        } else {
            stateDBPresent = nil
        }
        runsKnown = WireSupport.flexInt(c, .runsKnown)
        dataHealth = try c.decodeIfPresent(DataHealthBlock.self,
                                           forKey: .dataHealth)
    }
}

/// The `projects.list` response (paging envelope shared with runs.list).
public struct ProjectsListEnvelope: Codable, Equatable {
    public let header: EngineEnvelopeHeader
    public let projects: [ProjectListRow]
    public let page: RunsListPageMeta?
    public let snapshots: [String: SnapshotView]
    public let warnings: [QueryWarning]

    enum CodingKeys: String, CodingKey {
        case projects, page, snapshots, warnings
    }

    public init(from decoder: Decoder) throws {
        header = try EngineEnvelopeHeader(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        projects = try c.decodeIfPresent([ProjectListRow].self,
                                         forKey: .projects) ?? []
        page = try c.decodeIfPresent(RunsListPageMeta.self, forKey: .page)
        snapshots = try c.decodeIfPresent([String: SnapshotView].self,
                                          forKey: .snapshots) ?? [:]
        warnings = try c.decodeIfPresent([QueryWarning].self,
                                         forKey: .warnings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try header.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(projects, forKey: .projects)
        try c.encodeIfPresent(page, forKey: .page)
        try c.encode(snapshots, forKey: .snapshots)
        try c.encode(warnings, forKey: .warnings)
    }
}
