import XCTest
@testable import ORXAnalyticsCore

final class OverviewCacheTests: XCTestCase {
    private var directory: URL!

    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("orx-cache-tests-\(UUID().uuidString)",
                                    isDirectory: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    private func fixtureEnvelope(active: Int, attention: Int) -> OverviewEnvelope {
        let data = EngineSimulator.encode(
            EngineSimulator.overviewResponse(activeRuns: active, attention: attention))
        do {
            return try JSONDecoder().decode(OverviewEnvelope.self, from: data)
        } catch {
            fatalError("fixture decode failed: \(error)")
        }
    }

    func testRoundtripPreservesPayloadAndTimestamp() {
        let store = OverviewCacheStore(directory: directory)
        let at = Date(timeIntervalSince1970: 1_790_000_000)
        store.save(CachedOverview(envelope: fixtureEnvelope(active: 4, attention: 1),
                                  cachedAt: at))
        let loaded = store.load()
        XCTAssertNotNil(loaded)
        XCTAssertEqual(loaded?.envelope.summary.activeRuns, 4)
        XCTAssertEqual(loaded?.envelope.summary.attention, 1)
        XCTAssertEqual(loaded?.cachedAt.timeIntervalSince1970 ?? 0,
                       at.timeIntervalSince1970, accuracy: 0.001)
        XCTAssertEqual(loaded?.ageSeconds(now: at.addingTimeInterval(30)) ?? 0, 30)
    }

    func testSaveOverwritesLatestSnapshot() {
        let store = OverviewCacheStore(directory: directory)
        store.save(CachedOverview(envelope: fixtureEnvelope(active: 9, attention: 0),
                                  cachedAt: Date(timeIntervalSince1970: 100)))
        store.save(CachedOverview(envelope: fixtureEnvelope(active: 2, attention: 0),
                                  cachedAt: Date(timeIntervalSince1970: 200)))
        XCTAssertEqual(store.load()?.envelope.summary.activeRuns, 2)
    }

    func testMissingFileLoadsNil() {
        let store = OverviewCacheStore(directory: directory)
        XCTAssertNil(store.load())
    }

    func testCorruptFileLoadsNilNotCrash() throws {
        try FileManager.default.createDirectory(at: directory,
                                                withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("overview-cache.json")
        try Data("{ not valid json".utf8).write(to: file)
        let store = OverviewCacheStore(directory: directory)
        XCTAssertNil(store.load(), "unknown cache content is isolated, never a crash")
    }

    func testClearIsSeparateAndExplicit() {
        let store = OverviewCacheStore(directory: directory)
        store.save(CachedOverview(envelope: fixtureEnvelope(active: 1, attention: 0),
                                  cachedAt: Date()))
        XCTAssertNotNil(store.load())
        store.clear()
        XCTAssertNil(store.load())
    }

    func testCacheFileIsUserPrivate() throws {
        let store = OverviewCacheStore(directory: directory)
        store.save(CachedOverview(envelope: fixtureEnvelope(active: 1, attention: 0),
                                  cachedAt: Date()))
        let file = directory.appendingPathComponent("overview-cache.json")
        let attrs = try FileManager.default.attributesOfItem(atPath: file.path)
        let perms = (attrs[.posixPermissions] as? NSNumber)?.int16Value ?? -1
        XCTAssertEqual(perms, 0o600, "display cache must be user-private")
    }
}
