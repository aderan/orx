import SwiftUI
import ORXAnalyticsCore

/// 错误与诊断视图 (design §8 容错). Three honest blocks:
///
/// 1. 引擎 — connection state, the repeated-failure count (the threshold
///    that opened this entry), the LAST FAILURE REASON with its time,
///    and what to do about it.
/// 2. 数据状态 — 旧数据时间: when the still-displayed data was last
///    successfully collected, plus its age. During failures the panel
///    keeps showing that data; this is where its age is disclosed.
/// 3. 来源 — per-source failures from the last successful overview,
///    each with the engine's data-health classification, the failure
///    reason and that source's own 旧数据时间; healthy sources are
///    summarized — 单源失败不拖垮整体.
struct DiagnosticsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                channelSection
                Divider()
                engineSection
                Divider()
                dataSection
                Divider()
                sourcesSection
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 480, minHeight: 420)
        .navigationTitle("诊断")
    }

    // MARK: 构建渠道 (G011 T005)

    /// The distribution channel this app was built for, read from the
    /// app's own Info.plist. Shown honestly even when unlabeled — an
    /// internal preview is never presented as a formal installer.
    private var channelSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("构建渠道").font(.headline)
            if let info = Self.channelInfo {
                LabeledContent("渠道") { Text(info.summaryText) }
                if let audience = info.audience {
                    LabeledContent("受众") {
                        Text(audience).foregroundStyle(.secondary)
                    }
                }
                if let notice = info.gatekeeperNotice {
                    LabeledContent("Gatekeeper 限制") {
                        Text(notice).foregroundStyle(.secondary)
                    }
                }
                LabeledContent("来源提交") {
                    Text(info.sourceCommit.map { commit in
                        info.hasCompleteSourceCommit
                            ? commit : "\(commit)（非完整提交）"
                    } ?? "未记录")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                }
                if info.isInternalPreview {
                    Label("内部预览不是正式安装包：ad-hoc 签名、未公证，仅限内部测试。",
                          systemImage: "person.crop.circle.badge.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("未标注渠道（开发运行或旧构建）；不视为正式分发产物。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private static let channelInfo: AppChannelInfo? =
        AppChannelInfo.from(infoDictionary: Bundle.main.infoDictionary ?? [:])

    // MARK: 引擎

    private var engineSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("引擎").font(.headline)
            LabeledContent("连接状态") { Text(connectionText) }
            LabeledContent("连续刷新失败") {
                Text("\(MetricsFormat.countText(model.diagnostics.consecutiveFailures)) 次（达到 \(MetricsFormat.countText(DiagnosticsState.failureThreshold)) 次显示此诊断入口）")
            }
            if let failure = model.diagnostics.lastFailureText {
                LabeledContent("最近失败原因") {
                    Text(failure).foregroundStyle(.secondary)
                }
            }
            if let at = model.diagnostics.lastFailureAt {
                LabeledContent("最近失败时间") { Text(Self.fullTime(at)) }
            }
            if model.diagnostics.needsDiagnosticsEntry {
                Label("引擎连续刷新失败。请在终端确认引擎在运行（python -m orxanalytics.engine），或重启引擎后重试。期间面板继续显示上次成功数据。",
                      systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var connectionText: String {
        switch model.connection {
        case .idle: return "未连接"
        case .connecting: return "连接中"
        case let .ready(version): return "已连接（协议 v\(version)）"
        case let .incompatible(requested, supported):
            return "协议不兼容：App v\(requested) / 引擎 v\(supported)（已停止查询，不强行接管）"
        case .notRunning(let why): return why
        }
    }

    // MARK: 数据状态 (旧数据时间)

    private var dataSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("数据状态").font(.headline)
            if let oldDataAt = model.lastRefreshedAt ?? model.cachedOverview?.cachedAt {
                LabeledContent("旧数据时间") {
                    Text("\(Self.fullTime(oldDataAt))（\(Self.ageText(oldDataAt))前采集）")
                }
            } else {
                LabeledContent("旧数据时间") { Text("尚无成功采集") }
            }
            if model.diagnostics.consecutiveFailures > 0 {
                Text("当前正在显示上次成功采集的数据；失败原因见上方。数据年龄不代表执行停摆。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if model.isResyncingAfterWake {
                Label("刚从睡眠唤醒，正在重新同步（睡眠期间不判定停摆）…", systemImage: "arrow.triangle.2.circlepath")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: 来源

    private var sourcesSection: some View {
        let envelope = model.cachedOverview?.envelope
        let (unhealthy, healthy) = envelope.map { DiagnosticsBuilder.sourceRows(from: $0) }
            ?? ([], 0)
        return VStack(alignment: .leading, spacing: 6) {
            Text("来源").font(.headline)
            if envelope == nil {
                Text("尚无成功采集的来源状态。").font(.caption).foregroundStyle(.secondary)
            } else if unhealthy.isEmpty {
                Text("没有失败来源。\(MetricsFormat.countText(healthy)) 个来源数据健康正常。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(unhealthy) { row in
                    sourceRow(row)
                }
                Text("其余 \(MetricsFormat.countText(healthy)) 个来源正常——单源失败不影响其他来源的展示。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func sourceRow(_ row: SourceStatusRow) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Image(systemName: "exclamationmark.triangle")
                    .font(.caption2).foregroundStyle(.orange)
                Text((row.project as NSString).lastPathComponent)
                    .font(.callout.weight(.medium))
                Spacer()
                Text(row.health ?? "异常").font(.caption)
            }
            if let warning = row.warning {
                Text("失败原因：\(warning)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text("旧数据时间：\(row.takenAt.flatMap(DisplayTime.full) ?? "未知")")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 6))
    }

    // MARK: helpers

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
