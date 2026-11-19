import XCTest
@testable import ORXAnalyticsCore

/// Diagnostics state (design §8 容错): repeated engine failures surface a
/// diagnostics entry; the last SUCCESS time (旧数据时间) and the last
/// failure reason survive across failures so the still-displayed data is
/// never presented as fresh.
final class DiagnosticsStateTests: XCTestCase {

    func testInitialStateHasNoEntry() {
        let state = DiagnosticsState()
        XCTAssertEqual(state.consecutiveFailures, 0)
        XCTAssertFalse(state.needsDiagnosticsEntry)
        XCTAssertNil(state.lastFailureText)
        XCTAssertNil(state.lastSuccessAt)
    }

    func testTwoFailuresDoNotOpenEntryThirdDoes() {
        var state = DiagnosticsState()
        state.recordFailure("引擎连接失败：socket 无监听", at: Date(timeIntervalSince1970: 100))
        state.recordFailure("引擎连接失败：socket 无监听", at: Date(timeIntervalSince1970: 200))
        XCTAssertFalse(state.needsDiagnosticsEntry,
                       "偶发失败不打扰；重复失败才显示诊断入口")
        state.recordFailure("引擎连接失败：socket 无监听", at: Date(timeIntervalSince1970: 300))
        XCTAssertTrue(state.needsDiagnosticsEntry)
        XCTAssertEqual(state.consecutiveFailures, 3)
        XCTAssertEqual(state.lastFailureText, "引擎连接失败：socket 无监听")
    }

    func testSuccessPreservesLastSuccessTimeAcrossLaterFailures() {
        var state = DiagnosticsState()
        let successAt = Date(timeIntervalSince1970: 1000)
        state.recordSuccess(at: successAt)
        state.recordFailure("boom", at: Date(timeIntervalSince1970: 1100))
        state.recordFailure("boom", at: Date(timeIntervalSince1970: 1200))
        // 旧数据时间：the still-displayed data's timestamp stays knowable
        XCTAssertEqual(state.lastSuccessAt, successAt)
        XCTAssertNotNil(state.lastFailureText)
        XCTAssertNotNil(state.lastFailureAt)
    }

    func testSuccessResetsConsecutiveCountButKeepsHistory() {
        var state = DiagnosticsState()
        state.recordFailure("a", at: Date(timeIntervalSince1970: 1))
        state.recordFailure("b", at: Date(timeIntervalSince1970: 2))
        state.recordFailure("c", at: Date(timeIntervalSince1970: 3))
        XCTAssertTrue(state.needsDiagnosticsEntry)
        let okAt = Date(timeIntervalSince1970: 4)
        state.recordSuccess(at: okAt)
        XCTAssertFalse(state.needsDiagnosticsEntry)
        XCTAssertEqual(state.consecutiveFailures, 0)
        XCTAssertEqual(state.lastSuccessAt, okAt)
        // the recent failure stays visible as history (恢复后可解释)
        XCTAssertEqual(state.lastFailureText, "c")
    }

    func testLatestFailureTextWins() {
        var state = DiagnosticsState()
        state.recordFailure("first")
        state.recordFailure("second")
        XCTAssertEqual(state.lastFailureText, "second")
    }
}

/// Per-source failure listing (design §8: 一个项目失败不影响其他项目).
final class DiagnosticsBuilderTests: XCTestCase {

    private func envelope(rows: [ProjectHealthRow],
                          warnings: [QueryWarning]) -> OverviewEnvelope {
        OverviewEnvelope(
            header: EngineEnvelopeHeader(op: "overview", ok: true,
                                         protocolVersion: 1, generatedAt: nil,
                                         error: nil),
            summary: OverviewSummary(activeRuns: 0, attention: 0,
                                     recentCompleted: 0),
            active: [], attention: [], recentCompleted: [],
            projectHealth: rows, snapshots: [:], warnings: warnings)
    }

    func testHealthySourcesSummarizedAndUnhealthyListedWithReasonAndOldDataTime() {
        let env = envelope(
            rows: [
                ProjectHealthRow(project: "/tmp/good",
                                 dataHealth: DataHealthBlock(health: "正常"),
                                 snapshotID: 1, takenAt: "2026-10-16T09:00:00+00:00"),
                ProjectHealthRow(project: "/tmp/unreadable",
                                 dataHealth: DataHealthBlock(health: "来源不可读",
                                                             sourceReadable: false),
                                 snapshotID: 2,
                                 takenAt: "2026-10-15T09:00:00+00:00"),
                ProjectHealthRow(project: "/tmp/oldschema",
                                 dataHealth: DataHealthBlock(health: "版本不支持",
                                                             schemaSupported: false),
                                 snapshotID: 3,
                                 takenAt: "2026-10-14T09:00:00+00:00"),
            ],
            warnings: [])
        let (unhealthy, healthy) = DiagnosticsBuilder.sourceRows(from: env)
        XCTAssertEqual(healthy, 1, "单源失败不拖垮整体：healthy sources stay counted")
        XCTAssertEqual(unhealthy.count, 2)
        let unreadable = unhealthy.first { $0.project == "/tmp/unreadable" }
        XCTAssertEqual(unreadable?.health, "来源不可读")
        XCTAssertEqual(unreadable?.takenAt, "2026-10-15T09:00:00+00:00",
                       "旧数据时间 = the last successful snapshot for that source")
        XCTAssertTrue(unhealthy.contains { $0.health == "版本不支持" })
    }

    func testUnknownHealthClassificationIsNotAssertedAsFailure() {
        let env = envelope(rows: [ProjectHealthRow(project: "/tmp/unknown",
                                                   dataHealth: nil)],
                           warnings: [])
        let (unhealthy, healthy) = DiagnosticsBuilder.sourceRows(from: env)
        XCTAssertTrue(unhealthy.isEmpty, "未知不等于失败 — never guessed")
        XCTAssertEqual(healthy, 0)
    }

    func testWarningsBecomeFailureRowsForTheirProject() {
        let env = envelope(
            rows: [ProjectHealthRow(project: "/tmp/gone",
                                    dataHealth: DataHealthBlock(health: "来源不可读"))],
            warnings: [
                QueryWarning(code: "project-directory-missing",
                             project: "/tmp/gone",
                             detail: "registered project directory no longer exists"),
                QueryWarning(code: "snapshot-stale",
                             project: "/tmp/other",
                             detail: "snapshot older than staleness window"),
            ])
        let (unhealthy, _) = DiagnosticsBuilder.sourceRows(from: env)
        let gone = unhealthy.first { $0.project == "/tmp/gone" }
        XCTAssertTrue(gone?.warning?.contains("project-directory-missing") ?? false,
                      "the engine's warning detail is the failure reason")
        // a warned project without a health row still gets a row
        XCTAssertTrue(unhealthy.contains { $0.project == "/tmp/other" })
    }

    func testEmptyOverviewYieldsNoFailures() {
        let (unhealthy, healthy) = DiagnosticsBuilder.sourceRows(from: envelope(rows: [],
                                                                                warnings: []))
        XCTAssertTrue(unhealthy.isEmpty)
        XCTAssertEqual(healthy, 0)
    }
}
