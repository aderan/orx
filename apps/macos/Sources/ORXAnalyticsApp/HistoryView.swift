import SwiftUI
import ORXAnalyticsCore

/// The history query window content (design §3 历史查询): cross-project
/// search over `runs.list` with project / goal-or-task title / Run or
/// Task number / status / model / time-range filters, time and
/// observed-consumption sorting, stable paging.
///
/// - All filtering, sorting and paging run ENGINE-SIDE; this view only
///   mirrors filter state and renders the returned page (no local
///   re-sort — the engine's identity secondary key is what keeps pages
///   stable).
/// - Every consumption number is rendered next to its coverage line —
///   mandatory under the consumption sort, so a run without usage data
///   can never look like the cheapest one (its usage shows "—" and the
///   row says it sorts last).
/// - The listed consumption is the run's ALL-REVISION total (旧计划消耗
///   没有消失); clicking a row opens the run detail where each historical
///   revision keeps its own ledger.
struct HistoryQueryView: View {
    @EnvironmentObject private var app: AppModel
    @ObservedObject private var history: HistoryModel

    init(history: HistoryModel) {
        self.history = history
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            filterBar
            Divider()
            resultSummary
            resultList
            paginationFooter
        }
        .padding(14)
        .frame(minWidth: 760, minHeight: 520)
        .onAppear {
            Task {
                await history.loadProjects()
                if history.results == nil {
                    await history.search()
                }
            }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("历史查询").font(.title2)
                Text("按项目、目标或任务标题、Run/Task 编号、状态、模型、时间范围查询全部执行；排序与分页由引擎执行。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if history.isLoading {
                ProgressView().controlSize(.small)
                    .help("正在异步查询（不阻塞界面）")
            }
            Button {
                Task {
                    await history.loadProjects()
                    await history.search()
                }
            } label: {
                Label("重新查询", systemImage: "arrow.clockwise")
            }
            .help("按当前过滤条件重新查询")
        }
    }

    // MARK: filters

    private var filterBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                filterField(label: "目标或任务标题", width: 200) {
                    TextField("关键词", text: $history.filter.text)
                        .onSubmit(runSearch)
                }
                filterField(label: "模型", width: 140) {
                    TextField("模型名", text: $history.filter.model)
                        .onSubmit(runSearch)
                }
                Picker("项目", selection: $history.filter.project) {
                    Text("全部项目").tag("")
                    ForEach(history.availableProjects) { project in
                        Text(project.displayName).tag(project.project)
                    }
                }
                .frame(width: 190)
                .onChange(of: history.filter.project) { _ in runSearch() }
                Picker("状态", selection: $history.filter.status) {
                    Text("全部状态").tag("")
                    ForEach(HistoryStatusOption.known) { option in
                        Text(option.label).tag(option.raw)
                    }
                }
                .frame(width: 120)
                .onChange(of: history.filter.status) { _ in runSearch() }
            }
            HStack(spacing: 10) {
                filterField(label: "Run 编号", width: 110) {
                    TextField("如 R001", text: $history.filter.runID)
                        .onSubmit(runSearch)
                }
                filterField(label: "Task 编号", width: 110) {
                    TextField("如 T002", text: $history.filter.taskID)
                        .onSubmit(runSearch)
                }
                Picker("时间范围", selection: $history.filter.timeRange) {
                    ForEach(HistoryTimeRange.allCases) { range in
                        Text(range.label).tag(range)
                    }
                }
                .frame(width: 140)
                .onChange(of: history.filter.timeRange) { _ in runSearch() }
                Picker("排序", selection: $history.filter.sortKey) {
                    ForEach(HistorySortKey.allCases) { key in
                        Text(key.label).tag(key)
                    }
                }
                .frame(width: 130)
                .onChange(of: history.filter.sortKey) { _ in runSearch() }
                Picker("顺序", selection: $history.filter.orderDesc) {
                    Text("降序").tag(true)
                    Text("升序").tag(false)
                }
                .pickerStyle(.segmented)
                .frame(width: 120)
                .onChange(of: history.filter.orderDesc) { _ in runSearch() }
                Button("查询", action: runSearch)
            }
            if let failure = history.projectsFailureText {
                Text("项目列表加载失败（可继续查询全部项目）：\(failure)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func runSearch() {
        Task { await history.search() }
    }

    private func filterField<Content: View>(label: String, width: CGFloat,
                                            @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            content().frame(width: width)
        }
    }

    // MARK: results

    private var resultSummary: some View {
        HStack(spacing: 10) {
            Text(history.rangeText).font(.caption)
            if let sort = history.results?.query?.sort {
                Text("排序：\(sortLabel(sort.key)) · \(sort.order == "desc" ? "降序" : "升序") · 稳定次级键 \(sort.secondary ?? "—")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let failure = history.lastFailureText {
                Text("上次查询失败（仍显示上次成功结果）：\(failure)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
    }

    private func sortLabel(_ key: String) -> String {
        HistorySortKey(rawValue: key)?.label ?? key
    }

    private var resultList: some View {
        List(history.rows) { row in
            HistoryRunRowView(row: row) {
                app.openRunDetail(project: row.run.project,
                                  runID: row.run.runID)
            }
        }
        .listStyle(.inset)
        .overlay {
            if history.rows.isEmpty && !history.isLoading {
                Text(history.results == nil ? "尚未查询" : "没有匹配的执行")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var paginationFooter: some View {
        HStack {
            Button("上一页") { Task { await history.previousPage() } }
                .disabled(!history.canPreviousPage)
            Button("下一页") { Task { await history.nextPage() } }
                .disabled(!history.canNextPage)
            Spacer()
            if let warnings = history.results?.warnings, !warnings.isEmpty {
                ForEach(Array(warnings.prefix(3).enumerated()),
                        id: \.offset) { _, warning in
                    Text("警告 \(warning.code)：\(warning.detail ?? "")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .font(.callout)
    }
}

/// One history result row: identity + status + time, and the consumption
/// line that ALWAYS carries its coverage (消耗排序同时展示覆盖程度).
private struct HistoryRunRowView: View {
    let row: HistoryRunRow
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("\(Self.projectName(row.run.project)) / \(row.run.goal?.objective ?? "（无目标记录）")")
                            .font(.callout)
                            .lineLimit(1)
                        Text(row.run.runID)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                        // G011 分层：控制层徽标与执行状态并列（已取消的历史
                        // 行保留其控制层可见）
                        if let control = row.goalControlText {
                            Text("目标\(control)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .help("Goal 控制状态（与执行状态分立）")
                        }
                        Text(row.run.status.display)
                            .font(.caption)
                            .foregroundStyle(row.run.status.known ? .primary : .secondary)
                    }
                    HStack(spacing: 10) {
                        // 消耗与覆盖程度同行展示；数字是全部版本汇总
                        Text(row.consumptionText)
                        Text(row.coverageText)
                        if let note = row.unknownNote {
                            Label(note, systemImage: "questionmark.circle")
                                .foregroundStyle(.secondary)
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(row.consumptionKnown ? .secondary : .primary)
                    HStack(spacing: 10) {
                        Text("任务 \(MetricsFormat.progressText(row.run.progress))（当前计划）")
                        if let started = row.run.startedAt {
                            Text("开始 \(DisplayTime.full(started) ?? started)")
                        } else if let field = row.run.timeBasis?.field {
                            Text("开始 —（按 \(field) 排序）")
                        } else {
                            Text("开始 —")
                        }
                        if let asOf = row.run.dataAsOf {
                            Text("数据更新 \(DisplayTime.short(asOf) ?? asOf)")
                        }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("打开执行详情（含各 Revision 的消耗账本）")
    }

    static func projectName(_ key: String) -> String {
        (key as NSString).lastPathComponent
    }
}
