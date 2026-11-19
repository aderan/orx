import XCTest
@testable import ORXAnalyticsCore

final class ORXRoutingClientTests: XCTestCase {
    func testFinderPathResolvesUserToolWithoutShellPath() throws {
        let home = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let tool = home.appendingPathComponent(".local/bin/orx")
        try FileManager.default.createDirectory(at: tool.deletingLastPathComponent(),
                                               withIntermediateDirectories: true)
        try Data().write(to: tool)
        try FileManager.default.setAttributes([.posixPermissions: 0o755],
                                              ofItemAtPath: tool.path)
        XCTAssertEqual(try ORXRoutingClient.resolveExecutable(
            override: "", home: home, environment: ["PATH": "/usr/bin:/bin"]), tool)
    }

    func testExplicitMissingORXReturnsAnErrorWithoutFallingBack() {
        XCTAssertThrowsError(try ORXRoutingClient.resolveExecutable(
            override: "/unavailable/bin/orx", home: URL(fileURLWithPath: "/unavailable"),
            environment: ["PATH": "/usr/bin:/bin"]))
    }

    @MainActor
    func testTaskUsageOpensTheExactProjectWithoutClearingSelectedRun() {
        let model = AppModel(client: FakeEngineClient(), cache: OverviewCacheStore(directory: FileManager.default.temporaryDirectory))
        model.openRunDetail(project: "/projects/a", runID: "R001", page: .usage)
        model.openRouting(project: "/projects/a")
        XCTAssertEqual(model.mainPage, .routing)
        XCTAssertEqual(model.routingProject, "/projects/a")
        XCTAssertEqual(model.selectedRun, RunIdentity(project: "/projects/a", runID: "R001"))
        model.openRunDetail(project: "/projects/b", runID: "R001")
        XCTAssertEqual(model.mainPage, .execution)
        XCTAssertEqual(model.selectedRun?.project, "/projects/b")
    }
}
