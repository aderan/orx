import Foundation
import ORXAnalyticsCore

/// Xcode-free smoke self-check, run by the executable itself when started
/// with `--smoke-test` (scripts/smoke_macos_app.sh). It exercises the
/// REAL transport + client + cache + frozen formatting rules against a
/// local `EngineSimulator` — no live engine, no real projects, no
/// notifications (CONTRIBUTING: 测试用合成来源).
///
/// Runs before NSApplication starts, uses only its own threads, and
/// exits the process with 0 (pass) / 1 (fail).
enum SmokeRunner {
    static func runAndReport() -> Bool {
        var results: [(String, Bool, String)] = []
        results.append(("cache-roundtrip", checkCacheRoundtrip(), nil2s(cacheErr)))
        results.append(("negotiation-compatible", checkNegotiationCompatible(), nil2s(negErr)))
        results.append(("negotiation-incompatible", checkNegotiationIncompatible(), nil2s(incErr)))
        results.append(("overview-fetch", checkOverviewFetch(), nil2s(ovErr)))
        results.append(("metrics-freeze-rules", checkMetricsRules(), nil2s(mxErr)))
        results.append(("detail-fetch-two-calibers", checkDetailFetch(), nil2s(dtErr)))

        var allPass = true
        for (name, pass, note) in results {
            let mark = pass ? "ok" : "FAIL"
            print("SMOKE \(mark)  \(name)\(note.isEmpty ? "" : " — \(note)")")
            allPass = allPass && pass
        }
        print(allPass ? "SMOKE RESULT: PASS" : "SMOKE RESULT: FAIL")
        return allPass
    }

    private static func nil2s(_ s: String?) -> String { s ?? "" }
    private static var cacheErr: String?
    private static var negErr: String?
    private static var incErr: String?
    private static var ovErr: String?
    private static var mxErr: String?
    private static var dtErr: String?

    // MARK: checks

    private static func checkCacheRoundtrip() -> Bool {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("orx-smoke-\(UUID().uuidString)",
                                    isDirectory: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = OverviewCacheStore(directory: dir)
        guard let envelope = decodeOverview(EngineSimulator.encode(
            EngineSimulator.overviewResponse(activeRuns: 2, attention: 1))) else {
            cacheErr = "fixture overview failed to decode"
            return false
        }
        store.save(CachedOverview(envelope: envelope,
                                  cachedAt: Date(timeIntervalSince1970: 1_000)))
        guard let loaded = store.load() else {
            cacheErr = "cache load returned nil after save"
            return false
        }
        guard loaded.envelope.summary.activeRuns == 2,
              loaded.cachedAt.timeIntervalSince1970 == 1_000 else {
            cacheErr = "cache roundtrip changed the payload"
            return false
        }
        store.clear()
        if store.load() != nil {
            cacheErr = "clear() did not remove the cache"
            return false
        }
        return true
    }

    private static func checkNegotiationCompatible() -> Bool {
        let sim: EngineSimulator
        do {
            sim = try startSimulator { line in
                let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
                guard let op = obj?["op"] as? String else {
                    return EngineSimulator.encode([
                        "op": NSNull(), "ok": false, "protocol_version": 1,
                        "error": ["code": "invalid_request",
                                  "message": "request line is not valid JSON"],
                    ])
                }
                if op == "health" {
                    return EngineSimulator.encode(EngineSimulator.healthResponse(version: 1))
                }
                return EngineSimulator.encode(["op": op, "ok": true,
                                               "protocol_version": 1])
            }
        } catch {
            negErr = "simulator start failed: \(error)"
            return false
        }
        defer { sim.stop() }
        let client = EngineSocketClient(socketPath: sim.socketPath)
        let outcome = client.negotiate()
        guard case let .compatible(version) = outcome, version == 1 else {
            negErr = "expected compatible(1), got \(outcome)"
            return false
        }
        // every request must carry protocol_version (engine contract)
        guard let first = sim.receivedRequests.first,
              let obj = try? JSONSerialization.jsonObject(with: first) as? [String: Any],
              obj["protocol_version"] as? Int == 1,
              obj["op"] as? String == "health" else {
            negErr = "request missing protocol_version/op"
            return false
        }
        return true
    }

    private static func checkNegotiationIncompatible() -> Bool {
        let sim: EngineSimulator
        do {
            sim = try startSimulator { line in
                _ = line
                return EngineSimulator.encode(
                    EngineSimulator.unsupportedProtocolResponse(requested: 1,
                                                                supported: 99))
            }
        } catch {
            incErr = "simulator start failed: \(error)"
            return false
        }
        defer { sim.stop() }
        let client = EngineSocketClient(socketPath: sim.socketPath)
        let outcome = client.negotiate()
        guard case let .incompatible(requested, supported) = outcome,
              requested == 1, supported == 99 else {
            incErr = "expected incompatible(1, 99), got \(outcome)"
            return false
        }
        // fail-fast: the next call must not hit the socket again
        let before = sim.receivedRequests.count
        _ = try? client.fetchOverview()
        if sim.receivedRequests.count != before {
            incErr = "client kept querying after incompatibility"
            return false
        }
        // the user-facing report names both versions
        let text = EngineFailure.incompatible(requested: 1, supported: 99,
                                              message: "").localizedDescription
        guard text.contains("v1"), text.contains("v99") else {
            incErr = "incompatibility report does not name both versions"
            return false
        }
        return true
    }

    private static func checkOverviewFetch() -> Bool {
        let sim: EngineSimulator
        do {
            sim = try startSimulator { line in
                let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
                switch obj?["op"] as? String {
                case "health":
                    return EngineSimulator.encode(EngineSimulator.healthResponse())
                case "overview":
                    return EngineSimulator.encode(
                        EngineSimulator.overviewResponse(activeRuns: 3, attention: 2))
                case "refresh.request":
                    return EngineSimulator.encode([
                        "op": "refresh.accepted", "ok": true, "protocol_version": 1,
                        "job_id": "job-1", "merged_with": NSNull(),
                        "projects": NSNull(), "rejected": []])
                default:
                    return EngineSimulator.encode([
                        "op": NSNull(), "ok": false, "protocol_version": 1,
                        "error": ["code": "unknown_op", "message": "no",
                                  "supported": ["overview"]]])
                }
            }
        } catch {
            ovErr = "simulator start failed: \(error)"
            return false
        }
        defer { sim.stop() }
        let client = EngineSocketClient(socketPath: sim.socketPath)
        do {
            _ = try client.requestRefresh(reason: "panel-open")
            let envelope = try client.fetchOverview()
            let ok = envelope.summary.activeRuns == 3
                && envelope.summary.attention == 2
                && envelope.active.first?.status.display == "执行中"
            if !ok { ovErr = "decoded overview does not match fixture" }
            return ok
        } catch {
            ovErr = "\(error)"
            return false
        }
    }

    private static func checkMetricsRules() -> Bool {
        let rules: [(String, Bool)] = [
            ("unknown token -> —", MetricsFormat.tokenText(nil) == "—"),
            ("recorded zero -> 0", MetricsFormat.tokenText(0) == "0"),
            ("value renders with fixed 3-digit grouping", MetricsFormat.tokenText(1200) == "1,200"
                && MetricsFormat.tokenText(1_234_567) == "1,234,567"),
            ("two calibers stay separate", UsageCalibers(
                recordedTotal: 100, nativePreviewTotal: 40).rows.count == 2
                && UsageCalibers(recordedTotal: 100, nativePreviewTotal: 40)
                    .rows.map(\.text) == ["100", "40"]),
            ("unknown caliber row is —", UsageCalibers(recordedTotal: nil).rows[0].text == "—"),
        ]
        for (name, pass) in rules where !pass {
            mxErr = name
            return false
        }
        return true
    }

    /// T009 detail self-check: runs.get + tasks.get against the canned
    /// simulator payloads — the run's books decode, the native preview
    /// declares itself a separate caliber, unknown tokens stay "—".
    private static func checkDetailFetch() -> Bool {
        let sim: EngineSimulator
        do {
            sim = try startSimulator { line in
                let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
                switch obj?["op"] as? String {
                case "health":
                    return EngineSimulator.encode(EngineSimulator.healthResponse())
                case "runs.get":
                    return EngineSimulator.encode(
                        EngineSimulator.runDetailResponse(project: "/tmp/proj0",
                                                          runID: "R001"))
                case "tasks.get":
                    return EngineSimulator.encode(
                        EngineSimulator.taskDetailResponse(project: "/tmp/proj0",
                                                          runID: "R001",
                                                          revisionID: 101,
                                                          taskID: "T001"))
                default:
                    return EngineSimulator.encode([
                        "op": NSNull(), "ok": false, "protocol_version": 1,
                        "error": ["code": "unknown_op", "message": "no",
                                  "supported": ["runs.get"]]])
                }
            }
        } catch {
            dtErr = "simulator start failed: \(error)"
            return false
        }
        defer { sim.stop() }
        let client = EngineSocketClient(socketPath: sim.socketPath)
        do {
            let run = try client.fetchRunDetail(project: "/tmp/proj0", runID: "R001")
            guard run.found, let usage = run.usage else {
                dtErr = "run detail missing usage"
                return false
            }
            guard usage.books?.planner != nil,
                  usage.books?.unattributedWithinRun != nil else {
                dtErr = "run books incomplete"
                return false
            }
            // two calibers side by side, structurally separate
            let calibers = DetailCalibers(usage: run.usage,
                                          preview: run.nativePreview)
            guard run.nativePreview?.neverSumWith == "orx_recorded",
                  calibers.orxRows.count == 3, calibers.nativeRows.count == 6,
                  calibers.orxRows.map(\.text).contains("—") else {
                dtErr = "caliber separation broken"
                return false
            }
            let task = try client.fetchTaskDetail(project: "/tmp/proj0",
                                                  runID: "R001",
                                                  revisionID: 101, taskID: "T001")
            guard task.found,
                  task.usage?.books?.worker?.attempts == 1,
                  task.usage?.attempts.count == 2,
                  task.verifications.first?.passed == true else {
                dtErr = "task detail evidence incomplete"
                return false
            }
            // identity params on the wire
            let requests = sim.receivedRequests.compactMap {
                (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any]
            }
            guard let taskReq = requests.first(where: {
                $0["op"] as? String == "tasks.get" }),
                  taskReq["revision_id"] as? Int == 101 else {
                dtErr = "tasks.get params not sent as engine expects"
                return false
            }
            return true
        } catch {
            dtErr = "\(error)"
            return false
        }
    }

    // MARK: helpers

    private static func startSimulator(_ handler: @escaping EngineSimulator.Handler) throws -> EngineSimulator {
        let sim = EngineSimulator(handler: handler)
        try sim.start()
        return sim
    }

    private static func decodeOverview(_ data: Data) -> OverviewEnvelope? {
        try? JSONDecoder().decode(OverviewEnvelope.self, from: data)
    }
}
