import SwiftUI
import ORXAnalyticsCore

/// The menu-bar quick panel (design §3): header with status + manual
/// refresh, then 需要关注 / 进行中 / 最近完成 (attention first), then
/// footer actions.
///
/// - The three sections come from `PanelModel` — 同一 Run 只出现一次，
///   异常优先排序 (the attention section renders first).
/// - Cards show 项目/目标, attention lines, "任务 a/b（当前计划）" and
///   "已观测 token N（全部版本）" as SEPARATE expressions, plus the
///   project's data-health tag (third dimension, design §4).
/// - Clicking a card sets the app's detail selection (project + run
///   context preserved) and opens the detail window.
/// - Cache-first: the panel renders whatever `model.cachedOverview`
///   holds the moment it opens and refreshes asynchronously — the open
///   path never waits for a collection round to finish.
struct QuickPanelView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            statusBanner
            Divider()
            let panel = currentPanel
            attentionSection(panel)
            activeSection(panel)
            recentSection(panel)
            Spacer(minLength: 0)
            footer
        }
        .padding(12)
        .onAppear { model.bootstrap() ; model.startAutoRefresh() }
        .onDisappear { model.stopAutoRefresh() }
    }

    private var currentPanel: PanelModel {
        model.cachedOverview.map { PanelModel.build($0.envelope) } ?? .empty
    }

    // MARK: header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text("ORX Analytics").font(.headline)
                    if model.isRefreshing {
                        ProgressView()
                            .controlSize(.small)
                            .help("正在异步刷新（不阻塞显示）")
                    }
                }
                Text(statusLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                Task { await model.refreshNow() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("刷新状态")
        }
    }

    private var statusLine: String {
        var parts: [String] = []
        if let cached = model.cachedOverview {
            parts.append("进行中 \(MetricsFormat.countText(cached.envelope.summary.activeRuns))")
            // 控制层概览：进行中的执行里有多少处于已暂停/取消中控制态
            // （G011 分层——两个维度并列，不改写执行事实）
            let panel = PanelModel.build(cached.envelope)
            let control = panel.activeControlCounts
                .map { "\($0.label) \(MetricsFormat.countText($0.count))" }
                .joined(separator: "、")
            if !control.isEmpty {
                parts.append("（\(control)）")
            }
            parts.append("需要关注 \(MetricsFormat.countText(cached.envelope.summary.attention))")
        }
        switch model.connection {
        case .idle: parts.append("未连接")
        case .connecting: parts.append("连接中")
        case let .ready(version): parts.append("引擎协议 v\(version)")
        case let .incompatible(requested, supported):
            parts.append("协议不兼容：App v\(requested) / 引擎 v\(supported)")
        case .notRunning(let why): parts.append(why)
        }
        if let cached = model.cachedOverview {
            parts.append("缓存于 \(Self.shortTime(cached.cachedAt))")
        }
        return parts.joined(separator: " · ")
    }

    /// Engine/data status without touching run facts: a failed round
    /// shows its reason beside the still-displayed cache (design §8).
    @ViewBuilder
    private var statusBanner: some View {
        if model.cachedOverview == nil {
            switch model.connection {
            case let .incompatible(requested, supported):
                VStack(alignment: .leading, spacing: 2) {
                    Text("引擎协议版本不兼容")
                        .font(.subheadline.weight(.semibold))
                    Text("本应用使用协议 v\(requested)，引擎支持 v\(supported)。已停止查询，不强行接管引擎。请升级应用或引擎后重试。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .notRunning(let why):
                Text("\(why) 首次使用请先在终端启动引擎（python -m orxanalytics.engine）。当前没有可显示的缓存。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            default:
                Text("暂无缓存数据，正在异步刷新…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        // 睡眠唤醒后的重新同步状态：旧数据继续显示，标注为重新同步
        // 而不是异常（design §8: 睡眠期间不判定停摆）。
        if model.isResyncingAfterWake {
            Label("刚从睡眠唤醒，正在重新同步…", systemImage: "arrow.triangle.2.circlepath")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        if let failure = model.lastFailureText {
            Text(failure).font(.caption2).foregroundStyle(.secondary)
        }
        diagnosticsEntry
    }

    /// 引擎重复失败显示诊断入口 (design §8). Ordinary failures are
    /// shown as text; the ENTRY appears only when failures repeat or the
    /// protocol is fatally incompatible — with or without cached data.
    @ViewBuilder
    private var diagnosticsEntry: some View {
        if model.showDiagnosticsEntry {
            Button {
                openWindow(id: "diagnostics")
            } label: {
                Label("引擎连续失败——打开诊断…", systemImage: "stethoscope")
                    .font(.caption)
            }
            .buttonStyle(.link)
        }
    }

    private static func shortTime(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .medium
        return f.string(from: date)
    }

    // MARK: sections (needsAttention first — 异常优先排序)

    private func attentionSection(_ panel: PanelModel) -> some View {
        section(title: "需要关注（\(MetricsFormat.countText(panel.needsAttention.count)) 个执行）") {
            if panel.needsAttention.isEmpty {
                Text("没有需要关注的执行").font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(panel.needsAttention) { card in
                    runCard(card, showAttention: true, panel: panel)
                }
            }
        }
    }

    private func activeSection(_ panel: PanelModel) -> some View {
        section(title: "进行中（\(MetricsFormat.countText(panel.active.count)) 个执行）") {
            if panel.active.isEmpty {
                Text("当前没有进行中的执行").font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(panel.active) { card in
                    runCard(card, showAttention: false, panel: panel)
                }
            }
        }
    }

    private func recentSection(_ panel: PanelModel) -> some View {
        section(title: "最近完成（\(MetricsFormat.countText(panel.recentCompleted.count))）") {
            if panel.recentCompleted.isEmpty {
                Text("最近没有完成的执行").font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(panel.recentCompleted) { card in
                    runCard(card, showAttention: false, panel: panel)
                }
            }
        }
    }

    // MARK: card

    private func runCard(_ card: PanelRunCard, showAttention: Bool,
                         panel: PanelModel) -> some View {
        Button {
            model.openRunDetail(project: card.run.project, runID: card.run.runID)
            // G012 用户反馈 2: 激活应用并把（已存在或新建的）单一详情
            // 窗口前置取键——openWindow 本身不保证前置被遮挡的窗口。
            DetailWindowRouting.openRunDetailFrontmost(openWindow)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text("\(Self.projectName(card.run.project)) / \(card.run.goal?.objective ?? card.run.runID)")
                        .font(.callout)
                        .lineLimit(1)
                    Spacer()
                    // 控制层徽标（G011 分层）：目标控制状态与执行状态并列
                    // 呈现——已暂停 Goal 下仍执行中的 Run 两个维度同时可见。
                    // 终态 done 降为普通文字（G012 用户反馈 3）。
                    if let badge = card.controlBadgeText {
                        Text(badge)
                            .font(.caption2)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(RoundedRectangle(cornerRadius: 3)
                                .fill(Color.orange.opacity(0.18)))
                            .help("Goal 控制状态（与执行状态分立）")
                    } else if let plain = card.controlPlainText {
                        // 已完成是预期的终态：文字仍可见，但不徽标强调
                        Text(plain)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .help("Goal 控制状态（与执行状态分立）")
                    }
                    Text(card.run.status.display).font(.caption)
                        .foregroundStyle(card.run.status.known ? .primary : .secondary)
                }
                if showAttention {
                    ForEach(card.attention) { item in
                        HStack(alignment: .top, spacing: 4) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.caption2)
                                .foregroundStyle(.orange)
                            Text("\(Self.kindLabel(item.kind))：\(item.detail)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                }
                HStack(spacing: 8) {
                    // 当前计划进度：已完成任务数 / 当前计划任务数
                    Text("任务 \(MetricsFormat.progressText(card.run.progress))（当前计划）")
                    // 历史计划消耗：observed tokens over ALL revisions —
                    // a separate expression, never mixed into progress.
                    Text("已观测 token \(MetricsFormat.tokenText(card.run.consumption?.observedTotal))（全部版本）")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    if let health = panel.health(for: card.run.project)?.health {
                        Label("数据健康：\(health)", systemImage: "stethoscope")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if let asOf = card.run.dataAsOf {
                        Text("数据更新 \(Self.compactTime(asOf))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("打开执行详情")
    }

    private func section(title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline.weight(.semibold))
            content()
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack {
            Button("查看全部执行…") {
                model.openAllRuns()
                DetailWindowRouting.openRunDetailFrontmost(openWindow)
            }
            Spacer()
            Button("设置…") {
                Self.openSettingsWindow()
            }
            Button("退出") {
                model.shutdown()
                NSApp.terminate(nil)
            }
        }
        .buttonStyle(.link)
        .font(.callout)
    }

    /// macOS renamed the settings action across versions; try both.
    static func openSettingsWindow() {
        for selector in [Selector(("showSettingsWindow:")),
                         Selector(("showPreferencesWindow:"))] {
            if NSApp.sendAction(selector, to: nil, from: nil) { return }
        }
    }

    // MARK: helpers

    static func projectName(_ key: String) -> String {
        (key as NSString).lastPathComponent
    }

    static func kindLabel(_ kind: String) -> String {
        switch kind {
        case "failed_task": return "任务失败"
        case "blocked_run": return "执行阻塞"
        default: return kind
        }
    }

    /// "2026-10-16T09:59:30+00:00" -> "09:59:30"; unparseable stays raw.
    static func compactTime(_ iso: String) -> String {
        DisplayTime.short(iso) ?? iso
    }
}
