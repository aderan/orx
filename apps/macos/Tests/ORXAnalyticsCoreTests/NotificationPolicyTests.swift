import XCTest
@testable import ORXAnalyticsCore

/// Notification decision logic (design §4 通知): default OFF, same
/// anomaly deduped, one round's anomalies MERGED into one notification,
/// low-confidence kinds panel-only, click deep-link target carried.
///
/// No test ever touches the real UserNotifications framework — the
/// policy is a pure value type and the posting surface is a protocol
/// (CONTRIBUTING: 不弹真实系统通知).
final class NotificationPolicyTests: XCTestCase {

    private func item(_ i: Int, kind: String = "failed_task",
                      detail: String? = nil) -> AttentionItem {
        AttentionItem(kind: kind, project: "/tmp/proj\(i)", runID: "R001",
                      taskID: "T00\(i)", revisionID: 101,
                      detail: detail ?? "d\(i)")
    }

    // MARK: confidence classification

    func testFactBasedKindsAreNotifiable() {
        XCTAssertEqual(NotificationPolicy.confidence(forKind: "failed_task"),
                       .factBased)
        XCTAssertEqual(NotificationPolicy.confidence(forKind: "blocked_run"),
                       .factBased)
    }

    func testUnknownKindsAreLowConfidencePanelOnly() {
        // Future/speculative kinds (e.g. a guessed stall) must never fire
        // a system notification — 低置信度推测默认只在面板中显示.
        XCTAssertEqual(NotificationPolicy.confidence(forKind: "stall_suspected"),
                       .lowConfidence)
        XCTAssertEqual(NotificationPolicy.confidence(forKind: "usage_anomaly"),
                       .lowConfidence)
    }

    func testLifecycleControlStatesNeverBecomeNotifiableKinds() {
        // G011 matrix §3: 控制态是徽标，不是异常。Cancelled goals never
        // notify (the engine emits no attention for them), and pause or
        // cancel states must never be added as alert kinds. Any future
        // lifecycle kind stays low-confidence until explicitly promoted.
        XCTAssertEqual(NotificationPolicy.factBasedKinds,
                       ["failed_task", "blocked_run"])
        for kind in ["goal_cancelled", "goal_paused", "goal_cancelling",
                     "run_cancelled", "cancelling_goal", "paused_goal"] {
            XCTAssertEqual(NotificationPolicy.confidence(forKind: kind),
                           .lowConfidence, "\(kind) 不得成为通知类型")
        }
    }

    // MARK: baseline + dedup

    func testFirstRoundIsBaselineNoNotification() {
        var policy = NotificationPolicy()
        XCTAssertNil(policy.processRound([item(1), item(2)], emit: true),
                     "cold-start anomalies are shown by the panel, not announced")
    }

    func testSameAnomalyAcrossRoundsIsDeduped() {
        var policy = NotificationPolicy()
        _ = policy.processRound([item(1)], emit: true)      // baseline
        XCTAssertNil(policy.processRound([item(1)], emit: true),
                     "相同异常去重：a persisting anomaly never re-alarms")
        XCTAssertNil(policy.processRound([item(1)], emit: true))
    }

    // MARK: merge

    func testMultipleNewAnomaliesInOneRoundMergeIntoSingleNotification() {
        var policy = NotificationPolicy()
        _ = policy.processRound([], emit: true)             // baseline
        let payload = policy.processRound([item(1), item(2), item(3)],
                                          emit: true)
        XCTAssertNotNil(payload)
        XCTAssertEqual(payload?.itemCount, 3)
        XCTAssertEqual(payload?.title, "ORX Analytics：3 项需要关注")
        // every new anomaly is named in the merged body (compact card
        // form: project short name + run + task)
        for i in 1...3 {
            XCTAssertTrue(payload?.body.contains("proj\(i)/R001 T00\(i)") ?? false)
        }
        // deep link points at the first new item's execution
        XCTAssertEqual(payload?.project, "/tmp/proj1")
        XCTAssertEqual(payload?.runID, "R001")
    }

    func testSingleNewAnomalyTitle() {
        var policy = NotificationPolicy()
        _ = policy.processRound([], emit: true)
        let payload = policy.processRound([item(1)], emit: true)
        XCTAssertEqual(payload?.title, "ORX Analytics：需要关注")
        XCTAssertEqual(payload?.itemCount, 1)
    }

    // MARK: recovery

    func testRecoveredAnomalyDoesNotAlarmAndReappearanceIsFresh() {
        var policy = NotificationPolicy()
        _ = policy.processRound([item(1)], emit: true)      // baseline: seen={1}
        XCTAssertNil(policy.processRound([item(1)], emit: true))
        // anomaly gone (recovered by a successor attempt): nothing fires
        XCTAssertNil(policy.processRound([], emit: true),
                     "恢复后不反复报警")
        // still gone, still silent
        XCTAssertNil(policy.processRound([], emit: true))
        // the SAME anomaly fails again later: a fresh occurrence alarms once
        let payload = policy.processRound([item(1)], emit: true)
        XCTAssertNotNil(payload, "a re-appearing anomaly is a new occurrence")
        XCTAssertEqual(payload?.itemCount, 1)
        // and immediately dedups again
        XCTAssertNil(policy.processRound([item(1)], emit: true))
    }

    // MARK: low-confidence filtering inside a round

    func testLowConfidenceItemsAreExcludedFromNotifications() {
        var policy = NotificationPolicy()
        _ = policy.processRound([], emit: true)
        let payload = policy.processRound(
            [item(1), item(2, kind: "stall_suspected"), item(3)],
            emit: true)
        XCTAssertEqual(payload?.itemCount, 2,
                       "only the fact-based items notify (1 and 3 are new)")
        XCTAssertTrue(payload?.body.contains("proj1") ?? false)
        XCTAssertTrue(payload?.body.contains("proj3") ?? false)
        XCTAssertFalse(payload?.body.contains("proj2") ?? false,
                       "speculative kinds stay panel-only")
    }

    func testRoundOfOnlyLowConfidenceItemsEmitsNothing() {
        var policy = NotificationPolicy()
        _ = policy.processRound([], emit: true)
        XCTAssertNil(policy.processRound(
            [item(1, kind: "stall_suspected")], emit: true))
    }

    // MARK: disabled rounds still learn the state

    func testDisabledRoundsTrackSeenStateSoEnablingLaterDoesNotBurst() {
        var policy = NotificationPolicy()
        _ = policy.processRound([item(1)], emit: false)
        XCTAssertNil(policy.processRound([item(1)], emit: true),
                     "anomaly already known from panel rounds — no burst on enable")
        // a genuinely new one after enabling still notifies
        let payload = policy.processRound([item(1), item(2)], emit: true)
        XCTAssertEqual(payload?.itemCount, 1)
    }

    // MARK: label

    func testKindLabels() {
        XCTAssertEqual(NotificationPolicy.label(forKind: "failed_task"), "任务失败")
        XCTAssertEqual(NotificationPolicy.label(forKind: "blocked_run"), "执行阻塞")
        // unknown kinds keep their raw string — never a guess
        XCTAssertEqual(NotificationPolicy.label(forKind: "weird"), "weird")
    }

    // MARK: long rounds stay readable

    func testMergedBodyCapsAtFiveLines() {
        var policy = NotificationPolicy()
        _ = policy.processRound([], emit: true)
        let payload = policy.processRound((1...8).map { item($0) }, emit: true)
        XCTAssertEqual(payload?.itemCount, 8)
        XCTAssertTrue(payload?.body.contains("…等 8 项") ?? false)
    }
}
