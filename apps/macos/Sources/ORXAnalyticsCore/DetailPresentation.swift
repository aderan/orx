import Foundation

// MARK: - three independent dimensions (design §4)

/// The three axes every run presents SEPARATELY: execution status,
/// attention facts, data health. Each text comes from engine facts —
/// an unrecognized raw status stays 未知状态, an unavailable health
/// classification stays 未知, and nothing mixes the three into one
/// red/yellow/green verdict.
///
/// G011 lifecycle layering: `goalControl` carries the Goal CONTROL
/// layer (进行中/已暂停/取消中/已取消/已完成) beside `executionStatus`
/// (the Run's facts) — a paused Goal with an in-flight run shows
/// 已暂停 + 执行中; neither value rewrites the other. Absent or unknown
/// control layers display 未知, never a guessed terminal state.
public struct RunDimensions: Equatable {
    public let executionStatus: String
    public let goalControl: String
    public let attentionSummary: String
    public let dataHealth: String

    public static func build(run: RunFacts?, tasks: [TaskListItem],
                             health: DataHealthBlock?,
                             snapshots: [String: SnapshotView],
                             project: String?) -> RunDimensions {
        let status = run?.status.display ?? "未知状态"
        let goalControl = run?.goal?.status.map { control -> String in
            control.known ? control.display : "未知"
        } ?? "未知"

        // Attention = current-facts failures of the CURRENT plan only
        // (design §4: a recovered failure is history, waiting on
        // prerequisites is not an anomaly). We count failed tasks of
        // the current revision as reported by the engine's status
        // mapping — no recovery guessing here; the per-task retry
        // wording lives in the drill-down.
        let currentRevision = run?.progress?.revisionID
        let attention: String
        if let currentRevision {
            let failed = tasks.filter {
                $0.revisionID == currentRevision && $0.status.raw == "failed"
            }
            attention = failed.isEmpty
                ? "无当前失败任务"
                : "当前计划失败任务 \(MetricsFormat.countText(failed.count))"
        } else {
            attention = "当前计划未知"
        }

        let dataHealthText: String
        if let health, let label = health.health, !label.isEmpty {
            dataHealthText = label          // engine's own classification
        } else if let snap = snapshots[project ?? ""], snap.supported == false {
            dataHealthText = "版本不支持"
        } else {
            dataHealthText = "未知"
        }
        return RunDimensions(executionStatus: status,
                              goalControl: goalControl,
                              attentionSummary: attention,
                              dataHealth: dataHealthText)
    }
}

// MARK: - retry wording (design §4)

/// Wording for a task's retry state. "重试中" requires BOTH a failing
/// attempt and a successor; a recovered failure is labeled as history,
/// not an active anomaly.
public enum RetryDisplay {
    public static func text(_ retry: RetrySummary?) -> String {
        guard let retry else { return "—" }
        let failing = retry.failingAttemptIDs ?? []
        let successors = retry.successorAttemptIDs ?? []
        if retry.recovered == true {
            return "已恢复（后续尝试已接替）"
        }
        if !failing.isEmpty && !successors.isEmpty {
            return "重试中"
        }
        if !failing.isEmpty {
            return "失败待重试"
        }
        return "无失败尝试"
    }
}

// MARK: - model distribution (counts only)

/// Attempts-per-model distribution. COUNTING ONLY: the metrics freeze
/// forbids this layer from re-implementing token attribution, so an
/// entry carries no token numbers at all — token books come from the
/// engine's own ledgers.
public struct ModelDistribution: Equatable {
    public struct Entry: Equatable, Identifiable {
        public let id: String
        public let model: String
        public let attempts: Int
    }

    public let entries: [Entry]

    public init(attempts: [AttemptDetail]) {
        var counts: [String: Int] = [:]
        for attempt in attempts {
            counts[attempt.model ?? "未知模型", default: 0] += 1
        }
        entries = counts
            .sorted { lhs, rhs in
                lhs.value == rhs.value ? lhs.key < rhs.key : lhs.value > rhs.value
            }
            .map { Entry(id: $0.key, model: $0.key, attempts: $0.value) }
    }
}

// MARK: - controller native preview (request caliber, G012 用户反馈 5)

/// Display strings for the controller session preview. Every number comes
/// from the engine's own request-caliber aggregate through
/// `MetricsFormat.tokenText` (fixed 3-digit grouping) — nothing is summed
/// or merged here, unknown stays "—", and an unattributable controller is
/// shown with its reason instead of a zero-filled number.
public struct ControllerUsagePresentation: Equatable {
    public struct Line: Equatable, Identifiable {
        public let id: String
        public let label: String
        public let text: String
    }

    public let statusText: String
    public let statusRaw: String?
    public let reasonText: String?
    public let noteText: String?
    public let lines: [Line]
    public let sessionText: String?
    public let sourceText: String?
    public let evidenceText: String?
    public let directoryText: String?
    public let caveatText: String?
    /// Present when the session's own cumulative usage is shown WITHOUT a
    /// unique run attribution (shared controller): the numbers are the
    /// SESSION's, not this run's, and are never prorated.
    public let sharedCaveatText: String?
    /// Raw epoch-ms stamps; the view layer owns date formatting.
    public let updatedMS: Int?
    public let usageLastActivityMS: Int?
    public let hasAttributedUsage: Bool
    public let hasSessionUsage: Bool

    public static func build(preview: NativePreview?) -> ControllerUsagePresentation {
        let verdict = preview?.controllerForRun
        let statusRaw = verdict?.status
        let allSessions = preview?.controllerSessions ?? []
        // Token lines render whenever a controller session is NAMED for
        // this run (attributed, or shared-and-unattributed — the session's
        // own cumulative total is a fact, labeled as not this run's). With
        // no session at all (unbound / not imported / no attempts) there
        // is nothing to show but the reason.
        let named: [ControllerSession]
        if statusRaw == "attributed" || statusRaw == "unattributed" {
            named = (verdict?.sessionIDs ?? []).compactMap { sid in
                allSessions.first { $0.sessionID == sid }
            }
        } else {
            named = []
        }

        let statusText: String
        switch statusRaw {
        case "attributed": statusText = "已按唯一证据归属本执行"
        case "unattributed": statusText = "无法唯一归属（跨执行共享，不摊分）"
        case "host_session_unbound":
            statusText = "未绑定会话（host_session_unbound）"
        case "session_not_imported": statusText = "关联会话未在 zcode 镜像中"
        case "no_zcode_attempts": statusText = "本执行无 zcode 尝试"
        case "no_controller_session": statusText = "无独立 controller 会话"
        default: statusText = "未知"
        }

        var lines: [Line] = []
        var sessionText: String?
        var sourceText: String?
        var evidenceText: String?
        var directoryText: String?
        var caveatText: String?
        var updatedMS: Int?
        var usageLastActivityMS: Int?
        if let entry = named.first {
            sessionText = entry.sessionID
            updatedMS = entry.updatedMS
            let own = entry.ownUsage
            sourceText = own?.source
            usageLastActivityMS = own?.lastActivityMS
            lines = [
                Line(id: "ctrl.requests", label: "请求数",
                     text: MetricsFormat.countText(own?.requests)),
                Line(id: "ctrl.input", label: "输入",
                     text: MetricsFormat.tokenText(own?.inputTokens)),
                Line(id: "ctrl.output", label: "输出",
                     text: MetricsFormat.tokenText(own?.outputTokens)),
                Line(id: "ctrl.cache_read", label: "缓存读（input 子集）",
                     text: MetricsFormat.tokenText(own?.cacheRead)),
                Line(id: "ctrl.processed", label: "处理量（input＋output）",
                     text: MetricsFormat.tokenText(own?.processedTokens)),
            ]
            var caveats: [String] = []
            if let zeros = own?.errorRowsZeroTokens, zeros > 0 {
                caveats.append("错误请求 \(MetricsFormat.countText(zeros)) 行为零占位（非免费请求，已标记）")
            }
            if let settling = own?.settlingRequestRows, settling > 0 {
                caveats.append("未完成请求 \(MetricsFormat.countText(settling)) 行，累计数为部分值")
            }
            if let missing = own?.missingFields {
                let hits = missing.filter { $0.value > 0 }
                    .sorted { $0.key < $1.key }
                    .map { "\($0.key) 缺 \(MetricsFormat.countText($0.value)) 行" }
                if !hits.isEmpty {
                    caveats.append("字段缺失 " + hits.joined(separator: "，"))
                }
            }
            caveatText = caveats.isEmpty ? nil : caveats.joined(separator: "；")
            if let basis = entry.runAttribution?.basis {
                evidenceText = basis
            }
            if let dir = entry.directory {
                let match = entry.directoryMatchesProject == true
                    ? "与项目根一致" : (entry.directoryMatchesProject == false
                                        ? "与项目根不同（仅记录，不作归属依据）" : "一致性未知")
                directoryText = "\(dir)（\(match)）"
            }
        } else if let mentioned = verdict?.sessionIDs, !mentioned.isEmpty {
            sessionText = mentioned.joined(separator: ", ")
        }
        let sharedCaveat: String? = (statusRaw == "unattributed" && !named.isEmpty)
            ? "以上为该 controller 会话自身的累计请求量（跨多个 run 共享，未归属到单一 run，绝不按 run 摊分）"
            : nil

        return ControllerUsagePresentation(
            statusText: statusText,
            statusRaw: statusRaw,
            reasonText: verdict?.reason,
            noteText: verdict?.note,
            lines: lines,
            sessionText: sessionText,
            sourceText: sourceText,
            evidenceText: evidenceText,
            directoryText: directoryText,
            caveatText: caveatText,
            sharedCaveatText: sharedCaveat,
            updatedMS: updatedMS,
            usageLastActivityMS: usageLastActivityMS,
            hasAttributedUsage: statusRaw == "attributed" && !named.isEmpty,
            hasSessionUsage: !named.isEmpty)
    }
}

// MARK: - the two calibers, side by side, never added (design §5)

/// Row groups for the detail's 消耗分析 zone. STRUCTURALLY INCAPABLE of
/// producing a combined number: it holds exactly two independently
/// labeled groups, each row built from ONE engine value through
/// `MetricsFormat.tokenText` — nothing is summed here.
public struct DetailCalibers: Equatable {
    public struct Row: Equatable, Identifiable {
        public let id: String
        public let label: String
        public let text: String
    }

    public let orxRows: [Row]
    public let nativeRows: [Row]

    public init(usage: RunUsage?, preview: NativePreview?) {
        // ORX 已记录用量 — per-field rows; unknown stays "—"; NO total
        // is derived because cache semantics belong to the source.
        let tokens = usage?.tokens
        orxRows = [
            Row(id: "orx.input", label: "输入",
                text: MetricsFormat.tokenText(tokens?.inputTokens)),
            Row(id: "orx.output", label: "输出",
                text: MetricsFormat.tokenText(tokens?.outputTokens)),
            Row(id: "orx.cached", label: "缓存（cached_input）",
                text: MetricsFormat.tokenText(tokens?.cachedTokens)),
        ]
        // 原生会话预览 — the union's own turn sums as its own rows.
        let t = preview?.union?.tokens
        nativeRows = [
            Row(id: "nat.turns", label: "会话轮次",
                text: MetricsFormat.countText(t?.turns)),
            Row(id: "nat.input", label: "输入",
                text: MetricsFormat.tokenText(t?.inputTokens)),
            Row(id: "nat.output", label: "输出",
                text: MetricsFormat.tokenText(t?.outputTokens)),
            Row(id: "nat.reasoning", label: "推理",
                text: MetricsFormat.tokenText(t?.reasoningTokens)),
            Row(id: "nat.cache_read", label: "缓存读",
                text: MetricsFormat.tokenText(t?.cacheRead)),
            Row(id: "nat.cache_write", label: "缓存写",
                text: MetricsFormat.tokenText(t?.cacheCreation)),
        ]
    }
}
