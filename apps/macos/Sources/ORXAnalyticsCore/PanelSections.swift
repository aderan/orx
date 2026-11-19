import Foundation

/// Pure presentation model for the quick panel's three sections
/// (design §3). Every merge rule lives here so it is unit-testable
/// without a GUI:
///
/// - 同一 Run 只出现一次：a run referenced by attention items is shown
///   ONLY in 需要关注 and is removed from 进行中 / 最近完成.
/// - 异常优先排序：the attention section is the first section
///   (`sectionOrder`); within a section cards keep the engine's stable
///   identity order.
/// - Attention for a run missing from both lists (e.g. a terminal
///   failed run) still gets a synthetic card — attention facts are
///   never dropped, and the synthetic status is 未知状态, never a
///   guessed terminal state (design §4).
/// - Current-plan progress and all-revision consumption ride along as
///   SEPARATE fields (`run.progress` vs `run.consumption`); there is no
///   combined value (`combinedProgressConsumpletion` stays nil) and no
///   percentage is fabricated.
/// - Lifecycle layering (G011): which SECTION a run lands in is the
///   ENGINE's classification (a cancelled Goal's runs arrive in no
///   current list); the Goal control layer travels beside the run
///   status as `controlStatus` — two dimensions, never rewritten.
/// - Control-state presentation (G012 用户反馈 3): non-terminal control
///   states (已暂停/取消中/已取消) keep the card badge; the terminal
///   `done` renders as plain text (`controlPlainText`) — still visible,
///   no badge emphasis. The status-line summary counts both alike.
public struct PanelRunCard: Equatable, Identifiable {
    public let run: OverviewRunItem
    public let attention: [AttentionItem]

    public var id: String { run.id }

    /// Always nil. Exists so tests (and readers) can assert that this
    /// model never fabricates a combined progress/consumption number.
    public var combinedProgressConsumption: String? { nil }

    /// The Goal CONTROL layer of this card (已暂停/取消中/已取消/…),
    /// beside the execution facts of `run.status`. Nil when the engine
    /// carried no goal block (legacy payload) — never guessed.
    public var controlStatus: DisplayStatus? { run.goal?.status }

    /// Compact badge text for the card header: "目标 已暂停". Nil when the
    /// control layer is absent or plain active (nothing to distinguish),
    /// unknown (never guessed), or the terminal `done` (G012 用户反馈 3:
    /// 终态不再徽标强调，走 `controlPlainText`). Non-terminal states
    /// (已暂停/取消中/已取消) keep the badge.
    public var controlBadgeText: String? {
        guard let control = controlStatus, control.known,
              control.raw != "active", control.raw != "done" else { return nil }
        return "目标 \(control.display)"
    }

    /// Plain-text form of the TERMINAL control state (G012 用户反馈 3):
    /// "目标 已完成" stays visible on the card but WITHOUT the badge
    /// background or emphasis — done is the expected end state, not
    /// something to flag. Nil for every other control state (absent /
    /// plain active / unknown render nothing; non-terminal use the badge).
    /// Mutually exclusive with `controlBadgeText` by construction.
    public var controlPlainText: String? {
        guard let control = controlStatus, control.known,
              control.raw == "done" else { return nil }
        return "目标 \(control.display)"
    }
}

public enum PanelSection: Equatable {
    case needsAttention
    case active
    case recentCompleted
}

public struct PanelModel: Equatable {
    public let needsAttention: [PanelRunCard]
    public let active: [PanelRunCard]
    public let recentCompleted: [PanelRunCard]
    public let sectionOrder: [PanelSection]
    /// project -> engine-classified data health (third dimension).
    public let healthByProject: [String: DataHealthBlock]

    /// The panel of "nothing known yet" — used before the first cache
    /// load so the three sections render with empty captions.
    public static var empty: PanelModel {
        PanelModel(needsAttention: [], active: [], recentCompleted: [],
                   sectionOrder: [.needsAttention, .active, .recentCompleted],
                   healthByProject: [:])
    }

    public static func build(_ envelope: OverviewEnvelope) -> PanelModel {        var byRun: [String: PanelRunCard] = [:]
        var order: [String] = []
        let activeIds = Set(envelope.active.map(\.id))
        let recentIds = Set(envelope.recentCompleted.map(\.id))

        func add(_ run: OverviewRunItem) {
            guard byRun[run.id] == nil else { return }   // 同一 Run 只出现一次
            byRun[run.id] = PanelRunCard(run: run, attention: [])
            order.append(run.id)
        }
        envelope.active.forEach(add)
        envelope.recentCompleted.forEach(add)

        for item in envelope.attention {
            let key = "\(item.project)#\(item.runID)"
            if let card = byRun[key] {
                byRun[key] = PanelRunCard(run: card.run,
                                          attention: card.attention + [item])
            } else {
                // Run not listed anywhere (terminal but needing
                // attention): synthesize a card instead of dropping the
                // attention fact. Status stays unknown — no guessing.
                byRun[key] = PanelRunCard(run: OverviewRunItem(
                    project: item.project, runID: item.runID, goal: nil,
                    status: DisplayStatus(raw: nil, known: false,
                                          display: "未知状态"),
                    startedAt: nil, progress: nil, consumption: nil,
                    dataAsOf: nil), attention: [item])
                order.append(key)
            }
        }

        let attentionIds = Set(byRun.filter { !$0.value.attention.isEmpty }.map(\.key))
        let attentionCards = order.filter { attentionIds.contains($0) }
            .compactMap { byRun[$0] }
            .sorted { ($0.run.project, $0.run.runID) < ($1.run.project, $1.run.runID) }
        let activeCards = order
            .filter { activeIds.contains($0) && !attentionIds.contains($0) }
            .compactMap { byRun[$0] }
        let recentCards = order
            .filter { recentIds.contains($0) && !attentionIds.contains($0) }
            .compactMap { byRun[$0] }

        var health: [String: DataHealthBlock] = [:]
        for row in envelope.projectHealth {
            health[row.project] = row.dataHealth
        }
        return PanelModel(needsAttention: attentionCards, active: activeCards,
                          recentCompleted: recentCards,
                          sectionOrder: [.needsAttention, .active, .recentCompleted],
                          healthByProject: health)
    }

    /// The engine's own data-health classification for a project, if
    /// the overview carried one.
    public func health(for project: String) -> DataHealthBlock? {
        healthByProject[project]
    }

    /// Counts of the INTERESTING Goal control states among the active
    /// section (已暂停 / 取消中 / 已取消 / 已完成, in that fixed order) —
    /// the status line summarizes the control layer without touching run
    /// facts. Plain-active and unknown control states add no noise, and
    /// a control state is never counted twice.
    public var activeControlCounts: [(label: String, count: Int)] {
        let order = ["paused", "cancelling", "cancelled", "done"]
        var counts: [String: Int] = [:]
        var labels: [String: String] = [:]
        for card in active {
            guard let status = card.controlStatus, status.known,
                  let raw = status.raw, order.contains(raw) else { continue }
            counts[raw, default: 0] += 1
            labels[raw] = status.display
        }
        return order.compactMap { raw in
            counts[raw].map { (labels[raw] ?? raw, $0) }
        }
    }
}
