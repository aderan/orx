"""Verification validity window over ORX snapshots (0.5.0 closeout T001).

``tasks.get`` keeps every historical verification row with its original
``passed`` value and adds an optional per-row ``current`` flag plus a task
window summary, projecting the SAME rules as ORX
``Store.verifications_current``: latest worker-attempt boundary, latest
``verification_context`` watermark, unbound-legacy exclusion. A missing,
mismatched, corrupt or non-reusable basis is unknown — never confirmed, never
silently falling back to an earlier good context. One test cross-checks the
window against a temporary ORX state.db produced by the real ORX Store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import make_orx_db
from orxanalytics import correlate, query_model, registry
from orxanalytics.importers.orxstate import import_orx_project
from orxanalytics import verification_validity as vv

UTC_T = "2026-10-04T12:00:00+00:00"


# ------------------------------------------------------------- fixtures ----

@pytest.fixture()
def vw(tmp_path):
    """One imported project; tests seed verification facts straight into the
    snapshot mirror (same style as test_query_model's seed helpers)."""
    from orxanalytics.store import Store

    proj = tmp_path / "ProjV"
    (proj / ".orx").mkdir(parents=True)
    make_orx_db(proj / ".orx" / "state.db")
    store = Store(tmp_path / "analytics-home")
    registry.add_project(store, str(proj))
    assert "snapshot_id" in import_orx_project(store, str(proj))
    snap = correlate.latest_snapshot(store, str(proj))
    for rev, task in ((11, "T0V1"), (12, "T0V1")):
        add_task_row(store, snap, rev, task)
    return {"store": store, "proj": str(proj), "snap": snap}


def add_task_row(store, snap, rev, task):
    store.conn.execute(
        "INSERT INTO orx_tasks(snapshot_id,id,revision_id,task_id,objective,"
        "status,created_at,updated_at) VALUES "
        "(?,?,?,?,?,?,?,?)",
        (snap, 900 + rev, rev, task, "validity seed task", "passed",
         UTC_T, UTC_T))
    store.conn.commit()


def add_worker(store, snap, rev, task, att_id):
    store.conn.execute(
        "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,role,"
        "profile,driver,harness,result,run_id) VALUES "
        "(?,?,?,?,'worker','zcode-worker','host','zcode','completed','R001')",
        (snap, att_id, rev, task))
    store.conn.commit()


def add_verif(store, snap, rev, task, vid, passed=1, attempt_id=None):
    store.conn.execute(
        "INSERT INTO orx_verifications(snapshot_id,id,revision_id,task_id,"
        "attempt_id,kind,command,exit_code,passed,created_at) VALUES "
        "(?,?,?,?,?,'command','cmd',0,?,?)",
        (snap, vid, rev, task, attempt_id, passed, UTC_T))
    store.conn.commit()


def add_context(store, snap, rev, task, *, worker_attempt, after,
                reusable=True, event_id=None, bad=False):
    payload = {"version": 1, "worker_attempt": worker_attempt,
               "digest": "d" * 64, "manifest": {}, "verification_after": after,
               "reusable": reusable}
    reason = "{not json" if bad else json.dumps(payload, sort_keys=True)
    if event_id is None:
        event_id = store.conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS next FROM orx_task_events "
            "WHERE snapshot_id=?", (snap,)).fetchone()["next"]
    store.conn.execute(
        "INSERT INTO orx_task_events(snapshot_id,id,revision_id,task_id,"
        "from_status,to_status,event,reason,created_at) VALUES "
        "(?,?,?,?,'passed','passed','verification_context',?,?)",
        (snap, event_id, rev, task, reason, UTC_T))
    store.conn.commit()


def rows(resp):
    return {v["id"]: v for v in resp["verifications"]}


def get(vw, rev=11, task="T0V1"):
    return query_model.tasks_get(vw["store"], vw["proj"], "R001", rev, task)


# ------------------------------------------------------ the projection ----

def test_same_delivery_recheck_keeps_pass_current(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    # accepted delivery: the floor sits just BELOW the delivery-gate row
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=9)
    # same content version: a later pass of the same window stays current
    add_verif(store, snap, 11, "T0V1", 11, passed=1, attempt_id=900)
    resp = get(vw)
    assert rows(resp)[10]["current"] is True
    assert rows(resp)[11]["current"] is True
    win = resp["verification_window"]
    assert win["context_state"] == "valid"
    assert win["worker_attempt_id"] == 900
    assert win["verification_after"] == 9
    assert win["counts"] == {"current": 2, "current_passed": 2,
                             "historical": 0, "unknown": 0}


def test_watermark_advance_keeps_history_but_exits_window(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    add_verif(store, snap, 11, "T0V1", 11, passed=0, attempt_id=900)
    # artifact change: the floor jumps past BOTH earlier rows
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=11)
    add_verif(store, snap, 11, "T0V1", 12, passed=1, attempt_id=900)
    resp = get(vw)
    r = rows(resp)
    assert r[10]["current"] is False and r[11]["current"] is False
    assert "watermark" in r[10]["validity_reason"]
    # history is preserved with its original passed values
    assert r[10]["passed"] == 1 and r[11]["passed"] == 0
    assert r[12]["current"] is True
    assert resp["verification_window"]["counts"]["historical"] == 2


def test_worker_retry_excludes_previous_round(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    # retry: a newer worker attempt opens a new window
    add_worker(store, snap, 11, "T0V1", 950)
    add_context(store, snap, 11, "T0V1", worker_attempt=950, after=10)
    add_verif(store, snap, 11, "T0V1", 11, passed=1, attempt_id=950)
    resp = get(vw)
    r = rows(resp)
    assert r[10]["current"] is False
    assert "older worker attempt" in r[10]["validity_reason"]
    assert r[11]["current"] is True


def test_unbound_legacy_row_not_inherited_by_modern_delivery(vw):
    store, snap = vw["store"], vw["snap"]
    add_verif(store, snap, 11, "T0V1", 9, passed=1, attempt_id=None)
    add_worker(store, snap, 11, "T0V1", 900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=0)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    resp = get(vw)
    r = rows(resp)
    assert r[9]["current"] is False
    assert "unbound" in r[9]["validity_reason"]
    assert r[10]["current"] is True


def test_missing_context_is_unknown_not_confirmed(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    resp = get(vw)
    r = rows(resp)
    assert r[10]["current"] is None
    assert "missing" in r[10]["validity_reason"]
    win = resp["verification_window"]
    assert win["context_state"] == "missing"
    assert win["counts"]["unknown"] == 1


def test_corrupt_latest_context_does_not_fall_back(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=10,
                event_id=700)
    # a LATER corrupt event must not resurrect the earlier good window
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=10,
                event_id=701, bad=True)
    resp = get(vw)
    r = rows(resp)
    assert r[10]["current"] is None
    win = resp["verification_window"]
    assert win["context_state"] == "invalid"
    assert win["verification_after"] is None


def test_context_worker_mismatch_and_non_reusable_are_unknown(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    # context recorded for an OLDER worker: basis does not match
    add_context(store, snap, 11, "T0V1", worker_attempt=850, after=0)
    resp = get(vw)
    assert rows(resp)[10]["current"] is None
    assert resp["verification_window"]["context_state"] == "worker_mismatch"

    # non-reusable inputs (unavailable paths) never confirm old rows
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=0,
                reusable=False, event_id=800)
    resp = get(vw)
    assert rows(resp)[10]["current"] is None
    assert resp["verification_window"]["context_state"] == "not_reusable"


def test_old_snapshot_and_isolation(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=9)
    resp = get(vw)
    # snapshot basis is explicit, not a live file guarantee
    win = resp["verification_window"]
    assert win["basis"] == "snapshot"
    assert win["snapshot_id"] == snap
    assert "snapshot" in win["note"]
    # same task_id on ANOTHER revision keeps its own window (rev 12, T001)
    add_worker(store, snap, 12, "T0V1", 910)
    add_verif(store, snap, 12, "T0V1", 20, passed=1, attempt_id=910)
    other = get(vw, rev=12)
    assert rows(other)[20]["current"] is None      # no context there
    # rev 11's window never leaked into rev 12's rows and vice versa
    assert rows(resp)[10]["current"] is True
    assert set(rows(resp)) == {10}
    assert set(rows(other)) == {20}


def test_cross_project_and_run_rows_never_mix(vw, tmp_path):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=9)
    # a second project with the SAME numbers: its rows stay its own
    proj2 = tmp_path / "ProjV2"
    (proj2 / ".orx").mkdir(parents=True)
    make_orx_db(proj2 / ".orx" / "state.db")
    registry.add_project(store, str(proj2))
    assert "snapshot_id" in import_orx_project(store, str(proj2))
    snap2 = correlate.latest_snapshot(store, str(proj2))
    add_worker(store, snap2, 11, "T0V1", 900)
    add_verif(store, snap2, 11, "T0V1", 10, passed=0, attempt_id=900)
    r1 = get(vw)
    r2 = query_model.tasks_get(store, str(proj2), "R001", 11, "T0V1")
    assert rows(r1)[10]["current"] is True
    # no context in project 2: unknown, never borrowed from project 1
    assert rows(r2)[10]["current"] is None
    assert r2["verification_window"]["context_state"] == "missing"


def test_history_counts_keep_first_pass_caliber(vw):
    """Historical first-pass style metrics are untouched: the verifications
    list keeps every original passed row even when none is current."""
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    for vid, passed in ((10, 1), (11, 0), (12, 1)):
        add_verif(store, snap, 11, "T0V1", vid, passed=passed,
                  attempt_id=900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=12)
    resp = get(vw)
    assert [v["passed"] for v in resp["verifications"]] == [1, 0, 1]
    assert all(v["current"] is False for v in resp["verifications"])
    win = resp["verification_window"]
    assert win["counts"] == {"current": 0, "current_passed": 0,
                             "historical": 3, "unknown": 0}


# --------------------------------------------- real ORX cross-check ---------

def test_window_matches_real_orx_store(tmp_path):
    """Cross-check against a temporary ORX state.db produced by the REAL
    ORX Store: the projected current ids must equal
    ``Store.verifications_current`` at every stage, the history list must be
    append-only, and the source db bytes must not change on import."""
    orx_src = Path(__file__).resolve().parents[3] / "src"
    sys.path.insert(0, str(orx_src))
    try:
        orx_state = pytest.importorskip("orx.state")
        vc = pytest.importorskip("orx.verification_context")
    finally:
        sys.path.remove(str(orx_src))

    proj = tmp_path / "RealProj"
    (proj / ".orx").mkdir(parents=True)
    db = proj / ".orx" / "state.db"
    orx = orx_state.Store.open(db)
    goal, run = orx.goal_create("cross-check goal", ["a"], [], "")
    rev = orx.revision_create(run.id, "1", "planner", {})
    task = orx.task_insert(rev.id, "T001", "obj",
                           scope={"allowed": ["out.txt"]}, acceptance=["a"],
                           verification=[], routing={}, status="pending")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.txt").write_text("v1")
    w1 = orx.attempt_create(rev.id, "worker", "p", "cli", "zcode", "m",
                            "high", None, task_id="T001")
    vc.synchronize(orx, ws, rev.id, task)
    orx.verification_add(rev.id, "T001", "command", "cmd", True,
                         attempt_id=w1.id, exit_code=0)
    orx.verification_add(rev.id, "T001", "command", "cmd", False,
                         attempt_id=w1.id, exit_code=1)

    from orxanalytics.store import Store
    store = Store(tmp_path / "analytics-home")
    registry.add_project(store, str(proj))
    assert "snapshot_id" in import_orx_project(store, str(proj))

    def projected():
        resp = query_model.tasks_get(store, str(proj), run.id, rev.id,
                                     "T001")
        return ({v["id"] for v in resp["verifications"]
                 if v["current"] is True},
                [v["id"] for v in resp["verifications"]],
                resp)

    def oracle():
        return {v.id for v in orx.verifications_current(rev.id, "T001")}

    cur, hist, resp = projected()
    assert cur == oracle()
    assert hist == [v.id for v in orx.verifications_for(rev.id, "T001")]
    assert resp["verification_window"]["context_state"] == "valid"
    assert resp["verification_window"]["worker_attempt_id"] == w1.id

    # same delivery re-check: another pass stays current in both
    orx.verification_add(rev.id, "T001", "command", "cmd", True,
                         attempt_id=w1.id, exit_code=0)
    import_orx_project(store, str(proj))
    cur, _, _ = projected()
    assert cur == oracle() and len(cur) == 3

    # artifact change: watermark advances; old rows exit but survive
    (ws / "out.txt").write_text("v2")
    changed = vc.synchronize(orx, ws, rev.id, task)
    assert changed["changed"] is True and changed["invalidated_ids"]
    orx.verification_add(rev.id, "T001", "command", "cmd", True,
                         attempt_id=w1.id, exit_code=0)
    import_orx_project(store, str(proj))
    cur, hist, resp = projected()
    assert cur == oracle() and len(cur) == 1
    assert len(hist) == 4                      # append-only history
    dropped = {v["id"]: v for v in resp["verifications"]
               if v["current"] is False}
    assert set(dropped) == set(changed["invalidated_ids"])
    assert all("watermark" in v["validity_reason"]
               for v in dropped.values())

    # worker retry: a new worker attempt supersedes the whole round
    w2 = orx.attempt_create(rev.id, "worker", "p", "cli", "zcode", "m",
                            "high", None, task_id="T001")
    vc.synchronize(orx, ws, rev.id, task)
    orx.verification_add(rev.id, "T001", "command", "cmd", True,
                         attempt_id=w2.id, exit_code=0)
    import_orx_project(store, str(proj))
    cur, hist, resp = projected()
    assert cur == oracle() and cur == {max(v["id"] for v in
                                           resp["verifications"])}
    assert resp["verification_window"]["worker_attempt_id"] == w2.id
    older = [v for v in resp["verifications"]
             if v["attempt_id"] == w1.id]
    assert older and all(v["current"] is False for v in older)
    assert all("older worker attempt" in v["validity_reason"]
               for v in older)

    # the source ORX database is read-only to analytics
    orx.conn.close()
    digest_before = hashlib.sha256(db.read_bytes()).hexdigest()
    import_orx_project(store, str(proj))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest_before


# ------------------------------------------------------ module contract ----

def test_classify_is_pure_over_snapshot_rows(vw):
    store, snap = vw["store"], vw["snap"]
    add_worker(store, snap, 11, "T0V1", 900)
    add_verif(store, snap, 11, "T0V1", 10, passed=1, attempt_id=900)
    add_context(store, snap, 11, "T0V1", worker_attempt=900, after=9)
    out = vv.classify(store, snap, 11, "T0V1")
    assert out["rows"][10] == {"current": True, "validity_reason": None}
    assert out["window"]["context_state"] == "valid"
    # the classifier never touches the mirror tables
    counts = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_verifications").fetchone()["c"]
    vv.classify(store, snap, 11, "T0V1")
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM orx_verifications").fetchone()["c"] == counts


@pytest.mark.parametrize('change', [
    {'version': 2}, {'verification_after': True}, {'verification_after': -1},
    {'manifest': []}, {'digest': None}, {'reusable': 0}, {'worker_attempt': '900'},
])
def test_invalid_context_fields_never_confirm_or_restore_a_pass(vw, change):
    store, snap = vw['store'], vw['snap']
    add_worker(store, snap, 11, 'T0V1', 900)
    add_verif(store, snap, 11, 'T0V1', 10, attempt_id=900)
    add_context(store, snap, 11, 'T0V1', worker_attempt=900, after=0)
    payload = {'version': 1, 'worker_attempt': 900, 'verification_after': 0,
               'manifest': {}, 'digest': 'd'*64, 'reusable': True, **change}
    event_id = store.conn.execute(
        'SELECT MAX(id)+1 FROM orx_task_events WHERE snapshot_id=?', (snap,)).fetchone()[0]
    store.conn.execute(
        "INSERT INTO orx_task_events(snapshot_id,id,revision_id,task_id,event,reason) "
        "VALUES (?,?,11,'T0V1','verification_context',?)", (snap, event_id, json.dumps(payload)))
    store.conn.commit()
    response = get(vw)
    assert response['verification_window']['context_state'] == 'invalid'
    assert rows(response)[10]['current'] is None
    assert rows(response)[10]['passed'] == 1


def test_detail_revision_must_belong_to_the_requested_run(vw):
    response = query_model.tasks_get(vw['store'], vw['proj'], 'R999', 11, 'T0V1')
    assert response['found'] is False
    assert 'verifications' not in response
