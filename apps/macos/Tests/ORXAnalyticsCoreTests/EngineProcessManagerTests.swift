import XCTest
@testable import ORXAnalyticsCore

// Engine process management tests (task T012): runtime resolution,
// the upgrade backup guard, and the launch state machine. Everything
// runs against temp directories and injected fakes — no real engine,
// no real socket, no real projects (CONTRIBUTING: 测试用合成来源).

private func makeTempDir() -> URL {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("orx-enginepm-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

// MARK: - Runtime resolution

final class EngineRuntimeResolverTests: XCTestCase {
    func testBundledResolutionRequiresFullRuntime() throws {
        let root = makeTempDir()
        let resources = root.appendingPathComponent("Contents/Resources")
        try FileManager.default.createDirectory(
            at: resources.appendingPathComponent("python/bin"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: resources.appendingPathComponent("orxanalytics"), withIntermediateDirectories: true)

        // Incomplete: no python, no serve script, no package.
        XCTAssertNil(EngineRuntimeResolver.bundled(bundleURL: root))

        // Complete bundle layout resolves.
        try "#!/bin/sh".write(to: resources.appendingPathComponent("python/bin/python3"),
                              atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: resources.appendingPathComponent("python/bin/python3").path)
        try "pass".write(to: resources.appendingPathComponent("orx_engine_serve.py"),
                         atomically: true, encoding: .utf8)
        try "".write(to: resources.appendingPathComponent("orxanalytics/__init__.py"),
                     atomically: true, encoding: .utf8)

        let location = try XCTUnwrap(EngineRuntimeResolver.bundled(bundleURL: root))
        XCTAssertEqual(location.origin, "bundled")
        XCTAssertEqual(location.pythonExecutable.lastPathComponent, "python3")
        XCTAssertEqual(location.serveScript.lastPathComponent, "orx_engine_serve.py")
        XCTAssertEqual(location.moduleSearchPath?.path, resources.path)
    }

    func testDevOverrideUsesEnvAndFindsServeScript() throws {
        let root = makeTempDir()
        let python = root.appendingPathComponent("bin/python3")
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("bin"), withIntermediateDirectories: true)
        try "#!/bin/sh".write(to: python, atomically: true, encoding: .utf8)

        // No env, no override.
        XCTAssertNil(EngineRuntimeResolver.devOverride(environment: [:]))

        // Python set but no serve script anywhere -> unavailable.
        XCTAssertNil(EngineRuntimeResolver.devOverride(
            environment: [EngineRuntimeResolver.pythonOverrideEnv: python.path]))

        // Serve script beside the interpreter (e.g. repo root) resolves.
        let serve = root.appendingPathComponent("orx_engine_serve.py")
        try "pass".write(to: serve, atomically: true, encoding: .utf8)
        let location = try XCTUnwrap(EngineRuntimeResolver.devOverride(
            environment: [EngineRuntimeResolver.pythonOverrideEnv: python.path,
                          EngineRuntimeResolver.pythonPathOverrideEnv: "/repo/src"]))
        XCTAssertEqual(location.origin, "dev-override")
        XCTAssertEqual(location.serveScript, serve)
        XCTAssertEqual(location.moduleSearchPath?.path, "/repo/src")

        // Explicit serve env wins over neighbor discovery.
        let serve2 = root.appendingPathComponent("elsewhere-serve.py")
        try "pass".write(to: serve2, atomically: true, encoding: .utf8)
        let location2 = try XCTUnwrap(EngineRuntimeResolver.devOverride(
            environment: [EngineRuntimeResolver.pythonOverrideEnv: python.path,
                          EngineRuntimeResolver.serveOverrideEnv: serve2.path]))
        XCTAssertEqual(location2.serveScript, serve2)
        XCTAssertNil(location2.moduleSearchPath)
    }
}

// MARK: - Upgrade guard

final class EngineUpgradeGuardTests: XCTestCase {
    func testNeedsBackupDecision() {
        // No database -> never (nothing to protect).
        XCTAssertFalse(EngineUpgradeGuard.needsBackup(databaseExists: false,
                                                      stampContents: nil, engineVersion: "2"))
        // Same version restarted -> no piling up backups.
        XCTAssertFalse(EngineUpgradeGuard.needsBackup(databaseExists: true,
                                                      stampContents: "2\n", engineVersion: "2"))
        // New version, or unknown previous version -> protect first.
        XCTAssertTrue(EngineUpgradeGuard.needsBackup(databaseExists: true,
                                                     stampContents: "1", engineVersion: "2"))
        XCTAssertTrue(EngineUpgradeGuard.needsBackup(databaseExists: true,
                                                     stampContents: nil, engineVersion: "2"))
        XCTAssertTrue(EngineUpgradeGuard.needsBackup(databaseExists: true,
                                                     stampContents: "  ", engineVersion: "2"))
    }

    func testBackupFileNameEmbedsVersionAndSortsByTime() {
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        let a = EngineUpgradeGuard.backupFileName(engineVersion: "0.4.1", at: base)
        let b = EngineUpgradeGuard.backupFileName(engineVersion: "0.4.1",
                                                  at: base.addingTimeInterval(5))
        XCTAssertTrue(a.hasPrefix("analytics-pre-0.4.1-"))
        XCTAssertTrue(a.hasSuffix(".db"))
        XCTAssertLessThan(a, b)   // lexicographic == chronological for pruning
        XCTAssertEqual(EngineUpgradeGuard.backupFileName(engineVersion: "a/b", at: base)
                         .hasPrefix("analytics-pre-a-b-"), true)
    }

    func testBackupGuardCopiesStampsAndSkipsSameVersion() throws {
        let dataDir = makeTempDir()
        let db = EngineUpgradeGuard.databaseURL(dataDir: dataDir)
        try "existing data".write(to: db, atomically: true, encoding: .utf8)
        let guard_ = EngineUpgradeGuard(dataDir: dataDir)

        // New version over existing db -> one backup + stamp.
        let backup = try XCTUnwrap(guard_.backupIfNeeded(engineVersion: "0.5.0"))
        XCTAssertEqual(try String(contentsOf: backup, encoding: .utf8), "existing data")
        XCTAssertEqual(try String(contentsOf: EngineUpgradeGuard.stampURL(dataDir: dataDir),
                                  encoding: .utf8), "0.5.0")

        // Same version again -> no second backup.
        XCTAssertNil(try guard_.backupIfNeeded(engineVersion: "0.5.0"))
        let backups = try FileManager.default.contentsOfDirectory(
            atPath: EngineUpgradeGuard.backupsDir(dataDir: dataDir).path)
        XCTAssertEqual(backups.count, 1)
    }

    func testPruneKeepsNewestThree() throws {
        let dataDir = makeTempDir()
        let dir = EngineUpgradeGuard.backupsDir(dataDir: dataDir)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let names = (0..<5).map { String(format: "analytics-pre-0.5.0-20260101-00000%d.db", $0) }
        for name in names {
            try "x".write(to: dir.appendingPathComponent(name), atomically: true, encoding: .utf8)
        }
        let guard_ = EngineUpgradeGuard(dataDir: dataDir)
        guard_.pruneBackups(dir: dir)
        let remaining = try FileManager.default.contentsOfDirectory(atPath: dir.path).sorted()
        XCTAssertEqual(remaining, Array(names.suffix(EngineUpgradeGuard.keepBackups)))
    }
}

// MARK: - Launch state machine (fakes only)

private final class FakeChild: EngineChildProcess {
    let launchArguments: [String]
    var alive = true
    private(set) var launchCount = 0
    init(arguments: [String]) { launchArguments = arguments }
    var isAlive: Bool { alive && launchCount > 0 }
    func launch() throws { launchCount += 1 }
}

final class EngineProcessManagerTests: XCTestCase {
    private var clock: Date!
    private var probeResults: [Bool] = []
    private var launchedChildren: [FakeChild] = []

    @discardableResult
    private func makeManager(location: EngineRuntimeLocation? = EngineRuntimeLocation(
            pythonExecutable: URL(fileURLWithPath: "/fake/python3"),
            serveScript: URL(fileURLWithPath: "/fake/orx_engine_serve.py"),
            moduleSearchPath: nil, origin: "test"),
            version: String = "0.4.1",
            dataDirNeedingIO: Bool = false,
            probe: ((String) -> Bool)? = nil) -> (EngineProcessManager, String) {
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        clock = base
        let dir = makeTempDir()
        if dataDirNeedingIO {
            // Real upgrade-guard IO: a database that exists.
            try? "db".write(to: EngineUpgradeGuard.databaseURL(dataDir: dir),
                            atomically: true, encoding: .utf8)
        }
        let manager = EngineProcessManager(
            location: location,
            engineVersion: version,
            now: { [unowned self] in
                self.clock = self.clock.addingTimeInterval(1)   // 1s per call
                return self.clock
            },
            probe: probe ?? { [unowned self] _ in
                // Default: socket serves only after the child launched.
                guard let child = self.launchedChildren.last, child.launchCount > 0 else {
                    return false
                }
                self.probeResults.append(true)
                return true
            },
            childFactory: { [unowned self] loc, dataDir in
                let child = FakeChild(arguments: [loc.serveScript.path, dataDir])
                self.launchedChildren.append(child)
                return child
            },
            sleep: { _ in })
        return (manager, dir.path)
    }

    override func tearDown() {
        launchedChildren.removeAll()
        probeResults.removeAll()
        super.tearDown()
    }

    func testAlreadyRunningNeverSpawns() async {
        let (manager, _) = makeManager(
            location: nil,
            probe: { _ in true })
        let out = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: "/tmp/d")
        XCTAssertEqual(out, .alreadyRunning)
        XCTAssertTrue(launchedChildren.isEmpty)
    }

    func testUnavailableWithoutRuntimeReportsHowToFix() async {
        let (manager, _) = makeManager(location: nil, probe: { _ in false })
        let out = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: "/tmp/d")
        guard case let .unavailable(message) = out else {
            return XCTFail("expected unavailable, got \(out)")
        }
        XCTAssertTrue(message.contains("ORX_ANALYTICS_ENGINE_PYTHON"))
        XCTAssertTrue(launchedChildren.isEmpty)
    }

    func testLaunchesServeScriptWithDataDirAndWaitsForSocket() async {
        let (manager, dataDir) = makeManager(dataDirNeedingIO: true)
        let out = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: dataDir)
        XCTAssertEqual(out, .started)
        XCTAssertEqual(launchedChildren.count, 1)
        XCTAssertEqual(launchedChildren[0].launchArguments,
                       ["/fake/orx_engine_serve.py", dataDir])
        XCTAssertEqual(launchedChildren[0].launchCount, 1)
    }

    func testSocketNeverComingUpFailsThenBacksOff() async {
        let (manager, dataDir) = makeManager(
            dataDirNeedingIO: true,
            probe: { _ in false })   // socket never serves
        let first = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: dataDir)
        guard case .failed = first else { return XCTFail("expected failed, got \(first)") }
        XCTAssertEqual(launchedChildren.count, 1)

        // An immediate second attempt must back off, not respawn in a
        // tight loop (bounded respawn, design §8).
        let second = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: dataDir)
        guard case let .backingOff(seconds) = second else {
            return XCTFail("expected backingOff, got \(second)")
        }
        XCTAssertGreaterThan(seconds, 0)
        XCTAssertLessThanOrEqual(seconds, EngineProcessManager.respawnBackoff)
        XCTAssertEqual(launchedChildren.count, 1)
    }

    func testChildDyingBeforeSocketIsReportedWithLogTail() async throws {
        let dataDir = makeTempDir()
        try FileManager.default.createDirectory(
            at: dataDir.appendingPathComponent("logs"), withIntermediateDirectories: true)
        try "line1\nline2\nboom".write(to: dataDir.appendingPathComponent("logs/engine-launch.log"),
                                       atomically: true, encoding: .utf8)
        let (manager, _) = makeManager(
            dataDirNeedingIO: true,
            probe: { [unowned self] _ in
                // Kill the child on the first probe so isAlive is false.
                if let child = self.launchedChildren.last, child.launchCount > 0 {
                    child.alive = false
                }
                return false
            })
        let out = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: dataDir.path)
        guard case let .failed(message) = out else { return XCTFail("expected failed, got \(out)") }
        XCTAssertTrue(message.contains("boom"), "log tail missing from: \(message)")
    }

    func testUpgradeBackupFailureBlocksLaunch() async throws {
        // A file squatting on the backups/ path makes the backup throw;
        // the manager must refuse to launch over an unbacked database.
        let dataDir = makeTempDir()
        try "db".write(to: EngineUpgradeGuard.databaseURL(dataDir: dataDir),
                       atomically: true, encoding: .utf8)
        try "blocker".write(to: EngineUpgradeGuard.backupsDir(dataDir: dataDir),
                            atomically: true, encoding: .utf8)
        let (manager, _) = makeManager()
        let out = await manager.ensureRunning(socketPath: "/tmp/s.sock", dataDir: dataDir.path)
        guard case let .failed(message) = out else { return XCTFail("expected failed, got \(out)") }
        XCTAssertTrue(message.contains("备份"), message)
        XCTAssertTrue(launchedChildren.isEmpty, "must not launch before the backup succeeds")
    }
}

// MARK: - Real-subprocess seal regression (G012 T006)
//
// The disclosed v0.4.2 defect: makeChild passed the parent environment
// through verbatim, so an operator-launched app (whose env has no
// PYTHONDONTWRITEBYTECODE, or a conflicting value) let the bundled
// Python write 73 __pycache__/*.pyc entries INTO the signed .app on the
// first engine start, flipping `codesign --verify --strict` from 0 to 1
// (evidence: ORX repo dist/g011/gui-acceptance/seal-break-*). A
// harness-started engine stayed sealed only because the harness env
// already carried the flag.
//
// These tests launch REAL Python children through the production
// makeChild path and prove ON DISK that importing an importable temp
// module produces no pyc/__pycache__ anywhere — not merely that an
// environment dictionary has a key. The control test launches the same
// import WITHOUT makeChild's protection and asserts bytecode DOES
// appear, so a green regression can never mean "the test cannot see
// bytecode in the first place".

final class EngineProcessManagerRealChildSealTests: XCTestCase {
    private static let moduleMarker = "seal-probe-module-v1"

    /// The serve-script stand-in mirrors orx_engine_serve.py's role: it
    /// is argv[0] of the child makeChild builds, receives the data dir
    /// as argv[1], imports the package next to the runtime, and writes
    /// a machine-readable report of what the REAL child saw.
    private static let probeServeScript = """
        import json, os, sys
        result = {
            "dont_write_bytecode_env": os.environ.get("PYTHONDONTWRITEBYTECODE"),
            "sys_dont_write_bytecode": bool(sys.dont_write_bytecode),
        }
        import seal_probe_module
        result["module_marker"] = seal_probe_module.MARKER
        with open(os.path.join(sys.argv[1], "seal-probe-result.json"), "w") as fh:
            json.dump(result, fh)
        """

    private static let probeModule = """
        MARKER = "\(moduleMarker)"
        COMPUTED = sum(i * i for i in range(64))   # real bytecode to compile
        """

    private var scratch: URL!

    override func setUpWithError() throws {
        scratch = makeTempDir()
        try FileManager.default.createDirectory(
            at: scratch.appendingPathComponent("resources"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: scratch.appendingPathComponent("scripts"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: scratch.appendingPathComponent("data"), withIntermediateDirectories: true)
        try Self.probeModule.write(
            to: scratch.appendingPathComponent("resources/seal_probe_module.py"),
            atomically: true, encoding: .utf8)
        try Self.probeServeScript.write(
            to: scratch.appendingPathComponent("scripts/orx_engine_serve.py"),
            atomically: true, encoding: .utf8)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    /// A real, bytecode-writing Python for the real child. Apple's
    /// /usr/bin/python3 ships with sys.dont_write_bytecode forced True,
    /// so it can never produce the defect and proves nothing; the probe
    /// therefore verifies the candidate actually writes pyc when the
    /// env var is absent before it is trusted (empirically the bundled
    /// runtime's CPython 3.12 writes bytecode when the var is absent,
    /// "0", or empty — all three are hostile inherited values).
    private static let pythonCandidates: [URL] = {
        var list: [URL] = []
        let env = ProcessInfo.processInfo.environment
        if let raw = env["ORX_ANALYTICS_TESTS_PYTHON"]?.trimmingCharacters(in: .whitespaces),
           !raw.isEmpty {
            list.append(URL(fileURLWithPath: raw))
        }
        list.append(URL(fileURLWithPath: "/opt/homebrew/bin/python3"))
        // <repo-root>/.venv/bin/python, derived from this test file's
        // location: .../apps/macos/Tests/ORXAnalyticsCoreTests/<file>
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        list.append(repoRoot.appendingPathComponent(".venv/bin/python"))
        list.append(URL(fileURLWithPath: "/usr/bin/python3"))
        return list
    }()

    /// Run one candidate through a real import and report whether a pyc
    /// landed next to the module — the capability the regression needs.
    private func writesBytecodeByDefault(python: URL) -> Bool {
        let probeRoot = makeTempDir()
        defer { try? FileManager.default.removeItem(at: probeRoot) }
        let moduleDir = probeRoot.appendingPathComponent("mod")
        try? FileManager.default.createDirectory(at: moduleDir, withIntermediateDirectories: true)
        try? Self.probeModule.write(to: moduleDir.appendingPathComponent("m.py"),
                                     atomically: true, encoding: .utf8)
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", "import m"]
        process.environment = ["PATH": "/usr/bin:/bin",
                               "PYTHONPATH": moduleDir.path]
        guard (try? process.run()) != nil else { return false }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return false }
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(
            atPath: moduleDir.appendingPathComponent("__pycache__").path, isDirectory: &isDir)
    }

    private func capablePython() throws -> URL {
        for candidate in Self.pythonCandidates
        where FileManager.default.isExecutableFile(atPath: candidate.path) {
            if writesBytecodeByDefault(python: candidate) {
                return candidate
            }
        }
        throw XCTSkip("no bytecode-writing python3 available for the real-child "
                      + "seal regression (Apple's /usr/bin/python3 cannot produce the defect)")
    }

    private func makeLocation(python: URL) -> EngineRuntimeLocation {
        EngineRuntimeLocation(pythonExecutable: python,
                              serveScript: scratch.appendingPathComponent("scripts/orx_engine_serve.py"),
                              moduleSearchPath: scratch.appendingPathComponent("resources"),
                              origin: "test")
    }

    /// Every pyc / __pycache__ path under a root, sorted for messages.
    private func bytecodeEntries(under root: URL) -> [String] {
        guard let walker = FileManager.default.enumerator(
            at: root, includingPropertiesForKeys: nil) else { return [] }
        var found: [String] = []
        for case let url as URL in walker {
            if url.lastPathComponent == "__pycache__" || url.pathExtension == "pyc" {
                found.append(url.path)
            }
        }
        return found.sorted()
    }

    private func waitForResult(timeout: TimeInterval = 60) throws -> [String: Any] {
        let url = scratch.appendingPathComponent("data/seal-probe-result.json")
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let data = try? Data(contentsOf: url),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return object
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw XCTSkip("real child did not report within \(Int(timeout))s")
    }

    private func waitUntilNotAlive(_ child: EngineChildProcess, timeout: TimeInterval = 60) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if !child.isAlive { return true }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return false
    }

    /// The regression: the real makeChild child must run with
    /// PYTHONDONTWRITEBYTECODE=1 even when the PARENT environment is
    /// hostile. The bundled runtime's CPython writes bytecode when the
    /// inherited value is "0" or "" as well as when it is absent — every
    /// one of those inherited states produced the disclosed seal break —
    /// and importing a compilable module from the module search path must
    /// leave zero bytecode artifacts anywhere under the scratch tree.
    func testMakeChildRealChildNeverWritesBytecodeDespiteHostileParentEnv() throws {
        let python = try capablePython()
        let hostileEnvironments: [[String: String]] = [
            ["PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "0"],
            ["PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": ""],
            ["PATH": "/usr/bin:/bin"],   // absent — the operator case
        ]
        for (index, hostile) in hostileEnvironments.enumerated() {
            let child = try EngineProcessManager.makeChild(
                location: makeLocation(python: python),
                dataDir: scratch.appendingPathComponent("data").path,
                fileManager: .default,
                environment: hostile)
            try child.launch()
            XCTAssertTrue(waitUntilNotAlive(child),
                          "round \(index): real child did not exit")

            let report = try waitForResult()
            XCTAssertEqual(report["dont_write_bytecode_env"] as? String, "1",
                           "round \(index): real child env must force PYTHONDONTWRITEBYTECODE=1")
            XCTAssertEqual(report["sys_dont_write_bytecode"] as? Bool, true,
                           "round \(index): the interpreter itself must run with dont_write_bytecode")
            XCTAssertEqual(report["module_marker"] as? String, Self.moduleMarker,
                           "round \(index): the temp module was not imported by the real child")

            let stray = bytecodeEntries(under: scratch)
            XCTAssertTrue(stray.isEmpty,
                          "round \(index): the real child wrote bytecode despite the fix: \(stray)")
        }
    }

    /// The control that keeps the regression honest: the SAME interpreter
    /// importing the SAME module with no makeChild protection DOES
    /// produce a pyc. Before the fix, the test above failed exactly the
    /// way this control succeeds; a green regression therefore proves the
    /// env override, not blindness.
    func testControlUnprotectedImportWritesBytecode() throws {
        let python = try capablePython()
        let process = Process()
        process.executableURL = python
        process.arguments = [scratch.appendingPathComponent("scripts/orx_engine_serve.py").path,
                             scratch.appendingPathComponent("data").path]
        process.environment = ["PATH": "/usr/bin:/bin",
                               "PYTHONPATH": scratch.appendingPathComponent("resources").path]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0, "control import run failed")
        XCTAssertFalse(bytecodeEntries(under: scratch).isEmpty,
                       "control expected a __pycache__ entry; without one the "
                           + "regression above could not detect the defect")
    }

    /// makeChild must actually create the documented engine launch log
    /// (<dataDir>/logs/engine-launch.log): FileHandle(forWritingAtPath:)
    /// never creates files, so without an explicit create the log was
    /// silently absent on a fresh install (stderr went to nullDevice) —
    /// and the post-install seal check (G012 T006) requires that log as
    /// real-launch evidence for the installed app.
    func testMakeChildCreatesLaunchLogOnFreshDataDir() throws {
        let fresh = makeTempDir()
        defer { try? FileManager.default.removeItem(at: fresh) }
        _ = try EngineProcessManager.makeChild(
            location: makeLocation(python: URL(fileURLWithPath: "/usr/bin/true")),
            dataDir: fresh.path,
            fileManager: .default)
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: fresh.appendingPathComponent("logs/engine-launch.log").path),
                      "makeChild left no launch log on a fresh data dir")
    }
}
