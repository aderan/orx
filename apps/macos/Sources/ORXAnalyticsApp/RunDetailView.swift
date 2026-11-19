import SwiftUI
import ORXAnalyticsCore

/// Host of the "执行详情" window: the selected run's four-zone detail,
/// or the history query (查看全部执行) when no run is selected. The
/// HistoryModel lives on the AppModel, so filters and the current page
/// survive navigating into a detail and back.
struct RunDetailHostView: View {
    @EnvironmentObject private var model: AppModel
    /// The hosting NSWindow, captured once SwiftUI mounts the content —
    /// lets the host front ITS OWN window precisely (G012 用户反馈 2).
    @State private var hostWindow: NSWindow?

    var body: some View {
        Group {
            if let selection = model.selectedRun {
                RunDetailView(identity: selection, client: model.client)
                    .id(selection)
            } else {
                HistoryQueryView(history: model.historyModel)
            }
        }
        .background(WindowAccessor(window: $hostWindow))
        // 首次打开（窗口出现）与每次选中项变化（连续点击不同项目 /
        // 通知深链）都把窗口前置并取得键盘焦点；面板点击路径
        // (DetailWindowRouting) 同样前置，两条路径互为保障。
        .onAppear { DetailWindowRouting.focus(hostWindow) }
        .onChange(of: model.selectedRun) { _ in
            DetailWindowRouting.focus(hostWindow)
        }
    }
}

/// Bridges the SwiftUI view into its hosting NSWindow (nil until the
/// window materializes; capture is async off the first layout pass).
private struct WindowAccessor: NSViewRepresentable {
    @Binding var window: NSWindow?

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { self.window = view.window }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
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

    /// Revision rows the user expanded (default: none — Revision 默认收起).
    @State private var expandedRevisions: Set<Int> = []

    init(identity: RunIdentity, client: EngineClientProtocol) {
        self.identity = identity
        _model = StateObject(wrappedValue: RunDetailModel(client: client,
                                                          identity: identity))
    }

    var body: some View {
        Group {
            if let envelope = model.detail, envelope.found, let run = envelope.run {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        toolbar(run: run)
                        factsZone(envelope, run)
                        Divider()
                        taskListZone(envelope, run)
                        Divider()
                        consumptionZone(envelope)
                        Divider()
                        evidenceZone(envelope)
                    }
                    .padding(16)
                }
            } else if let envelope = model.detail, !envelope.found {
                notFound(envelope)
            } else {
                loading
            }
        }
        .frame(minWidth: 660, minHeight: 560)
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

    // MARK: toolbar

    private func toolbar(run: RunFacts) -> some View {
        HStack(alignment: .firstTextBaseline) {
            // Back to the history query that led here (filters and page
            // are kept); closing the window still keeps the app resident.
            Button {
                app.openAllRuns()
            } label: {
                Label("历史查询", systemImage: "chevron.backward")
            }
            .help("返回历史查询（保留过滤条件与结果页）")
            VStack(alignment: .leading, spacing: 2) {
                Text("\(QuickPanelView.projectName(run.project)) / \(run.goal?.objective ?? run.runID)")
                    .font(.title3)
                Text("\(run.project) · Run \(run.runID)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.isLoading {
                ProgressView().controlSize(.small)
                    .help("正在异步刷新（不阻塞界面）")
            }
            Button {
                Task { await model.refresh() }
            } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .help("异步刷新（不阻塞界面）")
        }
    }

    private var loading: some View {
        VStack(spacing: 10) {
            ProgressView()
            Text("正在加载执行详情…").foregroundStyle(.secondary)
            if let failure = model.lastFailureText {
                Text(failure).font(.caption).foregroundStyle(.secondary)
            }
            if model.detail == nil, model.lastFailureText != nil {
                Button("重试") { Task { await model.load() } }
            }
        }
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func notFound(_ envelope: RunDetailEnvelope) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "questionmark.square.dashed")
                .font(.largeTitle)
            Text("未找到该执行").font(.title3)
            Text(envelope.reason ?? "原因未知")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let failure = model.lastFailureText {
                Text(failure).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: zone 1 — 执行概况

    private func factsZone(_ envelope: RunDetailEnvelope, _ run: RunFacts) -> some View {
        zone("执行概况") {
            let dims = RunDimensions.build(
                run: run, tasks: envelope.tasks,
                health: app.cachedOverview
                    .flatMap { PanelModel.build($0.envelope).health(for: run.project) },
                snapshots: envelope.snapshots, project: run.project)
            HStack(spacing: 8) {
                dimensionChip("执行状态", dims.executionStatus)
                dimensionChip("目标控制", dims.goalControl)
                dimensionChip("关注事项", dims.attentionSummary)
                dimensionChip("数据健康", dims.dataHealth)
            }
            keyValue("项目", run.project)
            keyValue("Run", run.runID)
            if let objective = run.goal?.objective {
                keyValue("目标", objective)
            }
            // G011 分层：目标控制层（goals.status 原文的展示）与 Run 执行
            // 事实分立呈现——暂停/取消中是控制态，执行状态不被改写
            if let control = run.goal?.status {
                keyValue("目标状态",
                         "\(control.display)（raw: \(control.raw ?? "—")）")
            }
            keyValue("开始时间", DisplayTime.full(run.startedAt) ?? run.startedAt ?? "—")
            keyValue("结束时间", DisplayTime.full(run.completedAt) ?? run.completedAt ?? "—")
            keyValue("经过时长", DisplayTime.elapsed(from: run.startedAt,
                                                     to: run.completedAt) ?? "—")
            // 数据更新时间：来源快照拍摄时刻（§4：与任务进展时间分立）
            keyValue("数据更新时间", DisplayTime.full(run.dataAsOf) ?? run.dataAsOf ?? "—")
            if let snap = envelope.snapshots[run.project] {
                keyValue("数据来源", "快照 #\(snap.snapshotID.map(String.init) ?? "—")"
                         + " · schema \(snap.schemaVersion ?? "—")"
                         + (snap.supported == false ? "（版本不支持）" : ""))
                if let taken = DisplayTime.full(snap.takenAt) {
                    keyValue("快照拍摄时间", taken)
                }
            }
            if let field = run.timeBasis?.field, field != "started_at" {
                keyValue("时间依据", "该执行无 started_at，按 \(field) 排序与显示")
            }
            if let failure = model.lastFailureText {
                Text("最近一次刷新失败（仍显示上次成功数据）：\(failure)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// One independent dimension chip. Text always carries the meaning —
    /// color is decoration, never the only signal (design §4).
    private func dimensionChip(_ label: String, _ value: String) -> some View {
        HStack(spacing: 4) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.weight(.semibold))
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 4)
                    .fill(Color.primary.opacity(0.08)))
        }
        .help("\(label)：\(value)")
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
                Text("暂无计划任务").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(ordered) { revision in
                let tasks = (byRevision[revision.revisionID] ?? [])
                    .sorted { $0.taskID < $1.taskID }
                let isCurrent = revision.revisionID == currentRevision
                let revConsumption = envelope.usage?.perRevision?
                    .first { $0.revisionID == revision.revisionID }
                DisclosureGroup(isExpanded: binding(for: revision.revisionID)) {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(tasks) { task in
                            taskRow(task, isCurrent: isCurrent)
                        }
                    }
                    .padding(.top, 2)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text("Revision \(revision.revision.map(String.init) ?? "—")")
                                .font(.callout.weight(isCurrent ? .semibold : .regular))
                            if isCurrent {
                                Text("当前计划")
                                    .font(.caption2)
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(RoundedRectangle(cornerRadius: 3)
                                        .fill(Color.accentColor.opacity(0.18)))
                            } else {
                                Text("历史计划")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            Text("\(MetricsFormat.countText(revision.taskCount)) 任务")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        // 当前计划显示任务进度；历史计划只显示其已归账消耗
                        // （进度与消耗分开表达，§3）。
                        if isCurrent {
                            Text("任务 \(MetricsFormat.progressText(run.progress))（已完成/当前计划任务数）")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else if let book = revConsumption?.book {
                            Text("该版本已观测 token \(MetricsFormat.tokenText(book.tokens?.inputTokens)) 输入 / \(MetricsFormat.tokenText(book.tokens?.outputTokens)) 输出（保留计入 Run 汇总，不因重规划丢失）")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            Text("该版本消耗：—")
                                .font(.caption)
                                .foregroundStyle(.secondary)
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
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(task.objective ?? task.taskID)
                            .font(.callout)
                            .lineLimit(1)
                        Text(task.status.display)
                            .font(.caption)
                            .foregroundStyle(task.status.known ? .primary : .secondary)
                        if model.selectedTask?.id == task.id {
                            Image(systemName: "arrow.turn.down.right")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    HStack(spacing: 8) {
                        Text("执行次数 \(MetricsFormat.countText(task.attemptCount))")
                        Text("已观测 token \(MetricsFormat.tokenText(task.consumption?.observedTotal))（ORX 已记录）")
                        Text(MetricsFormat.coverageText(task.consumption?.coverage))
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    if let failure = task.failureReason, !failure.isEmpty {
                        Label("异常提示：\(failure)", systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
                Spacer()
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("查看该任务的尝试、验证与用量")
    }

    // MARK: zone 3 — 消耗分析（两口径并列，不相加）

    private func consumptionZone(_ envelope: RunDetailEnvelope) -> some View {
        zone("消耗分析") {
            // 两个口径并排：ORX 已记录用量 / 原生会话预览。
            // 结构上不相加——两组行各自来自引擎，没有混合总数。
            let calibers = DetailCalibers(usage: envelope.usage,
                                          preview: envelope.nativePreview)
            HStack(alignment: .top, spacing: 12) {
                caliberBox(title: "ORX 已记录用量（主口径）",
                           subtitle: "来源快照 \(envelope.usage?.asOf ?? "—")",
                           rows: calibers.orxRows)
                caliberBox(title: "原生会话预览（独立口径，与左列不相加）",
                           subtitle: nativeSubtitle(envelope.nativePreview),
                           rows: calibers.nativeRows)
            }
            if let preview = envelope.nativePreview, preview.neverSumWith != nil {
                Text("说明：两口径可能描述同一批 token，任何视图都不将其相加（engine 标记 never_sum_with = \(preview.neverSumWith ?? "—")）。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            controllerPreviewSection(envelope.nativePreview)

            Divider()

            // 角色分账（engine 归属账本：任务/planner/controller/未归属）
            Text("角色分账（engine 归属，未按任务数或时间摊分）")
                .font(.subheadline.weight(.semibold))
            let books = envelope.usage?.books
            bookRow("任务账本（worker＋verifier 等可归属尝试）", books?.tasks)
            bookRow("规划开销（planner，归 Run）", books?.planner)
            bookRow("controller（单列）", books?.controller)
            bookRow("未归属（Run 内，绝不摊分）", books?.unattributedWithinRun)
            if let rule = envelope.usage?.attribution?.rule {
                Text("归属规则：\(rule)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Divider()

            // 覆盖与缺失说明
            Text("覆盖与缺失说明").font(.subheadline.weight(.semibold))
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

            Divider()

            // 模型分布：按尝试计数（不在此层重算 token 归属）
            Text("模型分布").font(.subheadline.weight(.semibold))
            if let selected = model.selectedTask,
               let attempts = model.taskDetail?.usage?.attempts {
                let dist = ModelDistribution(attempts: attempts)
                Text("当前所选任务 \(selected.taskID)（跨模型 token 分账见各角色账本与尝试列表）")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(dist.entries) { entry in
                    HStack {
                        Text(entry.model)
                        Spacer()
                        Text("尝试 \(MetricsFormat.countText(entry.attempts)) 次")
                    }
                    .font(.callout)
                }
            } else {
                Text("在任务列表选择任务后，按该任务的尝试显示模型分布。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
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
        return VStack(alignment: .leading, spacing: 4) {
            Text("controller 会话（原生预览 · model_usage 请求口径，独立显示）")
                .font(.subheadline.weight(.semibold))
            keyValue("归属状态", p.statusText)
            if let session = p.sessionText {
                keyValue("controller 会话", session)
            }
            if let reason = p.reasonText, !reason.isEmpty {
                Text("原因：\(reason)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(p.lines) { line in
                HStack {
                    Text(line.label).font(.caption)
                    Spacer(minLength: 8)
                    Text(line.text).font(.callout.monospacedDigit())
                }
            }
            if let shared = p.sharedCaveatText {
                Text(shared)
                    .font(.caption2)
                    .foregroundStyle(.orange)
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
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let caveat = p.caveatText {
                Text("数据说明：\(caveat)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let note = p.noteText, !note.isEmpty {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Text("该数字属于原生预览口径（controller 自身请求，不含 worker/verifier 后代，不叠加 turn_usage），与左侧 ORX 已记录口径不相加。")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8)
            .fill(Color.primary.opacity(0.04)))
    }

    private func caliberBox(title: String, subtitle: String,
                            rows: [DetailCalibers.Row]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.callout.weight(.semibold))
            Text(subtitle).font(.caption2).foregroundStyle(.secondary)
            ForEach(rows) { row in
                HStack {
                    Text(row.label).font(.caption)
                    Spacer()
                    Text(row.text).font(.callout.monospacedDigit())
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8)
            .fill(Color.primary.opacity(0.04)))
    }

    private func bookRow(_ label: String, _ book: UsageBook?) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack {
                Text(label).font(.callout)
                Spacer()
                Text("输入 \(MetricsFormat.tokenText(book?.tokens?.inputTokens)) · 输出 \(MetricsFormat.tokenText(book?.tokens?.outputTokens)) · 缓存 \(MetricsFormat.tokenText(book?.tokens?.cachedTokens))")
                    .font(.caption.monospacedDigit())
            }
            HStack(spacing: 8) {
                Text(MetricsFormat.coverageText(book?.coverage))
                if let note = book?.note {
                    Text(note).lineLimit(1)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 1)
    }

    // MARK: zone 4 — 尝试与证据

    private func evidenceZone(_ envelope: RunDetailEnvelope) -> some View {
        zone("尝试与证据") {
            if let selected = model.selectedTask {
                if let taskDetail = model.taskDetail, taskDetail.found {
                    taskBreakdown(selected, taskDetail)
                } else if model.isTaskLoading {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("正在加载任务 \(selected.taskID) 的尝试与证据…")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("任务 \(selected.taskID) 的详情加载失败：\(model.taskFailureText ?? "原因未知")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("在任务列表点击任务，查看其每次尝试的结果、用量、会话关联、失败原因与来源。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func taskBreakdown(_ selected: SelectedTaskRef,
                               _ taskDetail: TaskDetailEnvelope) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("任务 \(selected.taskID)：\(selected.objective ?? "—")")
                    .font(.callout.weight(.semibold))
                Text("重试：\(RetryDisplay.text(taskDetail.retry))")
                    .font(.caption)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: 4)
                        .fill(Color.primary.opacity(0.08)))
                if let status = taskDetail.task?.status?.display {
                    Text("状态：\(status)").font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if let failure = taskDetail.task?.failureReason, !failure.isEmpty {
                Label("失败原因：\(failure)", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            // 任务消耗分解：worker / verifier 分账 + 缺失说明
            Text("任务消耗分解（ORX 已记录）").font(.subheadline.weight(.semibold))
            bookRow("worker（含失败与重试）", taskDetail.usage?.books?.worker)
            bookRow("verifier（计入任务，单列）", taskDetail.usage?.books?.verifier)
            if let other = taskDetail.usage?.books?.otherRoles {
                bookRow("其他角色（不计入任务总数，待契约定义）", other)
            }
            keyValue("任务用量覆盖", MetricsFormat.coverageText(taskDetail.usage?.coverage))
            keyValue("字段缺失", MetricsFormat.missingText(taskDetail.usage?.missingFields))

            Divider()

            // 尝试列表：结果/用量/会话关联/失败原因/来源
            Text("尝试（每次 Attempt 的结果、用量、会话关联、失败原因和来源）")
                .font(.subheadline.weight(.semibold))
            let attempts = taskDetail.usage?.attempts ?? []
            if attempts.isEmpty {
                Text("无尝试记录").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(attempts) { attempt in
                attemptRow(attempt)
            }

            Divider()

            // 验证证据
            Text("验证（verifier 运行记录）").font(.subheadline.weight(.semibold))
            if taskDetail.verifications.isEmpty {
                Text("无验证记录").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(taskDetail.verifications) { verification in
                HStack(spacing: 8) {
                    Text(verification.kind ?? "—").font(.caption)
                    Text(verification.command ?? "—")
                        .font(.system(.caption, design: .monospaced))
                        .lineLimit(1)
                    Spacer()
                    Text("退出码 \(verification.exitCode.map(String.init) ?? "—")")
                    Text(verification.passed == true ? "通过" :
                            (verification.passed == false ? "未通过" : "—"))
                        .foregroundStyle(verification.passed == false ? .orange : .secondary)
                    Text(DisplayTime.short(verification.createdAt) ?? verification.createdAt ?? "—")
                        .foregroundStyle(.secondary)
                }
                .font(.caption)
            }
        }
    }

    private func attemptRow(_ attempt: AttemptDetail) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text("#\(attempt.attemptID) \(attempt.role ?? "—")")
                    .font(.callout.weight(.medium))
                Text(attempt.result ?? "进行中")
                    .font(.caption)
                    .foregroundStyle(attempt.result == "failed" ? .orange : .secondary)
                Text(attempt.model ?? "模型 —").font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(DisplayTime.short(attempt.startedAt) ?? attempt.startedAt ?? "—") → \(DisplayTime.short(attempt.endedAt) ?? attempt.endedAt ?? "进行中")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                // 单次尝试的规范观测（每 attempt 取一，不同来源不相加）
                if let observation = attempt.observation {
                    Text("来源 \(observation.source ?? "—")（\(observation.accuracy ?? "—")）")
                    Text("输入 \(MetricsFormat.tokenText(observation.tokens?.inputTokens)) · 输出 \(MetricsFormat.tokenText(observation.tokens?.outputTokens)) · 缓存 \(MetricsFormat.tokenText(observation.tokens?.cachedTokens))")
                        .font(.caption.monospacedDigit())
                } else {
                    Text("用量未观测\(attempt.usageMissingReason.map { "（\($0)）" } ?? "（原因未记录）")")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.caption)
            if let link = attempt.sessionLink {
                Text("会话关联：\(link.sessionID ?? "—")（\(link.authority ?? "—") · \(link.confidence ?? "—")）")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let failure = attempt.failureReason, !failure.isEmpty {
                Text("失败原因：\(failure)")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 2)
    }

    // MARK: shared

    private func zone<Content: View>(_ title: String,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func keyValue(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value).font(.callout).textSelection(.enabled)
        }
    }
}
