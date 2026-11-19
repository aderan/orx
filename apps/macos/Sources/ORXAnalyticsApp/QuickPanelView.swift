import SwiftUI
import ORXAnalyticsCore

struct QuickPanelView: View {
    @EnvironmentObject private var model: AppModel
    @State private var searching: Bool
    @State private var query = ""
    @State private var confirmingClear = false

    let openRunWindow: () -> Void
    let openDiagnostics: () -> Void
    let openSettings: () -> Void
    let closePanel: () -> Void

    init(openRunWindow: @escaping () -> Void, openDiagnostics: @escaping () -> Void,
         openSettings: @escaping () -> Void, startSearching: Bool = false,
         closePanel: @escaping () -> Void = {}) {
        self.openRunWindow = openRunWindow
        self.openDiagnostics = openDiagnostics
        self.openSettings = openSettings
        self.closePanel = closePanel
        _searching = State(initialValue: startSearching)
    }

    var body: some View {
        let panel = model.cachedOverview.map { PanelModel.build($0.envelope) } ?? .empty
        VStack(spacing: 0) {
            header(panel)
            ORXRule()
            if searching {
                searchResults(panel)
            } else {
                VStack(alignment: .leading, spacing: ORXSpace.md) {
                    shortcuts
                    connectionNotice
                    if model.cachedOverview != nil {
                        runSections(panel)
                    }
                }
                .padding(ORXSpace.md)
            }
            ORXRule()
            footer
        }
        .font(ORXType.body)
        .foregroundStyle(ORXColor.text)
        .tint(ORXColor.accent)
        .frame(width: ORXLayout.panelWidth)
        .background(ORXColor.surface)
        .onAppear {
            model.bootstrap()
            model.startAutoRefresh()
        }
        .onDisappear { model.stopAutoRefresh() }
        .onExitCommand {
            if searching { closeSearch() }
            else { closePanel() }
        }
        .orxCacheConfirmation(isPresented: $confirmingClear, onConfirm: model.clearDisplayCache)
    }

    private func header(_ panel: PanelModel) -> some View {
        HStack(alignment: .center, spacing: ORXSpace.md) {
            VStack(alignment: .leading, spacing: ORXSpace.xs) {
                HStack(spacing: ORXSpace.sm) {
                    Text("ORX").font(ORXType.title)
                    if AppChannelInfo.from(infoDictionary: Bundle.main.infoDictionary ?? [:])?.isInternalPreview == true {
                        ORXBadge("Internal Preview")
                    }
                }
                Text(statusLine(panel))
                    .font(ORXType.caption)
                    .foregroundStyle(ORXColor.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            if model.isRefreshing {
                ProgressView().controlSize(.small).accessibilityLabel("正在刷新")
            }
            Button {
                Task { await model.refreshNow() }
            } label: {
                Label("刷新状态", systemImage: "arrow.clockwise").labelStyle(.iconOnly)
            }
            .buttonStyle(ORXButtonStyle(variant: .quiet))
            .disabled(model.isRefreshing || model.connection.isFatal)
            .help("刷新状态")

            Button {
                searching ? closeSearch() : openSearch()
            } label: {
                Label(searching ? "关闭搜索" : "搜索菜单", systemImage: searching ? "xmark" : "magnifyingglass")
                    .labelStyle(.iconOnly)
            }
            .buttonStyle(ORXButtonStyle())
            .keyboardShortcut("k", modifiers: .command)
            .help(searching ? "关闭搜索" : "搜索菜单 · ⌘K")
        }
        .padding(ORXSpace.md)
        .background(ORXColor.inset)
    }

    private func statusLine(_ panel: PanelModel) -> String {
        var parts: [String] = []
        if let cached = model.cachedOverview {
            parts.append("进行中 \(MetricsFormat.countText(cached.envelope.summary.activeRuns))")
            parts.append("需关注 \(MetricsFormat.countText(cached.envelope.summary.attention))")
            parts += panel.activeControlCounts.map { "\($0.label) \(MetricsFormat.countText($0.count))" }
            parts.append("缓存 \(cached.cachedAt.formatted(date: .omitted, time: .shortened))")
        }
        switch model.connection {
        case .idle: parts.append("未连接")
        case .connecting: parts.append("正在连接")
        case .ready(let version): parts.append("引擎 v\(version)")
        case .incompatible: parts.append("协议不兼容")
        case .notRunning: parts.append("引擎离线")
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var shortcuts: some View {
        if !model.settings.panelShortcuts.isEmpty {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: ORXSpace.xs), count: 3),
                      alignment: .leading, spacing: ORXSpace.xs) {
                ForEach(model.settings.panelShortcuts) { shortcut in
                    Button { perform(shortcut) } label: {
                        Label(shortcut.title, systemImage: shortcut.symbol)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(ORXButtonStyle())
                    .help(shortcut.title)
                }
            }
        }
    }

    @ViewBuilder
    private var connectionNotice: some View {
        if model.isResyncingAfterWake {
            ORXNotice(text: "唤醒后正在重新同步", symbol: "arrow.triangle.2.circlepath")
        }
        switch model.connection {
        case let .incompatible(requested, supported):
            ORXNotice(text: "协议不兼容 · App v\(requested)，引擎 v\(supported)。请升级应用或引擎。"
                      + cacheSuffix, tone: .warning, symbol: "exclamationmark.triangle")
        case .notRunning(let reason):
            ORXNotice(text: (model.lastFailureText ?? reason)
                      + (model.cachedOverview == nil ? " 当前没有可显示的缓存。" : ""),
                      tone: .warning, symbol: "bolt.slash")
        default:
            if let failure = model.lastFailureText {
                ORXNotice(text: failure + cacheSuffix, tone: .warning, symbol: "exclamationmark.triangle")
            } else if model.cachedOverview == nil {
                ORXNotice(text: "正在等待首次采集，尚无缓存", symbol: "arrow.clockwise")
            }
        }

        if model.showDiagnosticsEntry {
            Button(action: openDiagnostics) {
                Label("查看诊断 · 连续失败 \(MetricsFormat.countText(model.diagnostics.consecutiveFailures)) 次",
                      systemImage: "stethoscope")
            }
            .buttonStyle(ORXButtonStyle())
        }
    }

    private var cacheSuffix: String {
        model.cachedOverview == nil ? "" : " 正在显示上次成功的数据。"
    }

    @ViewBuilder
    private func runSections(_ panel: PanelModel) -> some View {
        let count = panel.needsAttention.count + panel.active.count + panel.recentCompleted.count
        if count > 0 {
            PanelScroll { sections(panel) }
        } else {
            sections(panel)
        }
    }

    private func sections(_ panel: PanelModel) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.md) {
            section("需要关注", symbol: "exclamationmark.triangle", cards: panel.needsAttention,
                    empty: "没有需要关注的执行", panel: panel, tone: .warning)
            section("进行中", symbol: "circle.dotted", cards: panel.active,
                    empty: "当前没有进行中的执行", panel: panel)
            section("最近完成", symbol: "checkmark.circle", cards: panel.recentCompleted,
                    empty: "最近没有完成的执行", panel: panel)
        }
    }

    private func section(_ title: String, symbol: String, cards: [PanelRunCard],
                         empty: String, panel: PanelModel, tone: ORXTone = .neutral) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            HStack(spacing: ORXSpace.sm) {
                Label(title, systemImage: symbol).font(ORXType.section)
                    .foregroundStyle(tone.ink).accessibilityAddTraits(.isHeader)
                Text(MetricsFormat.countText(cards.count)).font(ORXType.number)
                    .foregroundStyle(ORXColor.secondary)
            }
            if cards.isEmpty {
                Text(empty).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                    .padding(.leading, ORXSpace.xl)
            } else {
                ForEach(cards) { runCard($0, panel: panel) }
            }
        }
    }

    private func runCard(_ card: PanelRunCard, panel: PanelModel) -> some View {
        Button {
            model.openRunDetail(project: card.run.project, runID: card.run.runID)
            openRunWindow()
        } label: {
            VStack(alignment: .leading, spacing: ORXSpace.xs) {
                HStack(alignment: .firstTextBaseline, spacing: ORXSpace.xs) {
                    Text(Self.projectName(card.run.project)).font(ORXType.label)
                    Text(card.run.runID).font(ORXType.code).foregroundStyle(ORXColor.secondary)
                    Spacer(minLength: ORXSpace.xs)
                    ORXBadge(card.run.status.display, tone: .execution(card.run.status.raw))
                    Image(systemName: "chevron.right").font(ORXType.badge)
                        .foregroundStyle(ORXColor.secondary).accessibilityHidden(true)
                }
                Text(card.run.goal?.objective ?? card.run.runID).font(ORXType.section)
                    .lineLimit(2)
                if let badge = card.controlBadgeText {
                    ORXBadge(badge, tone: .warning, symbol: card.controlStatus?.raw == "paused"
                             ? "pause.circle" : "exclamationmark.circle")
                } else if let plain = card.controlPlainText {
                    Text(plain).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                }
                ForEach(card.attention) { item in
                    Label("\(Self.kindLabel(item.kind)) · \(item.detail)", systemImage: "exclamationmark.triangle")
                        .font(ORXType.caption)
                        .foregroundStyle(ORXColor.dangerText)
                        .lineLimit(2)
                }
                if let progress = card.run.progress, progress.total > 0,
                   card.run.status.raw != "done", card.run.status.raw != "completed" {
                    ProgressView(value: Double(max(0, min(progress.completed, progress.total))),
                                 total: Double(progress.total))
                        .accessibilityLabel("当前计划任务 \(MetricsFormat.progressText(progress))")
                }
                HStack(spacing: ORXSpace.sm) {
                    Text("当前计划 \(MetricsFormat.progressText(card.run.progress))")
                    Spacer(minLength: ORXSpace.xs)
                    Text("已观测 token \(MetricsFormat.tokenText(card.run.consumption?.observedTotal))")
                        .help("Run 全部计划版本的已观测用量")
                }
                .font(ORXType.caption.monospacedDigit())
                .foregroundStyle(ORXColor.secondary)
                if model.settings.panelDensity == .detailed, let consumption = card.run.consumption {
                    Text(MetricsFormat.coverageText(consumption.coverage))
                        .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                }
                healthLine(card, panel: panel)
            }
            .orxCard(padding: ORXSpace.sm, fill: .clear)
        }
        .buttonStyle(ORXRowButtonStyle())
        .help("打开 \(card.run.runID) 执行详情")
    }

    @ViewBuilder
    private func healthLine(_ card: PanelRunCard, panel: PanelModel) -> some View {
        let health = panel.health(for: card.run.project)?.health
        let time = card.run.dataAsOf.map(Self.compactTime)
        if health != nil || time != nil {
            HStack(spacing: ORXSpace.sm) {
                if let health { Label(health, systemImage: "stethoscope").help("数据健康") }
                if let time { Text("数据更新 \(time)") }
            }
            .font(ORXType.caption)
            .foregroundStyle(ORXColor.secondary)
        }
    }

    private var footer: some View {
        HStack(spacing: 0) {
            Button("查看全部", action: { perform(.history) })
            Button("模型与路由", action: { perform(.routing) })
            Spacer(minLength: 0)
            Button("设置", action: openSettings)
            Button("退出") { NSApp.terminate(nil) }
        }
        .buttonStyle(ORXButtonStyle(variant: .quiet))
        .padding(ORXSpace.xs)
        .background(ORXColor.inset)
    }

    private func searchResults(_ panel: PanelModel) -> some View {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let actions = PanelShortcut.allCases.filter {
            $0 != .search && (term.isEmpty || $0.title.localizedCaseInsensitiveContains(term)
                            || $0.rawValue.localizedCaseInsensitiveContains(term))
        }
        let cards = (panel.needsAttention + panel.active + panel.recentCompleted).filter {
            !term.isEmpty && [$0.run.project, $0.run.runID, $0.run.goal?.objective ?? ""]
                .contains { $0.localizedCaseInsensitiveContains(term) }
        }
        return VStack(alignment: .leading, spacing: ORXSpace.sm) {
            ORXTextInput(title: "搜索菜单", prompt: "搜索功能、项目或 Run", text: $query, autoFocus: true)
                .onSubmit {
                    if let action = actions.first { perform(action) }
                    else if let card = cards.first {
                        model.openRunDetail(project: card.run.project, runID: card.run.runID)
                        openRunWindow()
                    }
                }
            if actions.isEmpty && cards.isEmpty {
                Text("没有匹配的菜单或面板记录")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                    .padding(ORXSpace.md)
            } else {
                PanelScroll { matches(actions, cards: cards, panel: panel) }
            }
        }
        .padding(ORXSpace.md)
    }

    private func matches(_ actions: [PanelShortcut], cards: [PanelRunCard], panel: PanelModel) -> some View {
        VStack(spacing: ORXSpace.xs) {
            ForEach(actions) { action in
                Button { perform(action) } label: {
                    Label(action.title, systemImage: action.symbol)
                        .padding(ORXSpace.sm)
                }
                .buttonStyle(ORXRowButtonStyle())
            }
            ForEach(cards) { runCard($0, panel: panel) }
        }
    }

    private func openSearch() {
        searching = true
    }

    private func closeSearch() {
        searching = false
        query = ""
    }

    private func perform(_ action: PanelShortcut) {
        switch action {
        case .history:
            model.openAllRuns()
            openRunWindow()
        case .usage:
            model.openAllRuns(page: .usage)
            openRunWindow()
        case .routing:
            model.openRouting(project: nil)
            openRunWindow()
        case .diagnostics: openDiagnostics()
        case .search: openSearch()
        case .cache: confirmingClear = true
        }
    }

    static func projectName(_ path: String) -> String { (path as NSString).lastPathComponent }

    static func kindLabel(_ kind: String) -> String {
        switch kind {
        case "failed_task": return "任务失败"
        case "blocked_run": return "执行阻塞"
        default: return kind
        }
    }

    static func compactTime(_ iso: String) -> String { DisplayTime.short(iso) ?? iso }
}

// Give the hosting popover an explicit viewport; an unconstrained ScrollView's
// ideal height is only a single row in AppKit's preferred-content-size pass.
private struct PanelScroll<Content: View>: View {
    let content: Content
    @State private var contentHeight = ORXLayout.panelScrollHeight

    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        ScrollView {
            content.background {
                GeometryReader { geometry in
                    Color.clear.preference(key: PanelContentHeight.self, value: geometry.size.height)
                }
            }
        }
        .frame(height: min(contentHeight, ORXLayout.panelScrollHeight))
        .onPreferenceChange(PanelContentHeight.self) { height in
            if height > 0 { contentHeight = height }
        }
    }
}

private struct PanelContentHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

extension PanelShortcut {
    var title: String {
        switch self {
        case .history: return "执行历史"
        case .usage: return "用量分析"
        case .routing: return "模型与路由"
        case .diagnostics: return "来源诊断"
        case .search: return "搜索菜单"
        case .cache: return "清理缓存"
        }
    }

    var symbol: String {
        switch self {
        case .history: return "clock.arrow.circlepath"
        case .usage: return "chart.bar"
        case .routing: return "point.3.connected.trianglepath.dotted"
        case .diagnostics: return "stethoscope"
        case .search: return "magnifyingglass"
        case .cache: return "trash"
        }
    }
}
