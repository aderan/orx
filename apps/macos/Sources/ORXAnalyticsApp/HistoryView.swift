import SwiftUI
import ORXAnalyticsCore

/// Both entry points use the same engine query, filter state and results viewport.
struct HistoryQueryView: View {
    @EnvironmentObject private var app: AppModel
    @ObservedObject var history: HistoryModel
    let page: DesktopPage

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: ORXSpace.md) {
                header
                filterBar
                if let failure = history.projectsFailureText {
                    ORXNotice(text: "项目列表加载失败，仍可查询全部项目。\(failure)",
                              tone: .warning, symbol: "exclamationmark.triangle")
                }
                if let failure = history.lastFailureText, history.results != nil {
                    ORXNotice(text: "查询失败 · 正在显示上次成功的结果。\(failure)",
                              tone: .warning, symbol: "exclamationmark.triangle")
                }
            }
            .padding(ORXSpace.lg)
            .background(ORXColor.surface)
            ORXRule()
            results
            ORXRule()
            pagination
        }
        .onAppear {
            Task {
                await history.loadProjects()
                if history.results == nil {
                    await history.search()
                }
            }
        }
    }

    private var header: some View {
        ORXPageHeader(page == .usage ? "用量分析" : "执行历史",
                      detail: page == .usage
                        ? "选择一条 Run，查看全部计划版本的用量与原生会话预览。"
                        : "跨项目查找目标、任务与历史执行。") {
            if history.isLoading {
                ProgressView().controlSize(.small).accessibilityLabel("正在查询")
            }
            Button {
                Task {
                    await history.loadProjects()
                    await history.search()
                }
            } label: {
                Label("重新查询", systemImage: "arrow.clockwise")
            }
            .buttonStyle(ORXButtonStyle())
        }
    }

    private var filterBar: some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            HStack(alignment: .bottom, spacing: ORXSpace.md) {
                field("目标或任务标题", prompt: "搜索关键词", text: $history.filter.text)
                field("模型", prompt: "模型名", text: $history.filter.model)
                field("Run 编号", prompt: "R001", text: $history.filter.runID)
                    .frame(width: ORXLayout.historyIdentifierField)
                field("Task 编号", prompt: "T001", text: $history.filter.taskID)
                    .frame(width: ORXLayout.historyIdentifierField)
                Button("查询", action: runSearch)
                    .buttonStyle(ORXButtonStyle(variant: .primary))
                    .fixedSize()
            }
            HStack(alignment: .bottom, spacing: ORXSpace.md) {
                ORXField("项目") {
                    Picker("项目", selection: $history.filter.project) {
                        Text("全部项目").tag("")
                        ForEach(history.availableProjects) { project in
                            Text(project.displayName).tag(project.project)
                        }
                        if !history.filter.project.isEmpty,
                           !history.availableProjects.contains(where: { $0.project == history.filter.project }) {
                            Text(QuickPanelView.projectName(history.filter.project)).tag(history.filter.project)
                        }
                    }
                    .onChange(of: history.filter.project) { _ in runSearch() }
                }
                ORXField("状态") {
                    Picker("状态", selection: $history.filter.status) {
                        Text("全部状态").tag("")
                        ForEach(HistoryStatusOption.known) { Text($0.label).tag($0.raw) }
                    }
                    .onChange(of: history.filter.status) { _ in runSearch() }
                }
                ORXField("时间范围") {
                    Picker("时间范围", selection: $history.filter.timeRange) {
                        ForEach(HistoryTimeRange.allCases) { Text($0.label).tag($0) }
                    }
                    .onChange(of: history.filter.timeRange) { _ in runSearch() }
                }
                ORXField("排序") {
                    Picker("排序", selection: $history.filter.sortKey) {
                        ForEach(HistorySortKey.allCases) { Text($0.label).tag($0) }
                    }
                    .onChange(of: history.filter.sortKey) { _ in runSearch() }
                }
                ORXField("顺序") {
                    Picker("顺序", selection: $history.filter.orderDesc) {
                        Text("降序").tag(true)
                        Text("升序").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: history.filter.orderDesc) { _ in runSearch() }
                }
                .frame(width: ORXLayout.historyOrderField)
            }
            .labelsHidden()
        }
    }

    private func field(_ title: String, prompt: String, text: Binding<String>) -> some View {
        ORXField(title) {
            ORXTextInput(title: title, prompt: prompt, text: text)
                .onSubmit(runSearch)
        }
    }

    private var results: some View {
        VStack(spacing: 0) {
            HStack(spacing: ORXSpace.md) {
                Text(history.rangeText).font(ORXType.label)
                Spacer()
                if let sort = history.results?.query?.sort {
                    Text("\(HistorySortKey(rawValue: sort.key)?.label ?? sort.key) · \(sort.order == "desc" ? "降序" : "升序")")
                        .font(ORXType.caption)
                        .foregroundStyle(ORXColor.secondary)
                        .help(sort.secondary ?? "引擎稳定排序")
                }
            }
            .padding(.horizontal, ORXSpace.lg)
            .padding(.vertical, ORXSpace.sm)
            if history.rows.isEmpty {
                emptyResults
            } else {
                table
            }
        }
        .frame(maxHeight: .infinity)
    }

    @ViewBuilder
    private var emptyResults: some View {
        if history.isLoading {
            ORXEmptyState("正在查询执行", loading: true)
        } else if let failure = history.lastFailureText, history.results == nil {
            ORXEmptyState("查询失败", detail: failure, symbol: "exclamationmark.triangle") {
                Button("重试", action: runSearch).buttonStyle(ORXButtonStyle())
            }
        } else {
            ORXEmptyState(history.results == nil ? "尚未查询" : "没有匹配的执行",
                          detail: history.results == nil ? nil : "更改筛选条件后再次查询。",
                          symbol: "magnifyingglass")
        }
    }

    private var table: some View {
        VStack(spacing: 0) {
            HistoryColumns {
                Text("项目 / Run")
                Text("Goal 目标")
                Text("目标 / 执行状态")
                Text("当前计划")
                Text("已观测 token")
                Text("开始 / 更新")
            }
            .font(ORXType.caption)
            .foregroundStyle(ORXColor.secondary)
            .padding(.horizontal, ORXSpace.lg)
            .padding(.vertical, ORXSpace.sm)
            .background(ORXColor.inset)
            ORXRule()

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 0) {
                        Color.clear.frame(height: 0).id("history-top")
                        ForEach(history.rows) { row in
                            HistoryRunRowView(row: row) {
                                app.openRunDetail(project: row.run.project, runID: row.run.runID, page: page)
                            }
                            ORXRule().padding(.horizontal, ORXSpace.lg)
                        }
                    }
                }
                .onChange(of: history.results?.query) { _ in proxy.scrollTo("history-top", anchor: .top) }
                .onChange(of: history.results?.page?.offset) { _ in proxy.scrollTo("history-top", anchor: .top) }
            }
        }
    }

    private var pagination: some View {
        HStack(spacing: ORXSpace.sm) {
            if let warnings = history.results?.warnings {
                ForEach(Array(warnings.prefix(3).enumerated()), id: \.offset) { _, warning in
                    let text = "\(warning.code) · \(warning.detail ?? "—")"
                    Label(text, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(ORXColor.warningText)
                        .font(ORXType.caption)
                        .lineLimit(1)
                        .help(text)
                }
            }
            Spacer()
            Button {
                Task { await history.previousPage() }
            } label: {
                Label("上一页", systemImage: "chevron.left")
            }
            .disabled(!history.canPreviousPage || history.isLoading)
            Button {
                Task { await history.nextPage() }
            } label: {
                Label("下一页", systemImage: "chevron.right")
            }
            .disabled(!history.canNextPage || history.isLoading)
        }
        .buttonStyle(ORXButtonStyle())
        .padding(ORXSpace.md)
        .background(ORXColor.surface)
    }

    private func runSearch() { Task { await history.search() } }
}

/// Header and rows use the same grid, keeping observed usage and coverage together.
private struct HistoryColumns<Project: View, Goal: View, Status: View, Progress: View, Usage: View, Time: View>: View {
    let project: Project
    let goal: Goal
    let status: Status
    let progress: Progress
    let usage: Usage
    let time: Time

    init(@ViewBuilder _ columns: () -> TupleView<(Project, Goal, Status, Progress, Usage, Time)>) {
        (project, goal, status, progress, usage, time) = columns().value
    }

    var body: some View {
        HStack(alignment: .top, spacing: ORXSpace.md) {
            project.frame(width: ORXLayout.historyProjectColumn, alignment: .leading)
            goal.frame(maxWidth: .infinity, alignment: .leading).layoutPriority(1)
            status.frame(width: ORXLayout.historyStatusColumn, alignment: .leading)
            progress.frame(width: ORXLayout.historyProgressColumn, alignment: .leading)
            usage.frame(width: ORXLayout.historyUsageColumn, alignment: .leading)
            time.frame(width: ORXLayout.historyTimeColumn, alignment: .leading)
        }
    }
}

private struct HistoryRunRowView: View {
    let row: HistoryRunRow
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HistoryColumns {
                VStack(alignment: .leading, spacing: ORXSpace.xs) {
                    Text(QuickPanelView.projectName(row.run.project)).font(ORXType.section).lineLimit(1)
                    Text(row.run.runID).font(ORXType.code).foregroundStyle(ORXColor.secondary)
                }
                Text(row.run.goal?.objective ?? "无目标记录").font(ORXType.body).lineLimit(2)
                VStack(alignment: .leading, spacing: ORXSpace.xs) {
                    ORXBadge(row.run.status.display, tone: .execution(row.run.status.raw))
                    if let control = row.goalControlText {
                        Text("目标 \(control)").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                    }
                }
                VStack(alignment: .leading, spacing: ORXSpace.xs) {
                    Text(MetricsFormat.progressText(row.run.progress)).font(ORXType.number)
                    Text("当前版本").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                }
                VStack(alignment: .leading, spacing: ORXSpace.xs) {
                    Text(MetricsFormat.tokenText(row.run.consumption?.observedTotal))
                        .font(ORXType.number)
                        .help(row.consumptionText)
                    Text(row.coverageText).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                    if let note = row.unknownNote {
                        Text("用量未知").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                            .help(note).accessibilityLabel(note)
                    }
                }
                HStack(alignment: .top, spacing: ORXSpace.xs) {
                    VStack(alignment: .leading, spacing: ORXSpace.xs) {
                        Text(row.run.startedAt.flatMap(DisplayTime.full) ?? "—")
                        if row.run.startedAt == nil, let field = row.run.timeBasis?.field {
                            Text("时序依据 \(field)")
                        }
                        if let asOf = row.run.dataAsOf {
                            Text("更新 \(DisplayTime.short(asOf) ?? asOf)")
                        }
                    }
                    .font(ORXType.caption.monospacedDigit()).foregroundStyle(ORXColor.secondary)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(ORXType.badge)
                        .foregroundStyle(ORXColor.secondary).accessibilityHidden(true)
                }
            }
            .padding(.horizontal, ORXSpace.lg)
            .padding(.vertical, ORXSpace.md)
        }
        .buttonStyle(ORXRowButtonStyle())
        .help("打开 \(row.run.runID) 执行详情及各计划版本用量")
    }
}
