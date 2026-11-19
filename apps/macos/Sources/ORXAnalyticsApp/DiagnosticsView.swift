import SwiftUI
import ORXAnalyticsCore

struct DiagnosticsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: ORXSpace.lg) {
                ORXPageHeader("系统与来源诊断", detail: "引擎连接、采集时间和各项目来源状态。") {
                    if model.isRefreshing {
                        ProgressView().controlSize(.small).accessibilityLabel("正在刷新")
                    }
                    if !model.connection.isFatal {
                        Button {
                            Task { await model.refreshNow() }
                        } label: {
                            Label("刷新状态", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(ORXButtonStyle())
                        .disabled(model.isRefreshing)
                    }
                }
                engineSection
                dataSection
                sourcesSection
                channelSection
            }
            .padding(ORXSpace.xl)
        }
        .orxSurface()
    }

    private var engineSection: some View {
        section("引擎", symbol: "bolt.horizontal.circle") {
            HStack {
                Text(connectionText).font(ORXType.label).textSelection(.enabled)
                Spacer()
                ORXBadge(failureCountText, tone: model.diagnostics.consecutiveFailures > 0 ? .warning : .neutral)
            }
            if let failure = model.diagnostics.lastFailureText {
                ORXValueRow("最近失败原因", failure).textSelection(.enabled)
            }
            if let time = model.diagnostics.lastFailureAt {
                ORXValueRow("最近失败时间", Self.fullTime(time), monospaced: true)
            }
            if model.connection.isFatal {
                ORXNotice(text: "请升级应用或引擎至兼容版本后重新打开 ORX。",
                          tone: .warning, symbol: "exclamationmark.triangle")
            } else if model.diagnostics.needsDiagnosticsEntry {
                ORXNotice(text: "请确认本地引擎在运行，或重启引擎再刷新。可在终端运行 python -m orxanalytics.engine。",
                          tone: .warning, symbol: "exclamationmark.triangle")
            }
        }
    }

    private var failureCountText: String {
        "\(MetricsFormat.countText(model.diagnostics.consecutiveFailures)) 次连续失败"
    }

    private var connectionText: String {
        switch model.connection {
        case .idle: return "未连接"
        case .connecting: return "连接中"
        case .ready(let version): return "已连接 · 协议 v\(version)"
        case .incompatible(let requested, let supported):
            return "协议不兼容 · App v\(requested) / 引擎 v\(supported) · 查询已停止"
        case .notRunning(let reason): return reason
        }
    }

    private var dataSection: some View {
        section("数据状态", symbol: "clock") {
            if let date = model.lastRefreshedAt ?? model.cachedOverview?.cachedAt {
                ORXValueRow("上次成功采集", "\(Self.fullTime(date)) · \(Self.ageText(date))前", monospaced: true)
            } else {
                ORXValueRow("上次成功采集", "尚无成功采集")
            }
            if model.diagnostics.consecutiveFailures > 0 && model.cachedOverview != nil {
                ORXNotice(text: "正在显示上次成功的数据。数据年龄只表示观测时间。")
            }
            if model.isResyncingAfterWake {
                ORXNotice(text: "从睡眠唤醒后正在重新同步", symbol: "arrow.triangle.2.circlepath")
            }
        }
    }

    private var sourcesSection: some View {
        section("项目来源", symbol: "externaldrive") {
            if let envelope = model.cachedOverview?.envelope {
                let sources = DiagnosticsBuilder.sourceRows(from: envelope)
                ForEach(sources.unhealthy) { source in
                    VStack(alignment: .leading, spacing: ORXSpace.xs) {
                        HStack {
                            Text(QuickPanelView.projectName(source.project)).font(ORXType.section)
                            Text(source.project).font(ORXType.code).foregroundStyle(ORXColor.secondary)
                                .lineLimit(1).truncationMode(.middle).help(source.project)
                            Spacer()
                            ORXBadge(source.health ?? "异常", tone: .warning, symbol: "exclamationmark.triangle")
                        }
                        Text("上次可用快照 · \(source.takenAt.flatMap(DisplayTime.full) ?? "未知")")
                            .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                        if let warning = source.warning {
                            Text(warning).font(ORXType.caption).foregroundStyle(ORXColor.warningText)
                        }
                    }
                    .padding(ORXSpace.md)
                    .background(ORXColor.inset, in: RoundedRectangle(cornerRadius: ORXRadius.row))
                }
                Text("\(MetricsFormat.countText(sources.healthyCount)) 个来源正常")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            } else {
                Text("尚无项目来源状态。")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
        }
    }

    private var channelSection: some View {
        section("构建渠道", symbol: "shippingbox") {
            if let info = Self.channelInfo {
                ORXValueRow("渠道", info.summaryText)
                if let audience = info.audience { ORXValueRow("受众", audience) }
                if let notice = info.gatekeeperNotice { ORXValueRow("Gatekeeper 限制", notice) }
                let source = info.sourceCommit.map { $0 + (info.hasCompleteSourceCommit ? "" : "（非完整提交）") }
                ORXValueRow("来源提交", source ?? "未记录", monospaced: true)
                    .textSelection(.enabled)
                if info.isInternalPreview {
                    ORXNotice(text: "内部预览 · ad-hoc 签名 · 未公证 · 仅限内部测试")
                }
            } else {
                ORXValueRow("渠道", "未标注 · 开发运行或旧构建")
            }
        }
    }

    private func section<Content: View>(_ title: String, symbol: String,
                                       @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.md) {
            Label(title, systemImage: symbol).font(ORXType.title).accessibilityAddTraits(.isHeader)
            content()
        }
        .orxCard()
    }

    private static let channelInfo = AppChannelInfo.from(infoDictionary: Bundle.main.infoDictionary ?? [:])

    private static func fullTime(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.string(from: date)
    }

    private static func ageText(_ date: Date) -> String {
        let seconds = max(0, Int(Date().timeIntervalSince(date)))
        if seconds >= 3600 { return "\(seconds / 3600) 小时" }
        if seconds >= 60 { return "\(seconds / 60) 分钟" }
        return "\(seconds) 秒"
    }
}
