import Foundation
import Combine

/// Stable identity of one run: Project + run_id (design §2). Short ids
/// are never a cross-project key.
public struct RunIdentity: Equatable, Hashable, Codable {
    public let project: String
    public let runID: String

    public init(project: String, runID: String) {
        self.project = project
        self.runID = runID
    }

    public var id: String { "\(project)#\(runID)" }
}

/// Stable identity of a selected task inside a run:
/// Project+Run+Revision+task_id.
public struct SelectedTaskRef: Equatable, Identifiable {
    public let revisionID: Int
    public let taskID: String
    public let objective: String?

    public init(revisionID: Int, taskID: String, objective: String?) {
        self.revisionID = revisionID
        self.taskID = taskID
        self.objective = objective
    }

    public var id: String { "\(revisionID)#\(taskID)" }
}

/// Loads and holds one run's detail (runs.get) plus the selected task's
/// drill-down (tasks.get). Behavior contracts (design §3/§8):
///
/// - Loads are asynchronous and run OFF the main actor (the client's
///   async wrappers hop to the engine IO queue) — the window never
///   blocks on a fetch, and a fetch in flight never freezes the panel.
/// - A failed load keeps the last successful data and publishes the
///   failure reason next to it (never a blank window).
/// - Concurrent loads coalesce into the in-flight one.
/// - The two calibers (`usage`, `nativePreview`) are fetched together
///   but exposed as separate properties — nothing here can add them.
@MainActor
public final class RunDetailModel: ObservableObject {
    @Published public private(set) var detail: RunDetailEnvelope?
    @Published public private(set) var isLoading = false
    @Published public private(set) var lastFailureText: String?
    @Published public private(set) var lastLoadedAt: Date?

    @Published public private(set) var selectedTask: SelectedTaskRef?
    @Published public private(set) var taskDetail: TaskDetailEnvelope?
    @Published public private(set) var isTaskLoading = false
    @Published public private(set) var taskFailureText: String?

    public let identity: RunIdentity
    public let client: EngineClientProtocol

    private var loadTask: Task<Void, Never>?
    private var taskLoadTask: Task<Void, Never>?
    private var autoRefreshTask: Task<Void, Never>?

    public init(client: EngineClientProtocol, identity: RunIdentity) {
        self.client = client
        self.identity = identity
    }

    // MARK: - run detail

    public func load() async {
        if let existing = loadTask, !existing.isCancelled {
            _ = await existing.value
            return
        }
        let task = Task { [weak self] () -> Void in
            guard let self else { return }
            await self.runLoad()
        }
        loadTask = task
        _ = await task.value
        loadTask = nil
    }

    public func refresh() async {
        await load()
    }

    private func runLoad() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let envelope = try await client.fetchRunDetailAsync(
                project: identity.project, runID: identity.runID)
            detail = envelope
            lastLoadedAt = Date()
            lastFailureText = nil
            // keep the drill-down of the selected task in step with the
            // refreshed run data
            if let selected = selectedTask {
                await reloadTaskDetail(selected)
            }
        } catch let failure as EngineFailure {
            // last successful detail stays displayed
            lastFailureText = failure.localizedDescription
        } catch {
            lastFailureText = "加载执行详情失败：\(error)"
        }
    }

    // MARK: - task drill-down

    public func select(task: TaskListItem) async {
        let ref = SelectedTaskRef(revisionID: task.revisionID,
                                  taskID: task.taskID,
                                  objective: task.objective)
        selectedTask = ref
        await loadTaskDetail(ref)
    }

    private func reloadTaskDetail(_ ref: SelectedTaskRef) async {
        await loadTaskDetail(ref)
    }

    private func loadTaskDetail(_ ref: SelectedTaskRef) async {
        if let existing = taskLoadTask, !existing.isCancelled {
            _ = await existing.value
        }
        let task = Task { [weak self] () -> Void in
            guard let self else { return }
            await self.runTaskLoad(ref)
        }
        taskLoadTask = task
        _ = await task.value
        taskLoadTask = nil
    }

    private func runTaskLoad(_ ref: SelectedTaskRef) async {
        isTaskLoading = true
        defer { isTaskLoading = false }
        do {
            taskDetail = try await client.fetchTaskDetailAsync(
                project: identity.project, runID: identity.runID,
                revisionID: ref.revisionID, taskID: ref.taskID)
            taskFailureText = nil
        } catch let failure as EngineFailure {
            // last successful task detail stays displayed
            taskFailureText = failure.localizedDescription
        } catch {
            taskFailureText = "加载任务详情失败：\(error)"
        }
    }

    // MARK: - auto refresh (detail-open; cadence provided by the caller)

    /// While the detail window is open the app re-reads the read model on
    /// the cadence the caller provides — the app's SHARED refresh
    /// preference (default 30s, G012 用户反馈 4). There is deliberately NO
    /// detail-side default: the detail window never runs its own faster
    /// hardcoded loop, and its loads are pure reads (no `refresh.request`),
    /// so panel + detail open together never duplicate collection.
    public func startAutoRefresh(interval: TimeInterval) {
        stopAutoRefresh()
        autoRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self?.load()
            }
        }
    }

    public func stopAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
    }
}
