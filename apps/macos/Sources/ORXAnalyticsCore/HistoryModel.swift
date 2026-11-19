import Foundation
import Combine

/// The history window's filter state — all UI-facing fields. Empty
/// strings mean "no filter"; `RunsListQuery.wireParams()` drops them so
/// an empty box never narrows the query.
public struct HistoryFilter: Equatable {
    /// Canonical project key, or "" for all projects.
    public var project: String = ""
    /// RAW run status, or "" for all statuses.
    public var status: String = ""
    /// Goal or task title text.
    public var text: String = ""
    /// Model substring.
    public var model: String = ""
    public var runID: String = ""
    public var taskID: String = ""
    public var timeRange: HistoryTimeRange = .all
    public var sortKey: HistorySortKey = .startedAt
    public var orderDesc: Bool = true

    public init() {}

    /// "" or whitespace-only -> nil (an empty box is NOT a filter).
    private static func nonEmpty(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }

    public func query(pageSize: Int, offset: Int, now: Date = Date()) -> RunsListQuery {
        RunsListQuery(project: project.isEmpty ? nil : project,
                      text: Self.nonEmpty(text),
                      runID: Self.nonEmpty(runID),
                      taskID: Self.nonEmpty(taskID),
                      status: status.isEmpty ? nil : status,
                      model: Self.nonEmpty(model),
                      since: timeRange.sinceISO(now: now),
                      until: nil,
                      sortKey: sortKey.rawValue,
                      order: orderDesc ? "desc" : "asc",
                      limit: pageSize,
                      offset: offset)
    }
}

/// Loads and holds the cross-project history query (runs.list) plus the
/// project options (projects.list). Behavior contracts:
///
/// - All filtering, sorting AND paging execute engine-side; this model
///   only mirrors the filter state and the returned page — it never
///   re-sorts rows locally (the engine's identity secondary key is what
///   makes paging stable, and re-sorting a page would break it).
/// - Loads run off the main actor; a failed load keeps the last
///   successful results with the failure reason beside them.
/// - A new search supersedes an in-flight one (stale pages of a
///   previous filter never overwrite fresh results).
@MainActor
public final class HistoryModel: ObservableObject {
    @Published public var filter = HistoryFilter()
    @Published public private(set) var results: RunsListEnvelope?
    @Published public private(set) var isLoading = false
    @Published public private(set) var lastFailureText: String?
    @Published public private(set) var availableProjects: [ProjectListRow] = []
    @Published public private(set) var projectsFailureText: String?

    public let client: EngineClientProtocol
    public let pageSize: Int

    private var fetchTask: Task<Void, Never>?
    private var currentOffset = 0

    public init(client: EngineClientProtocol, pageSize: Int = 50) {
        self.client = client
        self.pageSize = pageSize
    }

    // MARK: - presentation

    /// Rows of the CURRENT page, in engine order. The sort key rides
    /// along so each row knows whether it must carry its coverage note
    /// (it always does — see HistoryRunRow).
    public var rows: [HistoryRunRow] {
        guard let results else { return [] }
        let sortKey = results.query?.sort?.key ?? filter.sortKey.rawValue
        return results.runs.map { HistoryRunRow.build($0, sortKey: sortKey) }
    }

    /// "第 1–50 条 · 共 137 条" style window description. Counts render
    /// through the unified grouping formatter (G012).
    public var rangeText: String {
        guard let page = results?.page else { return "未查询" }
        if page.totalMatched == 0 {
            return "没有匹配的执行"
        }
        let first = page.offset + 1
        let last = page.offset + page.returned
        return "第 \(MetricsFormat.countText(first))–\(MetricsFormat.countText(last)) 条 · 共 \(MetricsFormat.countText(page.totalMatched)) 条"
    }

    public var canPreviousPage: Bool { currentOffset > 0 }
    public var canNextPage: Bool {
        guard let page = results?.page else { return false }
        return page.offset + page.returned < page.totalMatched
    }

    // MARK: - queries

    /// New search: back to the first page with the current filter.
    /// Awaits its own fetch, so callers (and tests) observe completed
    /// state deterministically.
    public func search(now: Date = Date()) async {
        currentOffset = 0
        await startFetch(offset: 0, now: now).value
    }

    public func nextPage(now: Date = Date()) async {
        guard canNextPage else { return }
        let offset = currentOffset + pageSize
        currentOffset = offset
        await startFetch(offset: offset, now: now).value
    }

    public func previousPage(now: Date = Date()) async {
        guard canPreviousPage else { return }
        let offset = max(0, currentOffset - pageSize)
        currentOffset = offset
        await startFetch(offset: offset, now: now).value
    }

    @discardableResult
    private func startFetch(offset: Int, now: Date) -> Task<Void, Never> {
        // a newer query supersedes the in-flight one; the stale page
        // must never overwrite fresh results
        fetchTask?.cancel()
        let query = filter.query(pageSize: pageSize, offset: offset, now: now)
        let task = Task { [weak self] () -> Void in
            await self?.runFetch(query: query)
        }
        fetchTask = task
        return task
    }

    private func runFetch(query: RunsListQuery) async {
        isLoading = true
        defer { isLoading = false }
        do {
            let envelope = try await client.fetchRunsListAsync(query: query)
            guard !Task.isCancelled else { return }
            results = envelope
            lastFailureText = nil
        } catch let failure as EngineFailure {
            guard !Task.isCancelled else { return }
            // last successful page stays displayed beside the reason
            lastFailureText = failure.localizedDescription
        } catch {
            guard !Task.isCancelled else { return }
            lastFailureText = "查询历史失败：\(error)"
        }
    }

    // MARK: - project options

    /// Load the project picker's options (projects.list). Failure is
    /// non-fatal: the picker keeps whatever it had, with the reason.
    public func loadProjects() async {
        do {
            let envelope = try await client.fetchProjectsListAsync(limit: 200)
            guard !Task.isCancelled else { return }
            availableProjects = envelope.projects
            projectsFailureText = nil
        } catch let failure as EngineFailure {
            projectsFailureText = failure.localizedDescription
        } catch {
            projectsFailureText = "加载项目列表失败：\(error)"
        }
    }
}
