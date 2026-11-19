import XCTest
@testable import ORXAnalyticsCore

/// Wire-contract tests for `runs.get` / `tasks.get` payloads. The JSON
/// below mirrors EXACTLY what the Python query layer serializes
/// (`query_model.runs_get` / `tasks_get` + `usage_readmodel` blocks):
/// `cached_input` (not cached_tokens), per-field `missing_fields`
/// dicts, `coverage` blocks, INTEGER revision ids, 0/1 `passed`, and a
/// native preview that carries `never_sum_with`. If the Swift models
/// drift from the engine's wire format these tests go red before the
/// UI silently shows "—" everywhere.
final class DetailModelsTests: XCTestCase {

    // MARK: - runs.get

    func testRunDetailDecodesFullWirePayload() throws {
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Self.runDetailJSON)
        XCTAssertEqual(envelope.header.op, "runs.get")
        XCTAssertTrue(envelope.found)
        XCTAssertEqual(envelope.run?.progress?.revisionID, 102)
        XCTAssertEqual(envelope.run?.progress?.revision, 2)
        XCTAssertEqual(envelope.run?.consumption?.tokens?.cachedTokens, nil)
        XCTAssertEqual(envelope.run?.consumption?.observedTotal, 1200)
        // per-field missing dict, not a scalar
        XCTAssertEqual(envelope.run?.consumption?.missingFields?["output_tokens"], 1)
        XCTAssertEqual(envelope.run?.consumption?.coverage?.observed, 3)
        XCTAssertEqual(envelope.run?.consumption?.coverage?.attempts, 5)
        XCTAssertEqual(envelope.revisions.count, 2)
        XCTAssertEqual(envelope.revisions.last?.revisionID, 102)
        XCTAssertEqual(envelope.tasks.count, 2)
        XCTAssertEqual(envelope.tasks.first?.revisionID, 101)
        XCTAssertEqual(envelope.tasks.first?.status.display, "完成")
        XCTAssertEqual(envelope.tasks.last?.failureReason, "timeout")
        XCTAssertEqual(envelope.tasks.last?.attemptCount, 2)
    }

    func testRunUsageBooksDecodeWithRolesAndNotes() throws {
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Self.runDetailJSON)
        let usage = try XCTUnwrap(envelope.usage)
        XCTAssertEqual(usage.caliber, "orx_recorded")
        XCTAssertEqual(usage.books?.tasks?.attempts, 5)
        XCTAssertEqual(usage.books?.planner?.revisionIDs, [nil])
        XCTAssertEqual((usage.books?.controller?.note ?? "").isEmpty, false)
        XCTAssertEqual(usage.books?.unattributedWithinRun?.attempts, 1)
        XCTAssertEqual(usage.perTask?.first?.taskID, "T001")
        XCTAssertEqual(usage.perTask?.first?.revision, 1)
        XCTAssertEqual(usage.perRevision?.count, 2)
        XCTAssertEqual(usage.normalizedProcessing?.version, 1)
        XCTAssertEqual(usage.normalizedProcessing?.partial, true)
    }

    func testNativePreviewIsASeparateCaliberAndDeclaresIt() throws {
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Self.runDetailJSON)
        let preview = try XCTUnwrap(envelope.nativePreview)
        XCTAssertEqual(preview.caliber, "native_session_preview")
        XCTAssertEqual(preview.neverSumWith, "orx_recorded")
        XCTAssertEqual(preview.union?.tokens?.inputTokens, 600)
        XCTAssertEqual(preview.union?.sessions?.count, 2)
        XCTAssertEqual(preview.linkedAttempts?.first?.treeTotals?.turns, 10)
        // controller own usage is the model_usage REQUEST caliber: deduped
        // per request identity, cache a subset of input, processed =
        // input + output — never turn_usage, never descendants
        let own = try XCTUnwrap(preview.controllerSessions?.first?.ownUsage)
        XCTAssertEqual(own.caliber, "model_usage_requests")
        XCTAssertEqual(own.requests, 2)
        XCTAssertEqual(own.inputTokens, 4_000_000)
        XCTAssertEqual(own.cacheRead, 3_700_000)
        XCTAssertEqual(own.processedTokens, 4_060_000)
        XCTAssertEqual(own.statusCounts?["completed"], 2)
        XCTAssertEqual(own.errorRowsZeroTokens, 0)
        XCTAssertEqual(preview.controllerSessions?.first?.directoryMatchesProject, true)
        // the per-run verdict names the session; no token number at the
        // preview's top level
        let verdict = try XCTUnwrap(preview.controllerForRun)
        XCTAssertEqual(verdict.status, "attributed")
        XCTAssertEqual(verdict.sessionIDs, ["root-sess"])
        XCTAssertNil(verdict.reason)
    }

    func testRunNotFoundStillDecodes() throws {
        let json = """
        {"op": "runs.get", "ok": true, "protocol_version": 1,
         "generated_at": "2026-10-16T10:00:00+00:00",
         "project": "/tmp/p", "run_id": "R404", "found": false,
         "reason": "run not found",
         "snapshots": {}, "coverage": {}, "warnings": []}
        """
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Data(json.utf8))
        XCTAssertFalse(envelope.found)
        XCTAssertEqual(envelope.reason, "run not found")
        XCTAssertNil(envelope.run)
    }

    // MARK: - tasks.get

    func testTaskDetailDecodesAttemptsWithEvidenceAndVerifications() throws {
        let envelope = try JSONDecoder().decode(TaskDetailEnvelope.self,
                                                from: Self.taskDetailJSON)
        XCTAssertEqual(envelope.header.op, "tasks.get")
        XCTAssertTrue(envelope.found)
        XCTAssertEqual(envelope.task?.status?.display, "失败")
        XCTAssertEqual(envelope.task?.failureReason, "timeout")
        let usage = try XCTUnwrap(envelope.usage)
        XCTAssertEqual(usage.books?.worker?.attempts, 2)
        XCTAssertEqual(usage.books?.verifier?.attempts, 1)
        XCTAssertEqual(usage.attempts.count, 3)
        let first = usage.attempts.first
        XCTAssertEqual(first?.model, "glm-4.7")
        XCTAssertEqual(first?.observation?.source, "zcode_db")
        XCTAssertEqual(first?.observation?.tokens?.inputTokens, 200)
        XCTAssertEqual(first?.sessionLink?.sessionID, "sess-1")
        XCTAssertEqual(first?.sessionLink?.confidence, "high")
        let missingAttempt = usage.attempts[1]
        XCTAssertEqual(missingAttempt.usageMissingReason, "attempt still running")
        XCTAssertNil(missingAttempt.observation)
        // SQLite INTEGER booleans: 0 -> false
        XCTAssertEqual(envelope.verifications.first?.passed, false)
        XCTAssertEqual(envelope.verifications.first?.exitCode, 1)
        XCTAssertEqual(envelope.retry?.recovered, false)
        XCTAssertEqual(envelope.retry?.successorAttemptIDs, [5])
    }

    // MARK: - calibers never combine

    func testDetailCalibersExposeTwoIndependentGroupsAndNoMergedRow() {
        let calibers = DetailCalibers(usage: decodedRunDetail()?.usage,
                                      preview: decodedRunDetail()?.nativePreview)
        XCTAssertEqual(calibers.orxRows.map(\.label),
                       ["输入", "输出", "缓存（cached_input）"])
        // G012：输入 1000 按统一格式器分组显示为 "1,000"
        XCTAssertEqual(calibers.orxRows.map(\.text), ["1,000", "200", "—"])
        XCTAssertEqual(calibers.nativeRows.map(\.label).first, "会话轮次")
        XCTAssertEqual(calibers.nativeRows.map(\.text),
                       ["12", "600", "120", "30", "60", "6"])
        // The two groups may describe the same tokens; nothing combines
        // them: there is no row and no accessor producing 1600 or 1720.
        let allTexts = calibers.orxRows.map(\.text) + calibers.nativeRows.map(\.text)
        XCTAssertFalse(allTexts.contains("1600"))
        XCTAssertFalse(allTexts.contains { $0.contains("+") })
    }

    func testDetailCalibersUnknownFieldsStayDash() throws {
        let calibers = DetailCalibers(usage: nil, preview: nil)
        XCTAssertEqual(calibers.orxRows.map(\.text), ["—", "—", "—"])
        XCTAssertEqual(calibers.nativeRows.map(\.text).prefix(1), ["—"])
    }

    // MARK: - controller native preview presentation (G012 用户反馈 5)

    func testControllerPresentationShowsTokensSourceAndBasis() throws {
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Self.runDetailJSON)
        let p = ControllerUsagePresentation.build(preview: envelope.nativePreview)
        XCTAssertEqual(p.statusText, "已按唯一证据归属本执行")
        XCTAssertEqual(p.sessionText, "root-sess")
        // unified 3-digit grouping via the shared formatter
        XCTAssertEqual(p.lines.map(\.text),
                       ["2", "4,000,000", "60,000", "3,700,000", "4,060,000"])
        XCTAssertEqual(p.lines.map(\.label),
                       ["请求数", "输入", "输出", "缓存读（input 子集）", "处理量（input＋output）"])
        XCTAssertTrue(p.sourceText?.hasPrefix("zcode model_usage mirror") == true)
        XCTAssertEqual(p.updatedMS, 1_790_000_000_900)
        XCTAssertEqual(p.usageLastActivityMS, 1_790_000_000_500)
        XCTAssertTrue(p.evidenceText?.contains("root ancestor") == true)
        XCTAssertTrue(p.directoryText?.contains("与项目根一致") == true)
        XCTAssertTrue(p.hasAttributedUsage)
        XCTAssertNil(p.caveatText)
        // the controller request caliber is NEVER merged with the ORX
        // caliber: the fixture's ORX input is 1,000, so any view that
        // added them would render 4,001,000 — no presentation row does
        let texts = p.lines.map(\.text)
        XCTAssertFalse(texts.contains("4,001,000"),
                       "controller 与 ORX 两口径绝不相加")
    }

    func testControllerPresentationUnattributedShowsReasonNotZeros() throws {
        let envelope = try JSONDecoder().decode(RunDetailEnvelope.self,
                                                from: Self.runDetailJSON)
        let preview = try XCTUnwrap(envelope.nativePreview)
        // shared across runs: evidence exists but is not unique to R001
        let sharedJSON = """
        {"run_id": "R001", "status": "unattributed",
         "session_ids": ["root-sess"],
         "reason": "child sessions link to attempts of 2 different runs ['R001', 'R002']; controller usage cannot be attributed to one run and is not split by guesswork",
         "note": null}
        """
        let verdict = try JSONDecoder().decode(ControllerForRun.self,
                                               from: Data(sharedJSON.utf8))
        let rebuilt = NativePreview(
            caliber: preview.caliber, project: preview.project,
            runID: preview.runID, snapshotID: preview.snapshotID,
            asOf: preview.asOf, asOfSessionsMS: preview.asOfSessionsMS,
            linkedAttempts: preview.linkedAttempts, union: preview.union,
            sharedSessionTrees: preview.sharedSessionTrees,
            subtreeConflicts: preview.subtreeConflicts,
            controllerSessions: preview.controllerSessions,
            controllerForRun: verdict,
            neverSumWith: preview.neverSumWith, note: preview.note)
        let p = ControllerUsagePresentation.build(preview: rebuilt)
        XCTAssertEqual(p.statusText, "无法唯一归属（跨执行共享，不摊分）")
        XCTAssertTrue(p.reasonText?.contains("different runs") == true)
        XCTAssertEqual(p.sessionText, "root-sess")
        // the session's own cumulative total is a FACT and is shown — but
        // labeled as shared, never attributed to this run, never prorated
        XCTAssertTrue(p.hasSessionUsage)
        XCTAssertFalse(p.hasAttributedUsage)
        XCTAssertEqual(p.lines.map(\.text),
                       ["2", "4,000,000", "60,000", "3,700,000", "4,060,000"])
        let caveat = try XCTUnwrap(p.sharedCaveatText)
        XCTAssertTrue(caveat.contains("跨多个 run 共享"), caveat)
        XCTAssertTrue(caveat.contains("绝不按 run 摊分"), caveat)
        // no unknown field is ever zero-filled
        XCTAssertEqual(p.lines.map(\.label),
                       ["请求数", "输入", "输出", "缓存读（input 子集）", "处理量（input＋output）"])
    }

    func testControllerPresentationUnboundAndUnknownStayExplicit() {
        let unboundPreview = NativePreview(
            caliber: "native_session_preview", project: nil, runID: nil,
            snapshotID: nil, asOf: nil, asOfSessionsMS: nil,
            linkedAttempts: nil, union: nil, sharedSessionTrees: nil,
            subtreeConflicts: nil, controllerSessions: [],
            controllerForRun: ControllerForRun(
                runID: "R001", status: "host_session_unbound",
                sessionIDs: [],
                reason: "none of this run's zcode attempts is bound to a session",
                note: nil),
            neverSumWith: "orx_recorded", note: nil)
        let p = ControllerUsagePresentation.build(preview: unboundPreview)
        XCTAssertEqual(p.statusText, "未绑定会话（host_session_unbound）")
        XCTAssertTrue(p.reasonText?.contains("bound to a session") == true)
        XCTAssertTrue(p.lines.isEmpty)
        // nothing at all: unknown, never a guessed number
        let none = ControllerUsagePresentation.build(preview: nil)
        XCTAssertEqual(none.statusText, "未知")
        XCTAssertTrue(none.lines.isEmpty)
        XCTAssertFalse(none.hasAttributedUsage)
        XCTAssertNil(none.updatedMS)
    }

    func testControllerPresentationFlagsErrorZeroPlaceholderAndSettling() throws {
        let own = RequestUsage(caliber: "model_usage_requests",
                               source: "zcode model_usage mirror",
                               requests: 3, inputTokens: 100, outputTokens: 10,
                               reasoningTokens: nil, cacheRead: 90,
                               cacheCreation: nil, providerTotal: nil,
                               computedTotal: nil, processedTokens: 110,
                               processedRule: nil,
                               statusCounts: ["completed": 2, "error": 1],
                               settlingRequestRows: 1,
                               errorRowsZeroTokens: 1,
                               missingFields: ["input_tokens": 1],
                               rowsMissingProcessed: 1,
                               lastActivityMS: 1_790_000_000_500)
        let session = ControllerSession(sessionID: "root-sess",
                                        directory: "/tmp/projA",
                                        directoryMatchesProject: false,
                                        createdMS: 1, updatedMS: 2,
                                        ownUsage: own,
                                        runAttribution: nil)
        let preview = NativePreview(caliber: "native_session_preview",
                                    project: nil, runID: nil, snapshotID: nil,
                                    asOf: nil, asOfSessionsMS: nil,
                                    linkedAttempts: nil, union: nil,
                                    sharedSessionTrees: nil,
                                    subtreeConflicts: nil,
                                    controllerSessions: [session],
                                    controllerForRun: ControllerForRun(
                                        runID: "R001", status: "attributed",
                                        sessionIDs: ["root-sess"],
                                        reason: nil, note: nil),
                                    neverSumWith: "orx_recorded", note: nil)
        let p = ControllerUsagePresentation.build(preview: preview)
        let caveat = try XCTUnwrap(p.caveatText)
        XCTAssertTrue(caveat.contains("错误请求 1 行为零占位"), caveat)
        XCTAssertTrue(caveat.contains("未完成请求 1 行"), caveat)
        XCTAssertTrue(caveat.contains("input_tokens 缺 1 行"), caveat)
        // a foreign directory is a recorded fact, never a veto
        XCTAssertTrue(p.directoryText?.contains("与项目根不同") == true)
    }

    private func decodedRunDetail() -> RunDetailEnvelope? {
        try? JSONDecoder().decode(RunDetailEnvelope.self, from: Self.runDetailJSON)
    }

    // MARK: - fixtures (mirror the Python wire format exactly)

    static let runDetailJSON: Data = {
        let consumption = """
        {"caliber": "orx_recorded",
         "tokens": {"input_tokens": 1000, "output_tokens": 200, "cached_input": null},
         "observed_total": 1200,
         "missing_fields": {"input_tokens": 0, "output_tokens": 1, "cached_input": 3},
         "accuracy": {"exact": 2, "estimated": 1, "unknown": 0},
         "coverage": {"observed": 3, "attempts": 5, "missing_recorded": 1, "unassessed": 1}}
        """
        // inner fields of a usage book (no braces) so per-task /
        // per-revision entries can append their identity keys
        let book = { (attempts: Int, ids: String, revs: String, note: String?) in
            var s = """
            "attempts": \(attempts), "attempt_ids": [\(ids)], "revision_ids": [\(revs)],
             "coverage": {"observed": \(attempts), "attempts": \(attempts), "missing_recorded": 0, "unassessed": 0},
             "tokens": {"input_tokens": 100, "output_tokens": 20, "cached_input": null},
             "missing_fields": {"input_tokens": 0, "output_tokens": 0, "cached_input": 0},
             "accuracy": {"exact": 1, "estimated": 0, "unknown": 0},
             "as_of": "2026-10-16T09:59:30+00:00"
            """
            if let note { s += ", \"note\": \"\(note)\"" }
            return s
        }
        let json = """
        {"op": "runs.get", "ok": true, "protocol_version": 1,
         "generated_at": "2026-10-16T10:00:00+00:00",
         "project": "/tmp/projA", "run_id": "R001", "found": true,
         "run": {
           "project": "/tmp/projA", "run_id": "R001",
           "identity": {"project": "/tmp/projA", "run_id": "R001"},
           "goal": {"id": "G001", "objective": "交付面板",
                    "status": {"raw": "active", "known": true, "display": "进行中"}},
           "status": {"raw": "running", "known": true, "display": "执行中"},
           "started_at": "2026-10-16T09:00:00+00:00",
           "completed_at": null, "updated_at": "2026-10-16T09:59:00+00:00",
           "created_at": "2026-10-16T09:00:00+00:00",
           "time_basis": {"field": "started_at", "value": "2026-10-16T09:00:00+00:00"},
           "progress": {"completed": 2, "total": 5, "revision_id": 102, "revision": 2,
                        "basis": "tasks of the current (highest) revision only"},
           "consumption": \(consumption),
           "data_as_of": "2026-10-16T09:59:30+00:00",
           "schema_version": "11"},
         "revisions": [
           {"revision_id": 101, "revision": 1, "status": "superseded",
            "created_at": "2026-10-16T09:05:00+00:00", "task_count": 3},
           {"revision_id": 102, "revision": 2, "status": "active",
            "created_at": "2026-10-16T09:30:00+00:00", "task_count": 5}],
         "tasks": [
           {"project": "/tmp/projA", "run_id": "R001",
            "identity": {"project": "/tmp/projA", "run_id": "R001",
                         "revision_id": 101, "task_id": "T001"},
            "revision_id": 101, "revision": 1, "task_id": "T001",
            "objective": "旧计划任务",
            "status": {"raw": "passed", "known": true, "display": "完成"},
            "failure_reason": null, "attempt_count": 2, "consumption": \(consumption)},
           {"project": "/tmp/projA", "run_id": "R001",
            "identity": {"project": "/tmp/projA", "run_id": "R001",
                         "revision_id": 102, "task_id": "T002"},
            "revision_id": 102, "revision": 2, "task_id": "T002",
            "objective": "当前失败任务",
            "status": {"raw": "failed", "known": true, "display": "失败"},
            "failure_reason": "timeout", "attempt_count": 2, "consumption": \(consumption)}],
         "usage": {
           "caliber": "orx_recorded", "project": "/tmp/projA", "run_id": "R001",
           "revision_scope": "all revisions via attempts.run_id",
           "snapshot_id": 7, "as_of": "2026-10-16T09:59:30+00:00",
           "tokens": {"input_tokens": 1000, "output_tokens": 200, "cached_input": null},
           "missing_fields": {"input_tokens": 0, "output_tokens": 1, "cached_input": 3},
           "accuracy": {"exact": 2, "estimated": 1, "unknown": 0},
           "coverage": {"observed": 3, "attempts": 5, "missing_recorded": 1, "unassessed": 1},
           "books": {
             "tasks": {\(book(5, "1, 2, 3, 4, 5", "101, 102", nil))},
             "planner": {\(book(1, "6", "null", nil))},
             "controller": {\(book(0, "", "", "ORX-recorded controller attempts only"))},
             "unattributed_within_run": {\(book(1, "7", "101", "never prorated"))}},
           "per_task": [{\(book(2, "1, 2", "101", nil)), "revision_id": 101, "revision": 1, "task_id": "T001"}],
           "per_revision": [{\(book(2, "1, 2", "101", nil)), "revision_id": 101, "revision": 1},
                            {\(book(3, "3, 4, 5", "102", nil)), "revision_id": 102, "revision": 2}],
           "normalized_processing": {"version": 1, "value": 1200, "partial": true,
                                     "rows_derived": 2, "rows_missing": 1,
                                     "cache_semantics": {"cache_subset": 2, "cache_separate": 0, "unknown": 1},
                                     "note": "separate versioned derived volume"},
           "attribution": {"rule": "run attribution via attempts.run_id ONLY"}},
         "native_preview": {
           "caliber": "native_session_preview", "project": "/tmp/projA",
           "run_id": "R001", "snapshot_id": 7,
           "as_of": "2026-10-16T09:59:30+00:00", "as_of_sessions_ms": 1790000000000,
           "linked_attempts": [
             {"attempt_id": 1, "role": "worker", "revision_id": 101, "task_id": "T001",
              "harness": "zcode", "authority": "session_metadata", "confidence": "high",
              "session_id": "sess-1",
              "tree_totals": {"root": "sess-1", "sessions_in_tree": 2, "turns": 10,
                              "input_tokens": 500, "output_tokens": 100,
                              "reasoning_tokens": null, "cache_read": 50, "cache_creation": 5}}],
           "union": {"sessions": ["sess-1", "sess-2"],
                     "tokens": {"turns": 12, "input_tokens": 600, "output_tokens": 120,
                                "reasoning_tokens": 30, "cache_read": 60, "cache_creation": 6}},
           "shared_session_trees": [], "subtree_conflicts": [],
           "controller_sessions": [
             {"session_id": "root-sess",
              "directory": "/tmp/projA", "directory_matches_project": true,
              "created_ms": 1790000000000, "updated_ms": 1790000000900,
              "own_usage": {"caliber": "model_usage_requests",
                            "source": "zcode model_usage mirror (zc_requests; read-only import)",
                            "requests": 2,
                            "input_tokens": 4000000, "output_tokens": 60000,
                            "reasoning_tokens": 0, "cache_read": 3700000,
                            "cache_creation": 0, "provider_total": null,
                            "computed_total": 4060000,
                            "processed_tokens": 4060000,
                            "processed_rule": "processed = input + output; cache_read is a subset of input",
                            "status_counts": {"completed": 2},
                            "settling_request_rows": 0,
                            "error_rows_zero_tokens": 0,
                            "missing_fields": {},
                            "rows_missing_processed": 0,
                            "last_activity_ms": 1790000000500},
              "run_attribution": {"attributed": true, "runs": ["R001"],
                                  "reason": null,
                                  "basis": "root ancestor of subagent sessions carrying this run's attempt anchors",
                                  "child_evidence": [
                                    {"session_id": "sess-1", "attempt_id": 1,
                                     "role": "worker",
                                     "authority": "session_metadata",
                                     "confidence": "high", "nonce": null}]}}],
           "controller_for_run": {"run_id": "R001", "status": "attributed",
                                  "session_ids": ["root-sess"], "reason": null,
                                  "note": "controller own usage (model_usage request caliber)"},
           "never_sum_with": "orx_recorded",
           "note": "cumulative native-session numbers, a separate caliber"},
         "snapshots": {"/tmp/projA": {"snapshot_id": 7,
                                      "taken_at": "2026-10-16T09:59:30+00:00",
                                      "schema_version": "11", "supported": true,
                                      "source_path": "/tmp/projA/.orx/state.db"}},
         "coverage": {"project": "/tmp/projA", "found": true, "snapshot_id": 7},
         "warnings": []}
        """
        return Data(json.utf8)
    }()

    static let taskDetailJSON: Data = {
        let json = """
        {"op": "tasks.get", "ok": true, "protocol_version": 1,
         "generated_at": "2026-10-16T10:00:00+00:00",
         "project": "/tmp/projA", "run_id": "R001",
         "revision_id": 102, "task_id": "T002", "found": true,
         "task": {
           "identity": {"project": "/tmp/projA", "run_id": "R001",
                        "revision_id": 102, "task_id": "T002"},
           "objective": "修复失败用例",
           "status": {"raw": "failed", "known": true, "display": "失败"},
           "failure_reason": "timeout",
           "created_at": "2026-10-16T09:30:00+00:00",
           "updated_at": "2026-10-16T09:50:00+00:00"},
         "usage": {
           "caliber": "orx_recorded", "project": "/tmp/projA", "run_id": "R001",
           "revision_id": 102, "task_id": "T002", "snapshot_id": 7,
           "as_of": "2026-10-16T09:59:30+00:00",
           "tokens": {"input_tokens": 400, "output_tokens": 80, "cached_input": null},
           "missing_fields": {"input_tokens": 0, "output_tokens": 0, "cached_input": 2},
           "accuracy": {"exact": 1, "estimated": 0, "unknown": 1},
           "coverage": {"observed": 2, "attempts": 3, "missing_recorded": 1, "unassessed": 0},
           "books": {
             "worker": {"attempts": 2, "attempt_ids": [4, 5], "revision_ids": [102],
                        "coverage": {"observed": 2, "attempts": 2, "missing_recorded": 0, "unassessed": 0},
                        "tokens": {"input_tokens": 400, "output_tokens": 80, "cached_input": null},
                        "missing_fields": {"input_tokens": 0, "output_tokens": 0, "cached_input": 2},
                        "accuracy": {"exact": 1, "estimated": 0, "unknown": 1},
                        "as_of": "2026-10-16T09:59:30+00:00"},
             "verifier": {"attempts": 1, "attempt_ids": [9], "revision_ids": [102],
                          "coverage": {"observed": 1, "attempts": 1, "missing_recorded": 0, "unassessed": 0},
                          "tokens": {"input_tokens": 50, "output_tokens": 10, "cached_input": null},
                          "missing_fields": {"input_tokens": 0, "output_tokens": 0, "cached_input": 0},
                          "accuracy": {"exact": 1, "estimated": 0, "unknown": 0},
                          "as_of": "2026-10-16T09:59:30+00:00"}},
           "normalized_processing": {"version": 1, "value": 480, "partial": false,
                                     "rows_derived": 2, "rows_missing": 0,
                                     "cache_semantics": {"cache_subset": 2, "cache_separate": 0, "unknown": 0},
                                     "note": "separate versioned derived volume"},
           "attempts": [
             {"attempt_id": 4, "role": "worker", "revision_id": 102, "task_id": "T002",
              "result": "failed", "failure_reason": "timeout",
              "started_at": "2026-10-16T09:30:00+00:00",
              "ended_at": "2026-10-16T09:40:00+00:00",
              "harness": "zcode", "model": "glm-4.7", "usage_missing_reason": null,
              "observation": {"source": "zcode_db", "accuracy": "exact",
                              "tokens": {"input_tokens": 200, "output_tokens": 40, "cached_input": null}},
              "session_link": {"session_id": "sess-1", "authority": "session_metadata",
                               "confidence": "high"}},
             {"attempt_id": 5, "role": "worker", "revision_id": 102, "task_id": "T002",
              "result": null, "failure_reason": null,
              "started_at": "2026-10-16T09:41:00+00:00", "ended_at": null,
              "harness": "zcode", "model": "glm-4.7",
              "usage_missing_reason": "attempt still running",
              "observation": null},
             {"attempt_id": 9, "role": "verifier", "revision_id": 102, "task_id": "T002",
              "result": "completed", "failure_reason": null,
              "started_at": "2026-10-16T09:45:00+00:00",
              "ended_at": "2026-10-16T09:46:00+00:00",
              "harness": "zcode", "model": "glm-4.7-air", "usage_missing_reason": null,
              "observation": {"source": "native_cli", "accuracy": "exact",
                              "tokens": {"input_tokens": 50, "output_tokens": 10, "cached_input": 0}},
              "session_link": {"session_id": "sess-9", "authority": "session_metadata",
                               "confidence": "high"}}],
           "attribution": {"rule": "task consumption = every attributable attempt"}},
         "verifications": [
           {"id": 1, "attempt_id": 4, "kind": "command", "command": "pytest -q",
            "exit_code": 1, "passed": 0, "created_at": "2026-10-16T09:39:00+00:00"},
           {"id": 2, "attempt_id": 4, "kind": "command", "command": "swift test",
            "exit_code": 0, "passed": 1, "created_at": "2026-10-16T09:39:30+00:00"}],
         "retry": {"recovered": false, "failing_attempt_ids": [4],
                   "successor_attempt_ids": [5]},
         "snapshots": {"/tmp/projA": {"snapshot_id": 7,
                                      "taken_at": "2026-10-16T09:59:30+00:00",
                                      "schema_version": "11", "supported": true,
                                      "source_path": "/tmp/projA/.orx/state.db"}},
         "coverage": {"project": "/tmp/projA", "found": true, "snapshot_id": 7},
         "warnings": []}
        """
        return Data(json.utf8)
    }()
}
