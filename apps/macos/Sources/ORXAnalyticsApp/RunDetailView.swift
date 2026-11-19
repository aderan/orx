import SwiftUI
import ORXAnalyticsCore

/// Keeps the history viewport mounted so back navigation preserves its scroll position.
struct RunDetailHostView: View {
    @EnvironmentObject private var model: AppModel
    @State private var historyWasShown = false

    let openSettings: () -> Void
    var openDiagnostics: () -> Void = {}

    private var showsHistory: Bool { model.mainPage != .routing && model.selectedRun == nil }

    var body: some View {
        VStack(spacing: 0) {
            navigation
            ORXRule()
            ZStack {
                if showsHistory || historyWasShown {
                    HistoryQueryView(history: model.historyModel, page: model.mainPage)
                        .opacity(showsHistory ? 1 : 0)
                        .allowsHitTesting(showsHistory)
                        .disabled(!showsHistory)
                        .accessibilityHidden(!showsHistory)
                }
                if model.mainPage == .routing {
                    RoutingView()
                } else if let selection = model.selectedRun {
                    RunDetailView(identity: selection, client: model.client, page: model.mainPage)
                        .id(selection)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .orxSurface()
        .frame(minWidth: ORXLayout.windowMinWidth, minHeight: ORXLayout.windowMinHeight)
        .onAppear { historyWasShown = showsHistory }
        .onChange(of: showsHistory) { shown in
            if shown { historyWasShown = true }
        }
    }

    private var navigation: some View {
        HStack(spacing: ORXSpace.lg) {
            Label("ORX", systemImage: "circle.grid.2x2").font(ORXType.title)
            Picker("当前页面", selection: $model.mainPage) {
                Text("执行历史").tag(DesktopPage.execution)
                Text("用量分析").tag(DesktopPage.usage)
                Text("模型与路由").tag(DesktopPage.routing)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            Spacer(minLength: ORXSpace.md)
            Menu {
                Button("设置…", action: openSettings)
                Button("来源诊断…", action: openDiagnostics)
                Button("退出 ORX") { NSApp.terminate(nil) }
            } label: {
                Label("应用选项", systemImage: "gearshape").labelStyle(.iconOnly)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("应用选项")
        }
        .padding(.horizontal, ORXSpace.lg)
        .padding(.vertical, ORXSpace.sm)
        .background(ORXColor.inset)
    }
}

/// The run detail window: four zones (design §3/§4/§5).
///
/// 1. 执行概况 — identity, three INDEPENDENT status dimensions
///    (execution status / attention / data health), timing, and the
///    data-as-of stamps.
/// 2. 任务列表 — tasks grouped by revision, history COLLAPSED by
///    default; current-plan progress and historical-plan consumption
///    stay separate expressions.
/// 3. 消耗分析 — the two calibers side by side (never added), role
///    ledgers, missing explanations, model distribution.
/// 4. 尝试与证据 — the selected task's attempts (result / usage /
///    session link / failure reason / source), verifications, retry.
struct RunDetailView: View {
    @EnvironmentObject private var app: AppModel
    @StateObject private var model: RunDetailModel
    let identity: RunIdentity
    let page: DesktopPage

    /// Revision rows the user expanded (default: none — Revision 默认收起).
    @State private var expandedRevisions: Set<Int> = []

    init(identity: RunIdentity, client: EngineClientProtocol, page: DesktopPage) {
        self.identity = identity
        self.page = page
        _model = StateObject(wrappedValue: RunDetailModel(client: client,
                                                          identity: identity))
    }

    var body: some View {
        Group {
            if let envelope = model.detail, envelope.found, let run = envelope.run {
                GeometryReader { geometry in
                    VStack(spacing: 0) {
                        toolbar(run: run)
                        ORXRule()
                        ScrollView {
                            VStack(alignment: .leading, spacing: ORXSpace.lg) {
                                if let failure = model.lastFailureText {
                                    ORXNotice(text: "刷新失败 · 正在显示上次成功的数据。\(failure)",
                                              tone: .warning, symbol: "exclamationmark.triangle")
                                }
                                if page == .usage {
                                    consumptionZone(envelope)
                                    taskListZone(envelope, run)
                                    evidenceZone(envelope)
                                } else {
                                    factsZone(envelope, run)
                                    executionWorkspace(envelope, run, wide: geometry.size.width >= ORXLayout.splitBreakpoint)
                                }
                            }
                            .padding(ORXSpace.lg)
                        }
                    }
                }
            } else if let envelope = model.detail, !envelope.found {
                notFound(envelope)
            } else {
                loading
            }
        }
        .orxSurface()
        .onAppear {
            Task { await model.load() }
            // G012: the detail window follows the app's SHARED refresh
            // preference (default 30s) — no independent detail-side cadence.
            model.startAutoRefresh(interval: app.settings.panelRefreshInterval)
        }
        .onDisappear { model.stopAutoRefresh() }
        // Revision 默认收起，当前计划例外：任务列表的默认内容就是
        // 当前计划任务（设计 §3）；历史计划保持收起，调查重规划时展开。
        .onReceive(model.$detail.compactMap { $0 }.prefix(1)) { envelope in
            if let current = envelope.run?.progress?.revisionID {
                expandedRevisions.insert(current)
            }
        }
    }

    @ViewBuilder
    private func executionWorkspace(_ envelope: RunDetailEnvelope, _ run: RunFacts, wide: Bool) -> some View {
        let layout = wide
            ? AnyLayout(HStackLayout(alignment: .top, spacing: ORXSpace.lg))
            : AnyLayout(VStackLayout(alignment: .leading, spacing: ORXSpace.lg))
        layout {
            taskListZone(envelope, run)
                .frame(width: wide ? ORXLayout.taskColumn : nil)
            evidenceZone(envelope)
        }
    }

    // MARK: Toolbar

    private func toolbar(run: RunFacts) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            HStack(spacing: ORXSpace.sm) {
                Button {
                    app.openAllRuns(page: page)
                } label: {
                    Label("历史查询", systemImage: "chevron.left")
                }
                .buttonStyle(ORXButtonStyle())
                .help("返回并保留历史查询条件、当前页和滚动位置")
                Text("\(QuickPanelView.projectName(run.project)) / \(run.runID)")
                    .font(ORXType.code)
                    .foregroundStyle(ORXColor.secondary)
                Spacer()
                if model.isLoading { ProgressView().controlSize(.small).accessibilityLabel("正在刷新") }
                if page == .execution {
                    Button {
                        app.mainPage = .usage
                    } label: {
                        Label("此 Run 用量", systemImage: "chart.bar")
                    }
                    .buttonStyle(ORXButtonStyle())
                }
                Button {
                    app.openRouting(project: run.project)
                } label: {
                    Label("项目路由", systemImage: "point.3.connected.trianglepath.dotted")
                }
                .buttonStyle(ORXButtonStyle())
                Button {
                    Task { await model.refresh() }
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                }
                .buttonStyle(ORXButtonStyle())
            }
            ORXPageHeader(page == .usage ? "用量分析" : (run.goal?.objective ?? run.runID),
                          detail: page == .usage
                            ? "\(run.goal?.objective ?? run.runID) · 全部计划版本"
                            : run.project)
        }
        .padding(ORXSpace.lg)
        .background(ORXColor.surface)
    }

    @ViewBuilder
    private var loading: some View {
        if let failure = model.lastFailureText {
            ORXEmptyState("执行详情加载失败", detail: failure, symbol: "exclamationmark.triangle") {
                Button("重试") { Task { await model.load() } }
                    .buttonStyle(ORXButtonStyle())
                Button("返回历史") { app.openAllRuns(page: page) }
                    .buttonStyle(ORXButtonStyle(variant: .quiet))
            }
        } else {
            ORXEmptyState("正在加载执行详情", loading: true)
        }
    }

    private func notFound(_ envelope: RunDetailEnvelope) -> some View {
        ORXEmptyState("未找到该执行", detail: envelope.reason ?? model.lastFailureText ?? "原因未知",
                      symbol: "questionmark.square.dashed") {
            Button("返回历史") { app.openAllRuns(page: page) }
                .buttonStyle(ORXButtonStyle())
        }
    }

    // MARK: zone 1 — 执行概况

    private func factsZone(_ envelope: RunDetailEnvelope, _ run: RunFacts) -> some View {
        zone("执行概况") {
            let dims = RunDimensions.build(
                run: run, tasks: envelope.tasks,
                health: app.cachedOverview
                    .flatMap { PanelModel.build($0.envelope).health(for: run.project) },
                snapshots: envelope.snapshots, project: run.project)
            HStack(spacing: ORXSpace.sm) {
                dimensionChip("执行状态", dims.executionStatus, tone: .execution(run.status.raw))
                dimensionChip("Goal 控制态", dims.goalControl, tone: run.goal?.status?.raw == "paused" ? .warning : .neutral)
                dimensionChip("关注事项", dims.attentionSummary)
                dimensionChip("数据健康", dims.dataHealth)
            }
            Grid(alignment: .topLeading, horizontalSpacing: ORXSpace.xl, verticalSpacing: ORXSpace.sm) {
                GridRow {
                    fact("目标状态", run.goal?.status.map { "\($0.display) · raw \($0.raw ?? "—")" } ?? "—")
                    fact("开始时间", DisplayTime.full(run.startedAt) ?? run.startedAt ?? "—")
                    fact("结束时间", DisplayTime.full(run.completedAt) ?? run.completedAt ?? "—")
                    fact("经过时长", DisplayTime.elapsed(from: run.startedAt, to: run.completedAt) ?? "—")
                }
                GridRow {
                    fact("数据更新时间", DisplayTime.full(run.dataAsOf) ?? run.dataAsOf ?? "—")
                    if let snap = envelope.snapshots[run.project] {
                        fact("数据来源", "快照 #\(snap.snapshotID.map(String.init) ?? "—") · schema \(snap.schemaVersion ?? "—")"
                             + (snap.supported == false ? "（不支持）" : ""))
                        fact("快照拍摄时间", DisplayTime.full(snap.takenAt) ?? snap.takenAt ?? "—")
                    }
                    if let field = run.timeBasis?.field, field != "started_at" {
                        fact("时间排序依据", field)
                    }
                }
            }

        }
    }

    private func dimensionChip(_ label: String, _ value: String, tone: ORXTone = .neutral) -> some View {
        ORXBadge("\(label) · \(value)", tone: tone)
    }

    private func fact(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text(label).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            Text(value).font(ORXType.body.monospacedDigit()).textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    // MARK: zone 2 — 任务列表（Revision 默认收起）

    private func taskListZone(_ envelope: RunDetailEnvelope, _ run: RunFacts) -> some View {
        zone("任务列表") {
            let currentRevision = run.progress?.revisionID
            let byRevision = Dictionary(grouping: envelope.tasks, by: \.revisionID)
            let ordered = envelope.revisions.sorted {
                ($0.revision ?? 0, $0.revisionID) < ($1.revision ?? 0, $1.revisionID)
            }
            if ordered.isEmpty {
                Text("暂无计划任务").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            ForEach(ordered) { revision in
                let tasks = (byRevision[revision.revisionID] ?? [])
                    .sorted { $0.taskID < $1.taskID }
                let isCurrent = revision.revisionID == currentRevision
                let revConsumption = envelope.usage?.perRevision?
                    .first { $0.revisionID == revision.revisionID }
                DisclosureGroup(isExpanded: binding(for: revision.revisionID)) {
                    VStack(alignment: .leading, spacing: ORXSpace.xs) {
                        ForEach(tasks) { task in
                            taskRow(task, isCurrent: isCurrent)
                        }
                    }
                    .padding(.top, ORXSpace.xxs)
                } label: {
                    VStack(alignment: .leading, spacing: ORXSpace.xxs) {
                        HStack(spacing: ORXSpace.xs) {
                            Text("Revision \(revision.revision.map(String.init) ?? "—")")
                                .font(isCurrent ? ORXType.section : ORXType.body)
                            if isCurrent {
                                ORXBadge("当前计划")
                            } else {
                                Text("历史计划")
                                    .font(ORXType.caption)
                                    .foregroundStyle(ORXColor.secondary)
                            }
                            Text("\(MetricsFormat.countText(revision.taskCount)) 任务")
                                .font(ORXType.caption)
                                .foregroundStyle(ORXColor.secondary)
                        }
                        // 当前计划显示任务进度；历史计划只显示其已归账消耗
                        // （进度与消耗分开表达，§3）。
                        if isCurrent {
                            Text("任务 \(MetricsFormat.progressText(run.progress))（已完成/当前计划任务数）")
                                .font(ORXType.caption)
                                .foregroundStyle(ORXColor.secondary)
                        } else if let book = revConsumption?.book {
                            Text("该版本已观测 token \(MetricsFormat.tokenText(book.tokens?.inputTokens)) 输入 / \(MetricsFormat.tokenText(book.tokens?.outputTokens)) 输出（保留计入 Run 汇总，不因重规划丢失）")
                                .font(ORXType.caption)
                                .foregroundStyle(ORXColor.secondary)
                        } else {
                            Text("该版本消耗：—")
                                .font(ORXType.caption)
                                .foregroundStyle(ORXColor.secondary)
                        }
                    }
                }
            }
        }
    }

    private func binding(for revisionID: Int) -> Binding<Bool> {
        Binding(
            get: { expandedRevisions.contains(revisionID) },
            set: { expanded in
                if expanded {
                    expandedRevisions.insert(revisionID)
                } else {
                    expandedRevisions.remove(revisionID)
                }
            })
    }

    private func taskRow(_ task: TaskListItem, isCurrent: Bool) -> some View {
        Button {
            Task { await model.select(task: task) }
        } label: {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: ORXSpace.xxs) {
                    HStack(spacing: ORXSpace.xs) {
                        Text(task.objective ?? task.taskID)
                            .font(ORXType.body)
                            .lineLimit(2)
                        Spacer(minLength: 0)
                        ORXBadge(task.status.display, tone: .execution(task.status.raw))
                        if model.selectedTask?.id == task.id {
                            Image(systemName: "arrow.turn.down.right")
                                .font(ORXType.caption)
                                .foregroundStyle(ORXColor.secondary)
                        }
                    }
                    VStack(alignment: .leading, spacing: ORXSpace.xxs) {
                        Text("尝试 \(MetricsFormat.countText(task.attemptCount)) · \(MetricsFormat.coverageText(task.consumption?.coverage))")
                        Text("已观测 token \(MetricsFormat.tokenText(task.consumption?.observedTotal))")
                            .help("ORX 已记录用量")
                    }
                    .font(ORXType.caption.monospacedDigit())
                    .foregroundStyle(ORXColor.secondary)
                    if let failure = task.failureReason, !failure.isEmpty {
                        Label("异常提示：\(failure)", systemImage: "exclamationmark.triangle")
                            .font(ORXType.caption)
                            .foregroundStyle(ORXColor.warningText)
                    }
                }
                Spacer()
            }
            .padding(ORXSpace.sm)
        }
        .buttonStyle(ORXRowButtonStyle(selected: model.selectedTask?.id == task.id))
        .help("查看该任务的尝试、验证与用量")
    }

    // MARK: zone 3 — 消耗分析（两口径并列，不相加）

    private func consumptionZone(_ envelope: RunDetailEnvelope) -> some View {
        zone("用量分析") {
            Button("查看此项目的模型与路由") {
                app.openRouting(project: identity.project)
            }
            .buttonStyle(ORXButtonStyle(variant: .quiet))
            // 两个口径并排：ORX 已记录用量 / 原生会话预览。
            // 结构上不相加——两组行各自来自引擎，没有混合总数。
            let calibers = DetailCalibers(usage: envelope.usage,
                                          preview: envelope.nativePreview)
            HStack(alignment: .top, spacing: ORXSpace.md) {
                caliberBox(title: "ORX 已记录用量（主口径）",
                           subtitle: "来源快照 \(envelope.usage?.asOf ?? "—")",
                           rows: calibers.orxRows)
                caliberBox(title: "原生会话预览（独立口径，与左列不相加）",
                           subtitle: nativeSubtitle(envelope.nativePreview),
                           rows: calibers.nativeRows)
            }
            ORXNotice(text: "口径独立 · 两组数字可能覆盖相同 token，禁止相加。", symbol: "equal.circle")

            controllerPreviewSection(envelope.nativePreview)

            ORXRule()

            // 角色分账（engine 归属账本：任务/planner/controller/未归属）
            Text("角色分账（engine 归属，未按任务数或时间摊分）")
                .font(ORXType.section)
            let books = envelope.usage?.books
            bookRow("任务账本（worker＋verifier 等可归属尝试）", books?.tasks)
            bookRow("规划开销（planner，归 Run）", books?.planner)
            bookRow("controller（单列）", books?.controller)
            bookRow("未归属（Run 内，绝不摊分）", books?.unattributedWithinRun)
            if let rule = envelope.usage?.attribution?.rule {
                Text("归属规则：\(rule)")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }

            ORXRule()

            // 覆盖与缺失说明
            Text("覆盖与缺失说明").font(ORXType.section)
            keyValue("用量覆盖", MetricsFormat.coverageText(envelope.usage?.coverage))
            keyValue("字段缺失", MetricsFormat.missingText(envelope.usage?.missingFields))
            if let accuracy = envelope.usage?.accuracy {
                keyValue("准确度分布",
                         "exact \(MetricsFormat.countText(accuracy["exact"] ?? 0)) · estimated \(MetricsFormat.countText(accuracy["estimated"] ?? 0)) · unknown \(MetricsFormat.countText(accuracy["unknown"] ?? 0))")
            }
            if let normalized = envelope.usage?.normalizedProcessing {
                keyValue("标准化处理量（v\(normalized.version)，派生指标，与原始 token 分开）",
                         MetricsFormat.tokenText(normalized.value)
                         + MetricsFormat.partialSuffix(normalized))
            }

            ORXRule()

            // 模型分布：按尝试计数（不在此层重算 token 归属）
            Text("模型分布").font(ORXType.section)
            if let selected = model.selectedTask,
               let attempts = model.taskDetail?.usage?.attempts {
                let dist = ModelDistribution(attempts: attempts)
                Text("当前所选任务 \(selected.taskID)（跨模型 token 分账见各角色账本与尝试列表）")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
                ForEach(dist.entries) { entry in
                    HStack {
                        Text(entry.model)
                        Spacer()
                        Text("尝试 \(MetricsFormat.countText(entry.attempts)) 次")
                    }
                    .font(ORXType.body)
                }
            } else {
                Text("在任务列表选择任务后，按该任务的尝试显示模型分布。")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
        }
    }

    private func nativeSubtitle(_ preview: NativePreview?) -> String {
        guard let preview else { return "无关联原生会话" }
        let sessions = preview.union?.sessions?.count ?? 0
        if sessions == 0 { return "无关联原生会话" }
        var text = "会话 \(MetricsFormat.countText(sessions)) 个"
        if let ms = preview.asOfSessionsMS {
            text += " · 更新 \(msTimestamp(ms))"
        }
        return text
    }

    /// Epoch-ms stamp the session mirrors carry → local "MM-dd HH:mm:ss".
    private func msTimestamp(_ ms: Int) -> String {
        let date = Date(timeIntervalSince1970: TimeInterval(ms) / 1000)
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm:ss"
        return f.string(from: date)
    }

    // MARK: controller 会话（原生预览 · model_usage 请求口径）

    /// G012 用户反馈 5：controller 自身 token。只读镜像 zcode
    /// model_usage（请求口径，按 session/logical_request/attempt 去重，
    /// 缓存为 input 子集，处理量 = input＋output），按唯一会话归属证据
    /// 显示在本 run 名下；无法唯一归属时显示原因，绝不补零，绝不与
    /// ORX 口径相加。
    private func controllerPreviewSection(_ preview: NativePreview?) -> some View {
        let p = ControllerUsagePresentation.build(preview: preview)
        return VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text("controller 会话（原生预览 · model_usage 请求口径，独立显示）")
                .font(ORXType.section)
            keyValue("归属状态", p.statusText)
            if let session = p.sessionText {
                keyValue("controller 会话", session)
            }
            if let reason = p.reasonText, !reason.isEmpty {
                Text("原因：\(reason)")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            ForEach(p.lines) { line in
                HStack {
                    Text(line.label).font(ORXType.caption)
                    Spacer(minLength: ORXSpace.sm)
                    Text(line.text).font(ORXType.number)
                }
            }
            if let shared = p.sharedCaveatText {
                Text(shared)
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.warningText)
            }
            if let source = p.sourceText {
                keyValue("来源", source)
            }
            if p.hasSessionUsage {
                keyValue("更新时间", p.updatedMS.map { msTimestamp($0) } ?? "—")
                if let last = p.usageLastActivityMS {
                    keyValue("最近请求", msTimestamp(last))
                }
            }
            if let directory = p.directoryText {
                keyValue("会话目录", directory)
            }
            if let evidence = p.evidenceText {
                Text("关联依据：\(evidence)")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            if let caveat = p.caveatText {
                Text("数据说明：\(caveat)")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            if let note = p.noteText, !note.isEmpty {
                Text(note)
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            Text("该数字属于原生预览口径（controller 自身请求，不含 worker/verifier 后代，不叠加 turn_usage），与左侧 ORX 已记录口径不相加。")
                .font(ORXType.caption)
                .foregroundStyle(ORXColor.secondary)
        }
        .padding(ORXSpace.md)
        .background(ORXColor.inset, in: RoundedRectangle(cornerRadius: ORXRadius.row))
    }

    private func caliberBox(title: String, subtitle: String,
                            rows: [DetailCalibers.Row]) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text(title).font(ORXType.section)
            Text(subtitle).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            ForEach(rows) { row in
                HStack {
                    Text(row.label).font(ORXType.caption)
                    Spacer()
                    Text(row.text).font(ORXType.number)
                }
            }
        }
        .padding(ORXSpace.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(ORXColor.inset, in: RoundedRectangle(cornerRadius: ORXRadius.row))
    }

    private func bookRow(_ label: String, _ book: UsageBook?) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xxs) {
            HStack {
                Text(label).font(ORXType.body)
                Spacer()
                Text("输入 \(MetricsFormat.tokenText(book?.tokens?.inputTokens)) · 输出 \(MetricsFormat.tokenText(book?.tokens?.outputTokens)) · 缓存 \(MetricsFormat.tokenText(book?.tokens?.cachedTokens))")
                    .font(ORXType.caption.monospacedDigit())
            }
            HStack(spacing: ORXSpace.sm) {
                Text(MetricsFormat.coverageText(book?.coverage))
                if let note = book?.note {
                    Text(note).lineLimit(1)
                }
            }
            .font(ORXType.caption)
            .foregroundStyle(ORXColor.secondary)
        }
        .padding(.vertical, ORXSpace.xxs)
    }

    // MARK: zone 4 — 尝试与证据

    private func evidenceZone(_ envelope: RunDetailEnvelope) -> some View {
        zone("尝试与证据") {
            if model.selectedTask != nil {
                Button("查看此项目的模型与路由") {
                    app.openRouting(project: identity.project)
                }
                .buttonStyle(ORXButtonStyle(variant: .quiet))
            }
            if let selected = model.selectedTask {
                if let taskDetail = model.taskDetail, taskDetail.found {
                    taskBreakdown(selected, taskDetail)
                } else if model.isTaskLoading {
                    HStack(spacing: ORXSpace.xs) {
                        ProgressView().controlSize(.small)
                        Text("正在加载任务 \(selected.taskID) 的尝试与证据…")
                            .foregroundStyle(ORXColor.secondary)
                    }
                } else {
                    Text("任务 \(selected.taskID) 的详情加载失败：\(model.taskFailureText ?? "原因未知")")
                        .font(ORXType.caption)
                        .foregroundStyle(ORXColor.secondary)
                }
            } else {
                Text("在任务列表点击任务，查看其每次尝试的结果、用量、会话关联、失败原因与来源。")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
        }
    }

    @ViewBuilder
    private func taskBreakdown(_ selected: SelectedTaskRef,
                               _ taskDetail: TaskDetailEnvelope) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            HStack(spacing: ORXSpace.sm) {
                Text("任务 \(selected.taskID)：\(selected.objective ?? "—")")
                    .font(ORXType.section)
                Text("重试：\(RetryDisplay.text(taskDetail.retry))")
                    .font(ORXType.caption)
                    .padding(.horizontal, ORXSpace.sm).padding(.vertical, ORXSpace.xxs)
                    .background(ORXColor.inset, in: RoundedRectangle(cornerRadius: ORXRadius.badge))
                if let status = taskDetail.task?.status?.display {
                    Text("状态：\(status)").font(ORXType.caption)
                        .foregroundStyle(ORXColor.secondary)
                }
            }
            if let failure = taskDetail.task?.failureReason, !failure.isEmpty {
                ORXNotice(text: "失败原因：\(failure)", tone: .danger, symbol: "exclamationmark.triangle")
            }

            // 任务消耗分解：worker / verifier 分账 + 缺失说明
            Text("任务消耗分解（ORX 已记录）").font(ORXType.section)
            bookRow("worker（含失败与重试）", taskDetail.usage?.books?.worker)
            bookRow("verifier（计入任务，单列）", taskDetail.usage?.books?.verifier)
            if let other = taskDetail.usage?.books?.otherRoles {
                bookRow("其他角色（不计入任务总数，待契约定义）", other)
            }
            keyValue("任务用量覆盖", MetricsFormat.coverageText(taskDetail.usage?.coverage))
            keyValue("字段缺失", MetricsFormat.missingText(taskDetail.usage?.missingFields))

            ORXRule()

            // 尝试列表：结果/用量/会话关联/失败原因/来源
            Text("尝试（每次 Attempt 的结果、用量、会话关联、失败原因和来源）")
                .font(ORXType.section)
            let attempts = taskDetail.usage?.attempts ?? []
            if attempts.isEmpty {
                Text("无尝试记录").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            ForEach(attempts) { attempt in
                attemptRow(attempt)
            }

            ORXRule()

            // 验证证据
            Text("验证（verifier 运行记录）").font(ORXType.section)
            if taskDetail.verifications.isEmpty {
                Text("无验证记录").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            ForEach(taskDetail.verifications) { verification in
                HStack(spacing: ORXSpace.sm) {
                    Text(verification.kind ?? "—").font(ORXType.caption)
                    Text(verification.command ?? "—")
                        .font(ORXType.code)
                        .lineLimit(1)
                    Spacer()
                    Text("退出码 \(verification.exitCode.map(String.init) ?? "—")")
                    Text(verification.passed == true ? "通过" :
                            (verification.passed == false ? "未通过" : "—"))
                        .foregroundStyle(verification.passed == false ? ORXColor.dangerText : ORXColor.secondary)
                    Text(DisplayTime.short(verification.createdAt) ?? verification.createdAt ?? "—")
                        .foregroundStyle(ORXColor.secondary)
                }
                .font(ORXType.caption)
            }
        }
    }

    private func attemptRow(_ attempt: AttemptDetail) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xxs) {
            HStack(spacing: ORXSpace.sm) {
                Text("#\(attempt.attemptID) \(attempt.role ?? "—")")
                    .font(ORXType.label)
                Text(attempt.result ?? "进行中")
                    .font(ORXType.caption)
                    .foregroundStyle(attempt.result == "failed" ? ORXColor.dangerText : ORXColor.secondary)
                if attempt.routing == nil {
                    Text(attempt.model ?? "模型未知").font(ORXType.caption)
                        .foregroundStyle(ORXColor.secondary)
                }
                Spacer()
                Text("\(DisplayTime.short(attempt.startedAt) ?? attempt.startedAt ?? "—") → \(DisplayTime.short(attempt.endedAt) ?? attempt.endedAt ?? "进行中")")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            HStack(spacing: ORXSpace.sm) {
                // 单次尝试的规范观测（每 attempt 取一，不同来源不相加）
                if let observation = attempt.observation {
                    Text("来源 \(observation.source ?? "—")（\(observation.accuracy ?? "—")）")
                    Text("输入 \(MetricsFormat.tokenText(observation.tokens?.inputTokens)) · 输出 \(MetricsFormat.tokenText(observation.tokens?.outputTokens)) · 缓存 \(MetricsFormat.tokenText(observation.tokens?.cachedTokens))")
                        .font(ORXType.caption.monospacedDigit())
                } else {
                    Text("用量未观测\(attempt.usageMissingReason.map { "（\($0)）" } ?? "（原因未记录）")")
                        .foregroundStyle(ORXColor.secondary)
                }
            }
            .font(ORXType.caption)
            if let routing = attempt.routing {
                recordedDispatch(routing, attempt: attempt)
            }
            if let link = attempt.sessionLink {
                Text("会话关联：\(link.sessionID ?? "—")（\(link.authority ?? "—") · \(link.confidence ?? "—")）")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
            }
            if let failure = attempt.failureReason, !failure.isEmpty {
                Text("失败原因：\(failure)")
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.warningText)
            }
        }
        .padding(.vertical, ORXSpace.xxs)
    }

    private func recordedDispatch(_ routing: AttemptRouting, attempt: AttemptDetail) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text("当次派发").font(ORXType.section)
            ORXValueRow("Controller", routing.controllerHarness ?? "未记录", monospaced: true)
            let profile = routing.profile.map { "\($0)\(routing.fallbackUsed == true ? " · 回退" : "")" }
            let selection = [profile, RoutingLabels.execution(driver: attempt.driver, harness: attempt.harness)]
                .compactMap { $0 }.joined(separator: "  ·  ")
            ORXValueRow("执行配置", selection.isEmpty ? "未记录" : selection)
            ORXValueRow("请求模型", "\(routing.requestedModel ?? "未记录") · \(routing.requestedEffort ?? "推理未记录")")
            ORXValueRow("实际模型", "\(routing.actualModel ?? "未记录") · \(recordedEffort(routing))")
            if let reason = routing.reason { Text("选中原因  \(RoutingLabels.reason(reason))") }
            if let candidates = routing.candidates, !candidates.isEmpty {
                DisclosureGroup("当次候选与过滤结果") {
                    ForEach(Array(candidates.enumerated()), id: \.offset) { _, candidate in
                        let outcome = candidate.rejectReason.map(RoutingLabels.reason)
                            ?? (candidate.kept ? "通过筛选" : "已过滤")
                        Text([candidate.profile, RoutingLabels.tier(candidate.tier, legacyClass: candidate.legacyClass),
                              candidate.resourceStatus, outcome].compactMap { $0 }.joined(separator: "  ·  "))
                    }
                }
            }
        }
        .font(ORXType.caption)
        .textSelection(.enabled)
    }

    private func recordedEffort(_ routing: AttemptRouting) -> String {
        guard let value = routing.actualEffort else { return "未记录" }
        if value == "provider_default" || routing.effortSource == "provider_default" {
            return "供应商默认（档位未观测）"
        }
        switch routing.effortSource {
        case "reported": return "\(value)（工具报告）"
        case "requested_validated": return "\(value)（请求参数已校验）"
        default: return value
        }
    }

    // MARK: shared

    private func zone<Content: View>(_ title: String,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.md) {
            Text(title).font(ORXType.title).accessibilityAddTraits(.isHeader)
            content()
        }
        .orxCard()
    }

    private func keyValue(_ label: String, _ value: String) -> some View {
        ORXValueRow(label, value).textSelection(.enabled)
    }
}
