import XCTest
@testable import ORXAnalyticsCore

/// History query behavior (design §3 历史查询 / T010):
///
/// - every UI filter reaches the wire exactly once, engine-side
///   (project, text, run/task number, raw status, model, time range,
///   sort key + order + paging);
/// - paging walks limit/offset windows against the engine's page meta;
/// - the page is rendered in ENGINE order — never re-sorted locally
///   (the identity secondary key is what makes paging stable);
/// - consumption and coverage always render together; unknown usage
///   shows "—" with an explicit sorts-last note, never a zero;
/// - a failed query keeps the last successful page beside the reason.
@MainActor
final class HistoryModelTests: XCTestCase {

    private var client: FakeHistoryClient!

    override func setUp() {
        super.setUp()
        client = FakeHistoryClient()
    }

    // MARK: wire params

    private static func fixedNow() -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = 10
        components.day = 16
        components.hour = 6
        components.minute = 13
        components.second = 20
        components.timeZone = TimeZone(identifier: "UTC")
        return Calendar(identifier: .gregorian).date(from: components)!
    }

    func testSearchSendsEveryFilterEngineSide() async {
        let model = HistoryModel(client: client, pageSize: 50)
        model.filter.project = "/tmp/projA"
        model.filter.status = "done"
        model.filter.text = "gate work"
        model.filter.model = "glm"
        model.filter.runID = "R001"
        model.filter.taskID = "T002"
        model.filter.timeRange = .last7d
        model.filter.sortKey = .observedTokens
        model.filter.orderDesc = false

        await model.search(now: Self.fixedNow())

        guard let query = client.lastQuery else {
            XCTFail("search() 必须已完成一次查询")
            return
        }
        XCTAssertEqual(query.project, "/tmp/projA")
        XCTAssertEqual(query.status, "done", "状态过滤发送 raw 值，不是显示文案")
        XCTAssertEqual(query.text, "gate work")
        XCTAssertEqual(query.model, "glm")
        XCTAssertEqual(query.runID, "R001")
        XCTAssertEqual(query.taskID, "T002")
        XCTAssertEqual(query.sortKey, "observed_tokens")
        XCTAssertEqual(query.order, "asc")
        XCTAssertEqual(query.limit, 50)
        XCTAssertEqual(query.offset, 0)
        XCTAssertEqual(query.since, "2026-10-09T06:13:20+00:00",
                       "时间范围 bound 与引擎的 ISO 字典序比较同一格式")
        XCTAssertNil(query.until)
        // 刷新不阻塞界面：fetch 不在主线程
        XCTAssertFalse(client.sawMainThreadDuringFetch)
    }

    func testEmptyFilterFieldsAreDroppedNotSentAsEmptyStrings() {
        let query = HistoryFilter().query(pageSize: 50, offset: 0)
        XCTAssertNil(query.project)
        XCTAssertNil(query.status)
        XCTAssertNil(query.text)
        XCTAssertNil(query.model)
        XCTAssertNil(query.runID)
        XCTAssertNil(query.taskID)
        XCTAssertNil(query.since)
        let params = query.wireParams() as [String: AnyObject]
        for key in ["project", "text", "run_id", "task_id", "status",
                    "model", "since", "until"] {
            XCTAssertNil(params[key], "\(key) 不应出现在请求里（空 = 不过滤）")
        }
        XCTAssertEqual(params["sort"] as? String, "started_at")
        XCTAssertEqual(params["order"] as? String, "desc")
        XCTAssertEqual(params["limit"] as? Int, 50)
        XCTAssertEqual(params["offset"] as? Int, 0)
        // whitespace-only input is also "no filter"
        var blank = HistoryFilter()
        blank.text = "   "
        blank.model = " "
        let blankParams = blank.query(pageSize: 50, offset: 0).wireParams() as [String: AnyObject]
        XCTAssertNil(blankParams["text"])
        XCTAssertNil(blankParams["model"])
    }

    // MARK: paging

    func testPagingWalksOffsetWindowsAgainstPageMeta() async {
        client.runsList = Self.envelope(total: 120, returned: 50, offset: 0)
        let model = HistoryModel(client: client, pageSize: 50)
        await model.search()
        XCTAssertEqual(client.lastQuery?.offset, 0)
        XCTAssertTrue(model.canNextPage)
        XCTAssertFalse(model.canPreviousPage)
        XCTAssertEqual(model.rangeText, "第 1–50 条 · 共 120 条")

        client.runsList = Self.envelope(total: 120, returned: 50, offset: 50)
        await model.nextPage()
        XCTAssertEqual(client.lastQuery?.offset, 50, "分页稳定：同一查询仅推进 offset")
        XCTAssertTrue(model.canPreviousPage)
        XCTAssertEqual(model.rangeText, "第 51–100 条 · 共 120 条")

        client.runsList = Self.envelope(total: 120, returned: 20, offset: 100)
        await model.nextPage()
        XCTAssertEqual(client.lastQuery?.offset, 100)
        XCTAssertFalse(model.canNextPage, "offset+returned >= total_matched 时停止")

        await model.nextPage()
        XCTAssertEqual(client.lastQuery?.offset, 100, "越界翻页被拒绝")

        await model.search()
        XCTAssertEqual(client.lastQuery?.offset, 0, "新查询回到第一页")
    }

    // MARK: rows keep engine order + coverage honesty

    func testRowsKeepEngineOrderNeverResortLocally() async {
        // engine order (identity secondary key): B then A — the model
        // must render exactly that order whatever its local filter says
        client.runsList = Self.envelope(runs: [
            Self.run(project: "/tmp/projB", runID: "R001", tokens: 100),
            Self.run(project: "/tmp/projA", runID: "R009", tokens: 900),
        ], sortKey: "observed_tokens", order: "desc")
        let model = HistoryModel(client: client)
        model.filter.sortKey = .observedTokens
        await model.search()
        XCTAssertEqual(model.rows.map(\.id), ["/tmp/projB#R001", "/tmp/projA#R009"],
                       "排序与稳定次级键由引擎决定，本层不得重排")
    }

    func testConsumptionSortRowsAlwaysCarryCoverageBesideTheNumber() async {
        let known = Self.run(project: "/tmp/p", runID: "R001", tokens: 1200,
                             coverage: CoverageBlock(observed: 2, attempts: 3,
                                                     missingRecorded: 1, unassessed: 0))
        let zero = Self.run(project: "/tmp/p", runID: "R002", tokens: 0)
        let unknown = Self.run(project: "/tmp/p", runID: "R003", tokens: nil)

        let knownRow = HistoryRunRow.build(known, sortKey: "observed_tokens")
        // G012：token 千位分隔（统一格式器）
        XCTAssertEqual(knownRow.consumptionText, "已观测 token 1,200（全部版本）")
        XCTAssertEqual(knownRow.coverageText, "已观测 2/3 次尝试")
        XCTAssertNil(knownRow.unknownNote)

        // explicitly recorded zero renders 0 (metrics freeze)
        let zeroRow = HistoryRunRow.build(zero, sortKey: "observed_tokens")
        XCTAssertTrue(zeroRow.consumptionText.contains("token 0（全部版本）"))
        XCTAssertTrue(zeroRow.consumptionKnown)
        XCTAssertNil(zeroRow.unknownNote)

        // unknown renders "—" WITH a note that it is not the cheapest —
        // 缺数据不得显得“省”
        let unknownRow = HistoryRunRow.build(unknown, sortKey: "observed_tokens")
        XCTAssertFalse(unknownRow.consumptionKnown)
        XCTAssertTrue(unknownRow.consumptionText.contains("—"))
        XCTAssertFalse(unknownRow.consumptionText.contains(" 0"),
                       "未知不得补零")
        XCTAssertFalse(unknownRow.coverageText.isEmpty,
                       "覆盖程度随行展示，缺数据行也不例外")
        XCTAssertEqual(unknownRow.unknownNote,
                       "用量未知：排序时排在最后，不表示消耗最少")

        // under a TIME sort the note is shorter but still honest
        let timeRow = HistoryRunRow.build(unknown, sortKey: "started_at")
        XCTAssertEqual(timeRow.unknownNote, "用量未知")
    }

    func testRowConsumptionIsAllRevisionTotalFromEngine() {
        // the consumption block IS the engine's all-revision run total
        // (query_model._run_item); the row only labels it as such
        let row = HistoryRunRow.build(
            Self.run(project: "/tmp/p", runID: "R001", tokens: 5),
            sortKey: "started_at")
        XCTAssertTrue(row.consumptionText.contains("（全部版本）"),
                      "Run 汇总覆盖全部 revision，行内明确标注")
    }

    // MARK: failure behavior

    func testFailedQueryKeepsLastSuccessfulPageWithReason() async {
        client.runsList = Self.envelope(total: 2, returned: 1,
                                        runs: [Self.run(project: "/tmp/p",
                                                        runID: "R001", tokens: 10)])
        let model = HistoryModel(client: client)
        await model.search()
        let first = model.results
        XCTAssertTrue(model.canNextPage, "total 2 > returned 1：允许翻页")

        client.runsListError = .unreachable("socket closed")
        await model.nextPage()
        XCTAssertEqual(model.results, first, "失败保留上次成功结果页")
        XCTAssertNotNil(model.lastFailureText)
        XCTAssertTrue(model.lastFailureText?.contains("引擎连接失败") == true)
    }

    func testProjectOptionsLoadForFilterPicker() async {
        await HistoryModel(client: client).loadProjects()
        XCTAssertEqual(client.projectFetchCount, 1)
        XCTAssertFalse(client.capturedProjectLimits.isEmpty)
        XCTAssertEqual(client.capturedProjectLimits.last, 200)
    }

    func testProjectOptionsFailureIsNonFatalAndKeepsReason() async {
        client.projectsError = .unreachable("down")
        let model = HistoryModel(client: client)
        await model.loadProjects()
        XCTAssertNotNil(model.projectsFailureText)
        XCTAssertTrue(model.availableProjects.isEmpty)
    }

    // MARK: status/sort/time-range option sources

    func testStatusOptionsAreTheDisplayContractRawValues() {
        let raws = Set(HistoryStatusOption.known.map(\.raw))
        XCTAssertEqual(raws, ["planning", "running", "blocked", "done"],
                       "状态选项来自 display 契约的 raw 值（规划中/执行中/受阻/完成）")
        // G011: run-level blocked is 受阻 semantics — 暂停 belongs to the
        // GOAL control layer only
        let labels = Set(HistoryStatusOption.known.map(\.label))
        XCTAssertEqual(labels, ["规划中", "执行中", "受阻", "完成"])
    }

    /// G011 分层：历史列表每一行能同时辨认两个维度——执行事实
    /// （run.status）与 Goal 控制状态（goal.status 徽标文本）。
    func testRowsCarryGoalControlBesideRunStatus() {
        let blocked = RunFacts(
            project: "/tmp/p", runID: "R120",
            goal: GoalBlock(id: "G102", objective: "已取消目标",
                            status: DisplayStatus(raw: "cancelled", known: true,
                                                  display: "已取消")),
            status: DisplayStatus(raw: "blocked", known: true, display: "受阻"),
            startedAt: "2026-10-01T09:00:00+00:00",
            completedAt: nil, updatedAt: nil, createdAt: nil,
            timeBasis: nil, progress: nil, consumption: nil,
            dataAsOf: nil, schemaVersion: nil)
        let row = HistoryRunRow.build(blocked, sortKey: "started_at")
        XCTAssertEqual(row.run.status.display, "受阻")
        XCTAssertEqual(row.goalControlText, "已取消",
                       "已取消的执行保留在历史中，控制层照常可见")

        let pausedRun = RunFacts(
            project: "/tmp/p", runID: "R100",
            goal: GoalBlock(id: "G100", objective: "暂停目标",
                            status: DisplayStatus(raw: "paused", known: true,
                                                  display: "已暂停")),
            status: DisplayStatus(raw: "running", known: true, display: "执行中"),
            startedAt: "2026-10-01T09:00:00+00:00",
            completedAt: nil, updatedAt: nil, createdAt: nil,
            timeBasis: nil, progress: nil, consumption: nil,
            dataAsOf: nil, schemaVersion: nil)
        XCTAssertEqual(HistoryRunRow.build(pausedRun, sortKey: "started_at")
                        .goalControlText, "已暂停")

        // no goal row / unknown control value: the control layer is
        // absent, never guessed
        XCTAssertNil(HistoryRunRow.build(
            Self.run(project: "/tmp/p", runID: "R001", tokens: 5),
            sortKey: "started_at").goalControlText)
        let weird = RunFacts(
            project: "/tmp/p", runID: "R140",
            goal: GoalBlock(id: "G104", objective: nil,
                            status: DisplayStatus(raw: "hibernating",
                                                  known: false,
                                                  display: "未知状态")),
            status: DisplayStatus(raw: "running", known: true, display: "执行中"),
            startedAt: nil, completedAt: nil, updatedAt: nil, createdAt: nil,
            timeBasis: nil, progress: nil, consumption: nil,
            dataAsOf: nil, schemaVersion: nil)
        XCTAssertNil(HistoryRunRow.build(weird, sortKey: "started_at")
                        .goalControlText, "未知控制值不猜成任何终态")
    }

    func testSortKeysMirrorEngineSortKeys() {
        XCTAssertEqual(Set(HistorySortKey.allCases.map(\.rawValue)),
                       ["started_at", "completed_at", "observed_tokens"],
                       "排序键与引擎 SORT_KEYS 一一对应")
    }

    func testTimeRangeBoundUsesEngineLexicalISOStyle() {
        let now = Self.fixedNow()
        XCTAssertNil(HistoryTimeRange.all.sinceISO(now: now))
        XCTAssertEqual(HistoryTimeRange.last24h.sinceISO(now: now),
                       "2026-10-15T06:13:20+00:00")
        XCTAssertEqual(HistoryTimeRange.last7d.sinceISO(now: now),
                       "2026-10-09T06:13:20+00:00")
        XCTAssertEqual(HistoryTimeRange.last30d.sinceISO(now: now),
                       "2026-09-16T06:13:20+00:00")
    }

    // MARK: wire decode (simulator payload)

    func testSimulatorRunsListPayloadDecodes() throws {
        let data = EngineSimulator.encode(EngineSimulator.runsListResponse())
        let envelope = try JSONDecoder().decode(RunsListEnvelope.self, from: data)
        XCTAssertEqual(envelope.header.op, "runs.list")
        XCTAssertTrue(envelope.header.ok)
        XCTAssertEqual(envelope.query?.sort?.key, "observed_tokens")
        XCTAssertEqual(envelope.query?.sort?.secondary, "identity(project, run_id)")
        XCTAssertEqual(envelope.query?.filters?["status"], "done")
        XCTAssertEqual(envelope.page?.returned, 4)
        XCTAssertEqual(envelope.runs.count, 4)
        // nil observed_total decodes as nil (unknown, not zero) while a
        // recorded zero stays 0
        XCTAssertNil(envelope.runs[1].consumption?.observedTotal)
        XCTAssertEqual(envelope.runs[3].consumption?.observedTotal, 0)
        // every row can render consumption WITH coverage
        for run in envelope.runs {
            let row = HistoryRunRow.build(run, sortKey: "observed_tokens")
            XCTAssertFalse(row.coverageText.isEmpty)
        }
    }

    func testSimulatorProjectsListPayloadDecodes() throws {
        let data = EngineSimulator.encode(EngineSimulator.projectsListResponse())
        let envelope = try JSONDecoder().decode(ProjectsListEnvelope.self, from: data)
        XCTAssertEqual(envelope.projects.count, 2)
        XCTAssertEqual(envelope.projects.first?.displayName, "ProjA")
        XCTAssertEqual(envelope.projects.first?.runsKnown, 12)
        XCTAssertEqual(envelope.projects.first?.orxEnabled, true,
                       "SQLite 0/1 布尔解码")
    }

    // MARK: fixtures

    private static func run(project: String, runID: String, tokens: Int?,
                            coverage: CoverageBlock? = nil) -> RunFacts {
        let consumption: ConsumptionBlock?
        if let coverage {
            consumption = ConsumptionBlock(
                caliber: "orx_recorded",
                tokens: TokenSums(inputTokens: tokens.map { $0 - 200 },
                                  outputTokens: tokens.map { _ in 200 },
                                  cachedTokens: nil),
                observedTotal: tokens, missingFields: nil, accuracy: nil,
                coverage: coverage)
        } else {
            consumption = ConsumptionBlock(
                caliber: "orx_recorded", tokens: nil, observedTotal: tokens,
                missingFields: nil, accuracy: nil,
                coverage: CoverageBlock(observed: tokens == nil ? 0 : 2,
                                        attempts: 3, missingRecorded: 1,
                                        unassessed: 0))
        }
        return RunFacts(project: project, runID: runID, goal: nil,
                        status: DisplayStatus(raw: "done", known: true,
                                              display: "完成"),
                        startedAt: "2026-10-01T09:00:00+00:00",
                        completedAt: "2026-10-01T10:00:00+00:00",
                        updatedAt: nil, createdAt: nil,
                        timeBasis: TimeBasis(field: "started_at",
                                             value: "2026-10-01T09:00:00+00:00"),
                        progress: nil, consumption: consumption,
                        dataAsOf: nil, schemaVersion: nil)
    }

    private static func envelope(total: Int = 1, returned: Int = 1, offset: Int = 0,
                                 runs: [RunFacts]? = nil,
                                 sortKey: String = "started_at",
                                 order: String = "desc") -> RunsListEnvelope {
        let pageRuns = runs ?? [run(project: "/tmp/p", runID: "R001", tokens: 10)]
        return RunsListEnvelope(
            header: EngineEnvelopeHeader(op: "runs.list", ok: true,
                                         protocolVersion: 1, generatedAt: nil,
                                         error: nil),
            query: RunsListEnvelope.QueryEcho(
                filters: [:],
                sort: RunsListSortEcho(key: sortKey, order: order,
                                       secondary: "identity(project, run_id)")),
            runs: pageRuns,
            page: RunsListPageMeta(limit: 50, offset: offset,
                                   totalMatched: total, returned: returned))
    }
}

/// Injectable engine for history-model tests: captures the query,
/// serves canned envelopes/errors, records thread placement.
final class FakeHistoryClient: EngineClientProtocol {
    var runsList: RunsListEnvelope?
    var runsListError: EngineFailure?
    var projectsError: EngineFailure?
    private(set) var lastQuery: RunsListQuery?
    private(set) var projectFetchCount = 0
    private(set) var capturedProjectLimits: [Int] = []
    private(set) var sawMainThreadDuringFetch = false
    private let lock = NSLock()

    func negotiate() -> NegotiationOutcome { .compatible(engineVersion: 1) }

    func fetchOverview() throws -> OverviewEnvelope {
        throw EngineFailure.refused(code: "test", message: "not used")
    }

    func requestRefresh(reason: String) throws -> RefreshAccepted {
        RefreshAccepted(jobID: "job-1")
    }

    func fetchRunDetail(project: String, runID: String) throws -> RunDetailEnvelope {
        throw EngineFailure.refused(code: "test", message: "not used")
    }

    func fetchRunsList(query: RunsListQuery) throws -> RunsListEnvelope {
        lock.lock()
        lastQuery = query
        lock.unlock()
        sawMainThreadDuringFetch = sawMainThreadDuringFetch || Thread.isMainThread
        if let runsListError { throw runsListError }
        if let runsList { return runsList }
        let data = EngineSimulator.encode(EngineSimulator.runsListResponse())
        do {
            return try JSONDecoder().decode(RunsListEnvelope.self, from: data)
        } catch {
            throw EngineFailure.invalidResponse("fake runs list: \(error)")
        }
    }

    func fetchProjectsList(limit: Int) throws -> ProjectsListEnvelope {
        lock.lock()
        projectFetchCount += 1
        capturedProjectLimits.append(limit)
        lock.unlock()
        sawMainThreadDuringFetch = sawMainThreadDuringFetch || Thread.isMainThread
        if let projectsError { throw projectsError }
        let data = EngineSimulator.encode(EngineSimulator.projectsListResponse())
        do {
            return try JSONDecoder().decode(ProjectsListEnvelope.self, from: data)
        } catch {
            throw EngineFailure.invalidResponse("fake projects list: \(error)")
        }
    }

    func fetchTaskDetail(project: String, runID: String, revisionID: Int,
                         taskID: String) throws -> TaskDetailEnvelope {
        throw EngineFailure.refused(code: "test", message: "not used")
    }
}
