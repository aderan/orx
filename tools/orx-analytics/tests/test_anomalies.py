"""Anomaly rules: hit AND no-hit, with baseline/confidence requirements."""

from __future__ import annotations

import json
import time

from orxanalytics import anomalies, correlate, findings, registry
from orxanalytics.importers import import_zcode
from orxanalytics.importers.orxstate import import_orx_project


def _loaded(world):
    import_zcode(world["store"], world["zcode_db"])
    registry.add_project(world["store"], world["proj_a"])
    import_orx_project(world["store"], world["proj_a"])
    correlate.correlate(world["store"], world["proj_a"])
    return world["store"]


def test_rules_hit_and_miss(world):
    store = _loaded(world)
    pk = world["proj_a"]
    # duplicate_delivery: fixture declares rev1 T003 -> rev2 T003 redo
    hits = anomalies.duplicate_delivery(store, pk, {})
    assert len(hits) == 1
    assert hits[0].payload["evidence"]["redo_reason"] == "scope widened"
    assert hits[0].payload["missing_evidence"]  # must say what's missing

    # repeat_failures: rev1 T001 failed once -> below threshold of 2
    misses = anomalies.repeat_failures(store, pk, {"min_failed_events": 2})
    assert misses == []
    hits = anomalies.repeat_failures(store, pk, {"min_failed_events": 1})
    assert len(hits) == 1 and "T001" in hits[0].title

    # replan_burst: single replan -> no burst at >=2
    assert anomalies.replan_burst(store, pk, {"min_replans": 2}) == []
    assert len(anomalies.replan_burst(store, pk, {"min_replans": 1})) == 1

    # check_loop_overflow: fixture rows are all 1 per command -> no hit
    assert anomalies.check_loop_overflow(store, pk, {"budget": 3}) == []

    # coverage_gap: workers=4, unassessed=1 (204), unlinked host workers=1
    # (201 has inferred-high link, 204/202 linked, 203 is cli) -> no hit at
    # 0.5 threshold
    assert anomalies.coverage_gap(store, pk, {}) == []
    hits = anomalies.coverage_gap(
        store, pk, {"min_unassessed_frac": 0.2, "min_attempts": 3})
    assert any(h.detail_key == "unassessed" for h in hits)

    # usage_outlier: 3 canonical observations < min_samples 5 -> no claim
    assert anomalies.usage_outlier(store, pk, {"min_samples": 5}) == []

    # no_progress: fixture turn gaps are ~35-100s -> no hit at 3600
    assert anomalies.no_progress(store, {"min_gap_sec": 3600}) == []
    hits = anomalies.no_progress(store, {"min_gap_sec": 30})
    assert hits and hits[0].payload["confidence"] == "low"


def test_findings_dedupe_and_merge(world):
    store = _loaded(world)
    res = anomalies.run_all(store, [world["proj_a"]])
    assert res
    ids = set()
    for r in res:
        row = findings.upsert_finding(store, **r.upsert_args())
        ids.add(row["id"])
    n1 = len(findings.list_findings(store))
    # the same round again: fingerprints match, nothing new is created
    for r in anomalies.run_all(store, [world["proj_a"]]):
        row = findings.upsert_finding(store, **r.upsert_args())
        assert row["merged"] is True or row["id"] in ids
    assert len(findings.list_findings(store)) == n1


def test_issue_draft_structure_and_fix_link(world):
    store = _loaded(world)
    res = anomalies.run_all(store, [world["proj_a"]])
    row = findings.upsert_finding(store, **res[0].upsert_args())
    draft = findings.draft_issue(store, row["id"])
    for key in ("actual_behavior", "expected_behavior", "impact",
                "evidence", "hypotheses_to_verify",
                "minimal_fix_suggestion", "acceptance_criteria",
                "regression_verification", "missing_evidence",
                "linked_refs"):
        assert key in draft
    # a fix link without a comparable after-sample says unproven
    out = findings.link_fix(store, row["id"], "commit-abc",
                            before={"first_pass": 0.5}, after=None)
    assert out["comparable"] is False and out["verified_at"] is None
    draft = findings.draft_issue(store, row["id"])
    assert draft["linked_fixes"][0]["comparable"] == 0
    out = findings.link_fix(store, row["id"], "commit-abc",
                            before={"first_pass": 0.5},
                            after={"first_pass": 1.0})
    assert out["comparable"] is True


def test_watch_round_once_and_state(world, tmp_path):
    from orxanalytics import watch
    home = tmp_path / "watchhome"
    home.mkdir()
    # seed: a project registered in this data dir, sources pointed at the
    # fixture via env is complex; here we only exercise the loop with the
    # fixture store's data dir already containing data
    watch.run_one_round(world["store"].data_dir, 1, once=True)
    st = watch.status(world["store"].data_dir)
    assert st["rounds"] >= 1
    assert st["last_round_at"]
    # errors from unreachable global sources are recorded, not fatal
    if st["last_error"]:
        assert isinstance(st["last_error"], str)
    assert not (world["store"].data_dir / "watch.pid").exists()


def test_fast_fail_stall_signature_scope_recovery(world):
    store = _loaded(world)
    ctrl = world["zcode"]["controller"]
    other = world["zcode"]["other"]
    # fixture's latest turns are all completed: nothing stalled
    assert anomalies.fast_fail_stall(store, {}) == []

    now_ms = time.time() * 1000
    # errored-after-work as a session's latest turn: visible failure,
    # not the silent stall signature -> out of scope
    store.conn.execute(
        "INSERT INTO zc_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (other, "turn_o_err", "error", now_ms - 60000, now_ms - 1000,
         59000, 3, 0, 8, 0, 900000, 5000, 0, 0, 0))
    store.conn.commit()
    assert anomalies.fast_fail_stall(store, {}) == []

    # zero-token fast-fail as the latest turn -> one signal finding
    store.conn.execute(
        "INSERT INTO zc_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctrl, "turn_c3", "error", now_ms - 25 * 60000, None, None,
         1, 0, 0, 0, 0, 0, 0, 0, 0))
    store.conn.commit()
    hits = anomalies.fast_fail_stall(store, {})
    assert len(hits) == 1 and hits[0].rule == "fast_fail_stall"
    p = hits[0].payload
    assert p["confidence"] == "signal"
    assert p["evidence"]["minutes_since"] == 25
    assert p["evidence"]["model_retry_count"] == 0
    assert p["baseline"]["measured_stalls"]      # grounded in real samples

    # a later completed turn means the session recovered -> silent again
    store.conn.execute(
        "INSERT INTO zc_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctrl, "turn_c4", "completed", now_ms - 60000, now_ms, 59000,
         2, 0, 1, 0, 1000, 100, 0, 0, 0))
    store.conn.commit()
    assert anomalies.fast_fail_stall(store, {}) == []


def test_watch_notify_filter_escape_and_killswitch(monkeypatch):
    from orxanalytics import watch
    calls: list = []
    monkeypatch.delenv("ORX_ANALYTICS_NOTIFY", raising=False)
    monkeypatch.setattr(watch.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    rows = [
        {"rule": "fast_fail_stall", "title": 'stall "quoted"',
         "payload_json": '{"confidence": "signal"}'},
        {"rule": "no_progress", "title": "maybe think-time",
         "payload_json": '{"confidence": "low"}'},          # stays silent
        {"rule": "repeat_failures", "title": "failed twice",
         "payload_json": "not-json"},                       # notify anyway
    ]
    assert watch._notify(rows) == 2 and len(calls) == 1
    script = calls[0][0][2]
    assert "fast_fail_stall" in script and "repeat_failures" in script
    assert "no_progress" not in script
    assert '"quoted"' not in script           # double quotes escaped out

    monkeypatch.setenv("ORX_ANALYTICS_NOTIFY", "0")
    assert watch._notify(rows) == 0 and len(calls) == 1


def test_watch_round_notifies_new_stall(world, monkeypatch):
    from orxanalytics import watch
    store = _loaded(world)
    store.conn.execute(
        "INSERT INTO zc_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (world["zcode"]["controller"], "turn_c3", "error",
         time.time() * 1000 - 25 * 60000, None, None, 1, 0, 0, 0,
         0, 0, 0, 0, 0))
    store.conn.commit()
    calls: list = []
    monkeypatch.setattr(watch.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setenv("ORX_ANALYTICS_NOTIFY", "1")
    watch.run_one_round(store.data_dir, 1, once=True)
    assert any("fast_fail_stall" in c[0][2] for c in calls)
    st = watch.status(store.data_dir)
    assert st["notified_last_round"] >= 1
    # second identical round: fingerprint dedupe -> nothing new, no repeat
    watch.run_one_round(store.data_dir, 1, once=True)
    assert len(calls) == 1
