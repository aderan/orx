"""Query-layer read model (design §7, G002 T003).

Pure-function read interfaces: overview / runs.list / runs.get / tasks.get /
projects.list / health (+ the frozen refresh.request contract the engine
layer will implement). Regression fixtures cover: the same run_id in two
projects, the same short task number across revisions, retry and verifier
attribution, a planner attempt with a NULL revision, one session carrying
several attempts, and overlapping session subtrees. Every response shares
one envelope (protocol version, generated time, per-project snapshot
identity, coverage, warnings); consumption ordering always carries its
coverage so a missing-data run can never look "cheap".
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_codex_rollout, make_orx_db, make_zcode_db
from orxanalytics import correlate, display, query_model, registry, usage_readmodel
from orxanalytics.importers import import_codex, import_zcode
from orxanalytics.importers.orxstate import import_orx_project

UTC = timezone.utc
FIXED_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
ENVELOPE_KEYS = {"op", "protocol_version", "generated_at", "snapshots",
                 "coverage", "warnings"}


def T(sec: int) -> str:
    return (datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
            + timedelta(seconds=sec)).isoformat()


# ------------------------------------------------------------ fixture side ---

def seed_usage_rows(store, proj: str) -> None:
    """Same shape as the usage-readmodel fixture: two verifiers on rev1
    T001, cache-shape variety on rev1 T002, run-internal unattributable
    attempts, project-level orphans."""
    snap = correlate.latest_snapshot(store, proj)
    t0 = "2026-10-04T12:00:40+00:00"
    t1 = "2026-10-04T12:01:10+00:00"
    conn = store.conn
    attempts = [
        (210, 11, "T001", "verifier", "zcode-verifier", "host", "zcode",
         "m2", t0, t1, "completed", "R001"),
        (211, 11, "T001", "verifier", "zcode-verifier", "host", "zcode",
         "m2", t0, t1, "completed", "R001"),
        (212, 11, "T002", "worker", "cursor-strong", "cli", "cursor",
         "m3", t0, t1, "completed", "R001"),
        (213, 11, "T002", "worker", "cursor-strong", "cli", "cursor",
         "m3", t0, t1, "completed", "R001"),
        (214, 11, "T002", "worker", "cursor-strong", "cli", "cursor",
         "m3", t0, t1, "completed", "R001"),
        (215, 11, "T002", "worker", "warp-fast", "cli", "warp",
         "m4", t0, t1, "completed", "R001"),
        (220, None, None, "worker", "zcode-worker", "host", "zcode",
         "m2", t0, t1, "failed", "R001"),
        (221, 11, None, "worker", "zcode-worker", "host", "zcode",
         "m2", t0, t1, "completed", "R001"),
        (222, None, None, "worker", "zcode-worker", "host", "zcode",
         "m2", t0, t1, "completed", None),
    ]
    conn.executemany(
        "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,role,"
        "profile,driver,harness,model,started_at,ended_at,result,run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(snap, *row) for row in attempts])
    obs = [
        (310, 210, "zcode-verifier", "R001", "T001", 300, 30, 200,
         "native_cli", "exact"),
        (311, 211, "zcode-verifier", "R001", "T001", None, 50, None,
         "native_cli", None),
        (312, 212, "cursor-strong", "R001", "T002", 0, 0, 0,
         "native_cli", "exact"),
        (313, 213, "cursor-strong", "R001", "T002", 100, 10, 150,
         "native_cli", "exact"),
        (314, 214, "cursor-strong", "R001", "T002", 100, 10, None,
         "native_cli", "estimated"),
        (315, 215, "warp-fast", "R001", "T002", 50, 5, 0,
         "native_cli", "exact"),
        (320, 220, "zcode-worker", "R001", None, 700, 70, 0,
         "native_cli", "exact"),
        (321, 221, "zcode-worker", "R001", None, 800, 80, 0,
         "native_cli", "exact"),
        (322, 222, "zcode-worker", None, None, 900, 90, 0,
         "native_cli", "exact"),
        (330, None, "manual", None, None, 50, 5, 0,
         "host_report", "estimated"),
    ]
    conn.executemany(
        "INSERT INTO orx_usage_obs(snapshot_id,id,attempt_id,profile,run_id,"
        "task_id,input_tokens,output_tokens,cached_input,source,accuracy,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(snap, *row, t1) for row in obs])
    conn.commit()


def seed_extra_runs(store, proj: str) -> None:
    """Runs that exercise the query layer: an active run with failed /
    retrying tasks, a blocked run, a run with an unrecognized status, one
    well-observed spend run and one run whose attempts have no usage."""
    snap = correlate.latest_snapshot(store, proj)
    conn = store.conn
    conn.executemany(
        "INSERT INTO orx_runs VALUES (?,?,?,?,?,?,?,?)",
        [(snap, "R010", "G001", "running", T(0), T(121), T(100), None),
         (snap, "R011", "G001", "blocked", T(0), T(116), T(110), None),
         (snap, "R012", "G001", "weird", T(0), T(126), T(120), None),
         (snap, "R020", "G001", "done", T(0), T(145), T(130), T(140)),
         (snap, "R021", "G001", "done", T(0), T(155), T(130), T(150))])
    conn.executemany(
        "INSERT INTO orx_revisions VALUES (?,?,?,?,?,?,?,?)",
        [(snap, 21, "R010", 3, 2, "planner-a", "active", T(101)),
         (snap, 22, "R020", 1, 2, "planner-a", "superseded", T(129)),
         (snap, 23, "R021", 1, 2, "planner-a", "active", T(129))])
    tasks = [
        (121, 21, "T100", "query seed running task", "running", None,
         T(101), T(121)),
        (122, 21, "T101", "query seed failed task", "failed", "boom",
         T(101), T(115)),
        (123, 21, "T102", "query seed retry task", "failed", "boom twice",
         T(101), T(118)),
        (124, 22, "T200", "query seed spend task", "passed", None,
         T(129), T(139)),
        (125, 23, "T201", "query seed silent task", "passed", None,
         T(129), T(149)),
    ]
    conn.executemany(
        "INSERT INTO orx_tasks VALUES (?,?,?,?,?,?,?,?,?)",
        [(snap, *t) for t in tasks])
    attempts = [
        (205, 21, "T100", "worker", "zcode-worker", "host", "zcode", "m9",
         T(102), None, None, None, "R010", None),
        (206, 21, "T101", "worker", "zcode-worker", "host", "zcode", "m9",
         T(103), T(110), "failed", "boom", "R010", None),
        (207, 21, "T102", "worker", "zcode-worker", "host", "zcode", "m9",
         T(104), T(112), "failed", "boom twice", "R010", None),
        (208, 21, "T102", "worker", "zcode-worker", "host", "zcode", "m9",
         T(113), None, None, None, "R010", None),
        (250, 22, "T200", "worker", "zcode-worker", "host", "zcode",
         "bigm", T(131), T(139), "completed", None, "R020", None),
        (251, 23, "T201", "worker", "zcode-worker", "host", "zcode",
         "bigm", T(131), T(149), "completed", None, "R021",
         "harness_omitted"),
    ]
    conn.executemany(
        "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,role,"
        "profile,driver,harness,model,started_at,ended_at,result,"
        "failure_reason,run_id,usage_missing_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(snap, *row) for row in attempts])
    conn.execute(
        "INSERT INTO orx_usage_obs(snapshot_id,id,attempt_id,profile,run_id,"
        "task_id,input_tokens,output_tokens,cached_input,source,accuracy,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (snap, 350, 250, "zcode-worker", "R020", "T200", 100000, 5000, 0,
         "native_cli", "exact", T(139)))
    conn.commit()


def seed_subtree_conflict(store, proj: str) -> None:
    """Two zcode sessions P (root) and C (child) linked to two different
    attempts: tree(P) contains C, so per-attempt tree totals overlap."""
    snap = correlate.latest_snapshot(store, proj)
    conn = store.conn
    conn.executemany(
        "INSERT INTO zc_sessions VALUES (?,?,?,?,?,?,?)",
        [("sess_P", None, proj, "proj_a", "P root", 1791115200000,
          1791115201000),
         ("sess_C", "sess_P", proj, "proj_a", "P child", 1791115200050,
          1791115201500)])
    conn.executemany(
        "INSERT INTO zc_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("sess_P", "turn_p", "completed", 1791115200000, 1791115200900,
          900, 1, 0, 1, 0, 100, 10, 0, 0, 0),
         ("sess_C", "turn_c", "completed", 1791115200050, 1791115201450,
          400, 1, 0, 1, 0, 20, 2, 0, 0, 0)])
    for att_id, rev_id, task in ((260, 12, "T003"), (261, 11, "T001")):
        conn.execute(
            "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,"
            "role,profile,driver,harness,result,run_id) VALUES "
            "(?,?,?,?,'worker','zcode-worker','host','zcode',"
            "'completed','R001')",
            (snap, att_id, rev_id, task))
    for att_id, sid in ((260, "sess_P"), (261, "sess_C")):
        conn.execute(
            "INSERT INTO links(project_key,attempt_id,harness,session_id,"
            "authority,basis,confidence,computed_at) VALUES "
            "(?,?,'zcode',?,'stored','test',NULL,"
            "'2026-10-16T00:00:00+00:00')", (proj, att_id, sid))
    conn.commit()


def _min_orx_db(path, version: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE runs(id TEXT PRIMARY KEY, goal_id TEXT, status TEXT,"
        " created_at TEXT, updated_at TEXT, started_at TEXT, completed_at TEXT)"
    )
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
    conn.commit()
    conn.close()


@pytest.fixture()
def qw(tmp_path):
    from orxanalytics.store import Store

    proj_a = tmp_path / "ProjA"
    proj_a.mkdir()
    proj_b = tmp_path / "ProjBORX"
    (proj_b / ".orx").mkdir(parents=True)
    zcode_db = tmp_path / "zcode-db.sqlite"
    zc = make_zcode_db(zcode_db, str(proj_a), str(tmp_path / "ProjB"))
    (proj_a / ".orx").mkdir()
    make_orx_db(proj_a / ".orx" / "state.db")
    make_orx_db(proj_b / ".orx" / "state.db")   # SAME R001 / T001 / G001
    codex_dir = tmp_path / "codex-sessions"
    (codex_dir / "2026" / "10" / "04").mkdir(parents=True)
    make_codex_rollout(codex_dir / "2026" / "10" / "04"
                       / "rollout-test.jsonl", str(proj_a))
    store = Store(tmp_path / "analytics-home")
    import_zcode(store, zcode_db)
    import_codex(store, codex_dir)
    registry.add_project(store, str(proj_a))
    registry.add_project(store, str(proj_b))
    import_orx_project(store, str(proj_a))
    import_orx_project(store, str(proj_b))
    correlate.correlate(store, str(proj_a))
    correlate.correlate(store, str(proj_b))
    seed_usage_rows(store, str(proj_a))
    seed_extra_runs(store, str(proj_a))
    seed_subtree_conflict(store, str(proj_a))

    # cross-project results keep their own update times: proj_b was last
    # seen five minutes EARLIER than proj_a
    snap_a = correlate.latest_snapshot(store, str(proj_a))
    snap_b = correlate.latest_snapshot(store, str(proj_b))
    taken_a = store.conn.execute(
        "SELECT taken_at FROM orx_snapshots WHERE snapshot_id=?",
        (snap_a,)).fetchone()["taken_at"]
    taken_b = (datetime.fromisoformat(taken_a)
               - timedelta(minutes=5)).isoformat()
    store.conn.execute(
        "UPDATE orx_snapshots SET taken_at=? WHERE snapshot_id=?",
        (taken_b, snap_b))

    # unsupported-schema project: live v16 source (beyond the contract
    # gate) refuses to import, and a foreign snapshot (from a newer reader)
    # must stay isolated, no crash
    proj_c = tmp_path / "ProjC"
    (proj_c / ".orx").mkdir(parents=True)
    _min_orx_db(proj_c / ".orx" / "state.db", "16")
    registry.add_project(store, str(proj_c))
    assert "error" in import_orx_project(store, str(proj_c))
    cur = store.conn.execute(
        "INSERT INTO orx_snapshots(project_key,taken_at,schema_version,"
        "source_path) VALUES (?,?,?,?)",
        (str(proj_c), taken_a, "16", str(proj_c / ".orx" / "state.db")))
    snap_c = cur.lastrowid
    store.conn.execute(
        "INSERT INTO orx_runs VALUES (?,?,?,?,?,?,?,?)",
        (snap_c, "R099", "G001", "running", T(0), T(90), T(80), None))

    # deleted project: registered, then the directory disappears
    proj_d = tmp_path / "ProjD"
    (proj_d / ".orx").mkdir(parents=True)
    _min_orx_db(proj_d / ".orx" / "state.db", "9")
    registry.add_project(store, str(proj_d))
    shutil.rmtree(proj_d)

    # project without any ORX state (basic session analysis only)
    proj_e = tmp_path / "ProjE"
    proj_e.mkdir()
    registry.add_project(store, str(proj_e))
    store.conn.commit()
    return {
        "tmp": tmp_path, "store": store,
        "proj_a": str(proj_a), "proj_b": str(proj_b),
        "proj_c": str(proj_c), "proj_d": str(proj_d), "proj_e": str(proj_e),
        "zcode": zc, "taken_a": taken_a, "taken_b": taken_b,
    }


def _runs(resp):
    return resp["runs"]


def _ids(resp):
    return [(r["project"], r["run_id"]) for r in _runs(resp)]


def _fresh_now(qw):
    return datetime.fromisoformat(qw["taken_a"]) + timedelta(seconds=30)


# ------------------------------------------------------------ the envelope ---

def test_six_interfaces_share_one_envelope(qw):
    store = qw["store"]
    now = FIXED_NOW
    responses = [
        query_model.overview(store, now=now, stale_after=90.0),
        query_model.runs_list(store, now=now),
        query_model.runs_get(store, qw["proj_a"], "R001", now=now),
        query_model.tasks_get(store, qw["proj_a"], "R001", 11, "T001",
                              now=now),
        query_model.projects_list(store, now=now, stale_after=90.0),
        query_model.health(store, now=now, stale_after=90.0),
    ]
    ops = []
    for resp in responses:
        assert ENVELOPE_KEYS <= set(resp), set(resp)
        assert resp["protocol_version"] == query_model.PROTOCOL_VERSION
        assert resp["generated_at"] == "2026-10-16T12:00:00+00:00"
        assert isinstance(resp["snapshots"], dict)
        assert isinstance(resp["coverage"], dict)
        assert isinstance(resp["warnings"], list)
        ops.append(resp["op"])
        json.dumps(resp)                       # wire-serializable as-is
    assert set(ops) == {"overview", "runs.list", "runs.get", "tasks.get",
                        "projects.list", "health"}


def test_snapshots_view_carries_snapshot_identity(qw):
    resp = query_model.runs_list(qw["store"], now=FIXED_NOW)
    snaps = resp["snapshots"]
    a = snaps[qw["proj_a"]]
    b = snaps[qw["proj_b"]]
    assert a["snapshot_id"] != b["snapshot_id"]
    assert a["taken_at"] == qw_world_taken(qw, a["snapshot_id"])
    assert a["supported"] is True and b["supported"] is True
    # each project keeps its own update time
    assert a["taken_at"] == qw["taken_a"]
    assert b["taken_at"] == qw["taken_b"]
    assert snaps[qw["proj_c"]]["supported"] is False
    assert snaps[qw["proj_d"]]["snapshot_id"] is None


def qw_world_taken(qw, snapshot_id):
    row = qw["store"].conn.execute(
        "SELECT taken_at FROM orx_snapshots WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    return row["taken_at"]


# ------------------------------------------------- identity: no crosstalk ----

def test_cross_project_same_run_id_two_distinct_runs(qw):
    resp = query_model.runs_list(qw["store"], run_id="R001",
                                 now=FIXED_NOW)
    ids = _ids(resp)
    assert len(ids) == 2
    assert set(ids) == {(qw["proj_a"], "R001"), (qw["proj_b"], "R001")}
    by_proj = {r["project"]: r for r in _runs(resp)}
    # proj_a carries the seeded extra rows; proj_b only the base fixture —
    # no bleed in either direction
    assert by_proj[qw["proj_a"]]["consumption"]["tokens"] == {
        "input_tokens": 405249, "output_tokens": 11460,
        "cached_input": 327026}
    assert by_proj[qw["proj_b"]]["consumption"]["tokens"] == {
        "input_tokens": 404699, "output_tokens": 11355,
        "cached_input": 322176 + 500 + 4000}
    # cross-project results keep their own update times
    assert by_proj[qw["proj_a"]]["data_as_of"] == qw["taken_a"]
    assert by_proj[qw["proj_b"]]["data_as_of"] == qw["taken_b"]


def test_runs_list_consumption_matches_run_usage(qw):
    store = qw["store"]
    resp = query_model.runs_list(store, run_id="R001", project=qw["proj_a"],
                                 now=FIXED_NOW)
    item = _runs(resp)[0]
    detail = usage_readmodel.run_usage(store, qw["proj_a"], "R001")
    assert item["consumption"]["tokens"] == detail["tokens"]
    assert item["consumption"]["coverage"] == detail["coverage"]
    assert detail["coverage"] == {"observed": 11, "attempts": 15,
                                  "missing_recorded": 1, "unassessed": 3}


def test_cross_revision_same_task_number_never_merges(qw):
    store = qw["store"]
    resp = query_model.runs_get(store, qw["proj_a"], "R001", now=FIXED_NOW)
    tasks = {(t["revision_id"], t["task_id"]) for t in resp["tasks"]}
    assert (11, "T001") in tasks and (12, "T001") in tasks
    rev1 = usage_readmodel.task_usage(store, qw["proj_a"], "R001", 11,
                                      "T001")
    rev2 = usage_readmodel.task_usage(store, qw["proj_a"], "R001", 12,
                                      "T001")
    assert rev1["books"]["worker"]["tokens"]["input_tokens"] == 6000
    assert rev2["books"]["worker"]["tokens"]["input_tokens"] is None
    listed = {(t["revision_id"], t["task_id"]): t for t in resp["tasks"]}
    assert listed[(11, "T001")]["consumption"]["tokens"][
        "input_tokens"] == 6300
    assert listed[(12, "T001")]["consumption"]["tokens"][
        "input_tokens"] is None


def test_run_summary_covers_all_revisions(qw):
    store = qw["store"]
    resp = query_model.runs_get(store, qw["proj_a"], "R001", now=FIXED_NOW)
    usage = resp["usage"]
    assert usage["revision_scope"] == "all revisions via attempts.run_id"
    per_rev = {p["revision"]: p for p in usage["per_revision"]}
    assert per_rev[1]["tokens"]["input_tokens"] == 6550
    assert per_rev[2]["tokens"]["input_tokens"] is None  # unknown, not 0
    assert usage["tokens"]["input_tokens"] == 405249


# ----------------------------------------------- attribution via T002 books ---

def test_retry_and_verifier_attribution_in_tasks_get(qw):
    store = qw["store"]
    resp = query_model.tasks_get(store, qw["proj_a"], "R001", 11, "T001",
                                 now=FIXED_NOW)
    usage = resp["usage"]
    # the failed round and its retry BOTH count; verifiers separate. The
    # subtree-seeded attempt 261 shares this task identity and has no
    # observation, so it joins the book without changing the sums.
    assert usage["books"]["worker"]["attempt_ids"] == [201, 202, 261]
    assert usage["books"]["verifier"]["attempt_ids"] == [210, 211]
    assert usage["tokens"]["input_tokens"] == 6300
    results = {d["attempt_id"]: d["result"] for d in usage["attempts"]}
    assert results[201] == "failed"
    assert resp["retry"]["recovered"] is True
    assert resp["retry"]["failing_attempt_ids"] == [201]
    assert 202 in resp["retry"]["successor_attempt_ids"]
    assert [v["passed"] for v in resp["verifications"]] == [1, 0, 1, 1]


def test_planner_without_revision_belongs_to_run(qw):
    resp = query_model.runs_get(qw["store"], qw["proj_a"], "R001",
                                now=FIXED_NOW)
    planner = resp["usage"]["books"]["planner"]
    assert planner["attempt_ids"] == [200]
    assert planner["revision_ids"] == [None]
    assert planner["tokens"]["input_tokens"] == 398699
    assert resp["usage"]["tokens"]["input_tokens"] == 405249


def test_shared_session_tree_and_overlapping_subtree(qw):
    resp = query_model.runs_get(qw["store"], qw["proj_a"], "R001",
                                now=FIXED_NOW)
    prev = resp["native_preview"]
    assert prev["caliber"] == "native_session_preview"
    assert resp["usage"]["caliber"] == "orx_recorded"
    # one session carrying several attempts is reported, never re-summed
    assert prev["shared_session_trees"] == [
        {"session_ids": [qw["zcode"]["w1"]],
         "attempt_ids": [201, 204]}]
    # P/C trees differ and intersect on C -> a genuine conflict
    assert {"session_ids": ["sess_C"],
            "attempt_ids": [260, 261]} in prev["subtree_conflicts"]


# ------------------------------------------------------ lists: page & sort ---

def test_pagination_with_stable_identity_secondary_key(qw):
    store = qw["store"]
    resp = query_model.runs_list(store, limit=10, now=FIXED_NOW)
    assert [rid for _, rid in _ids(resp)] == [
        "R020", "R021", "R012", "R011", "R010", "R001", "R001"]
    assert _ids(resp)[5] == (qw["proj_a"], "R001")
    assert _ids(resp)[6] == (qw["proj_b"], "R001")
    page1 = query_model.runs_list(store, limit=3, offset=0, now=FIXED_NOW)
    page2 = query_model.runs_list(store, limit=3, offset=3, now=FIXED_NOW)
    page3 = query_model.runs_list(store, limit=3, offset=6, now=FIXED_NOW)
    joined = _ids(page1) + _ids(page2) + _ids(page3)
    assert joined == _ids(resp)
    assert page1["page"] == {"limit": 3, "offset": 0, "total_matched": 7,
                             "returned": 3}
    assert page3["page"]["returned"] == 1
    # R099 from the unsupported-schema project never leaks into any page
    assert "R099" not in [rid for _, rid in joined]


def test_runs_list_filters(qw):
    store = qw["store"]
    only_b = query_model.runs_list(store, project=qw["proj_b"],
                                   now=FIXED_NOW)
    assert _ids(only_b) == [(qw["proj_b"], "R001")]

    gate = query_model.runs_list(store, text="gate work", now=FIXED_NOW)
    assert set(_ids(gate)) == {(qw["proj_a"], "R001"),
                               (qw["proj_b"], "R001")}

    task = query_model.runs_list(store, task_id="T002", now=FIXED_NOW)
    assert set(_ids(task)) == {(qw["proj_a"], "R001"),
                               (qw["proj_b"], "R001")}

    running = query_model.runs_list(store, status="running", now=FIXED_NOW)
    assert _ids(running) == [(qw["proj_a"], "R010")]

    weird = query_model.runs_list(store, status="weird", now=FIXED_NOW)
    assert _ids(weird) == [(qw["proj_a"], "R012")]
    codes = {w["code"] for w in weird["warnings"]}
    assert "unknown-run-status" in codes

    m9 = query_model.runs_list(store, model="m9", now=FIXED_NOW)
    assert _ids(m9) == [(qw["proj_a"], "R010")]

    bigm = query_model.runs_list(store, model="bigm", now=FIXED_NOW)
    assert [rid for _, rid in _ids(bigm)] == ["R020", "R021"]

    recent = query_model.runs_list(store, since=T(125), now=FIXED_NOW)
    assert [rid for _, rid in _ids(recent)] == ["R020", "R021"]

    early = query_model.runs_list(store, until=T(125), now=FIXED_NOW)
    assert [rid for _, rid in _ids(early)] == [
        "R012", "R011", "R010", "R001", "R001"]


def test_unknown_project_filter_warns(qw):
    resp = query_model.runs_list(qw["store"],
                                 project="/definitely/not/registered",
                                 now=FIXED_NOW)
    assert _runs(resp) == []
    assert any(w["code"] == "unknown-project-filter"
               for w in resp["warnings"])


def test_consumption_sort_carries_coverage(qw):
    store = qw["store"]
    asc = query_model.runs_list(store, sort="observed_tokens", order="asc",
                                limit=10, now=FIXED_NOW)
    order = [rid for _, rid in _ids(asc)]
    # known spend ascending first; ALL unknown-consumption runs stay last
    # so a missing-data run can never look like the cheapest one
    assert order[:3] == ["R020", "R001", "R001"]
    assert set(order[3:]) == {"R010", "R011", "R012", "R021"}
    desc = query_model.runs_list(store, sort="observed_tokens",
                                 order="desc", limit=10, now=FIXED_NOW)
    dorder = [rid for _, rid in _ids(desc)]
    assert dorder[:3] == ["R001", "R001", "R020"]
    assert set(dorder[3:]) == {"R010", "R011", "R012", "R021"}
    items = {(r["run_id"], r["project"]): r for r in _runs(desc)}
    spend = items[("R020", qw["proj_a"])]["consumption"]
    assert spend["observed_total"] == 105000
    assert spend["coverage"]["observed"] == 1
    assert spend["coverage"]["attempts"] == 1
    silent = items[("R021", qw["proj_a"])]["consumption"]
    assert silent["observed_total"] is None
    assert silent["tokens"]["input_tokens"] is None
    assert silent["coverage"] == {"observed": 0, "attempts": 1,
                                  "missing_recorded": 1, "unassessed": 0}
    assert desc["query"]["sort"] == {
        "key": "observed_tokens", "order": "desc",
        "secondary": "identity(project, run_id)"}


def test_explicit_zero_is_zero_unknown_is_none(qw):
    store = qw["store"]
    resp = query_model.tasks_get(store, qw["proj_a"], "R001", 11, "T002",
                                 now=FIXED_NOW)
    assert resp["usage"]["tokens"] == {"input_tokens": 250,
                                       "output_tokens": 25,
                                       "cached_input": 150}
    silent = query_model.tasks_get(store, qw["proj_a"], "R001", 12, "T001",
                                   now=FIXED_NOW)
    assert silent["usage"]["tokens"]["input_tokens"] is None


def test_progress_counts_current_revision_only(qw):
    resp = query_model.runs_get(qw["store"], qw["proj_a"], "R001",
                                now=FIXED_NOW)
    assert resp["run"]["progress"]["completed"] == 2
    assert resp["run"]["progress"]["total"] == 2
    assert resp["run"]["progress"]["revision"] == 2
    r010 = query_model.runs_list(qw["store"], run_id="R010",
                                 now=FIXED_NOW)
    assert r010["runs"][0]["progress"] == {
        "completed": 0, "total": 3, "revision_id": 21, "revision": 3,
        "basis": "tasks of the current (highest) revision only"}


# ------------------------------------------------------ snapshot pinning -----

def test_related_queries_use_the_same_successful_snapshot(qw):
    store = qw["store"]
    resp = query_model.runs_get(store, qw["proj_a"], "R001", now=FIXED_NOW)
    pinned = resp["snapshots"][qw["proj_a"]]["snapshot_id"]
    assert pinned == resp["usage"]["snapshot_id"]
    assert pinned == resp["native_preview"]["snapshot_id"]
    task = query_model.tasks_get(store, qw["proj_a"], "R001", 11, "T001",
                                 now=FIXED_NOW)
    assert task["snapshots"][qw["proj_a"]]["snapshot_id"] == pinned


def test_newer_failed_import_never_serves_data(qw):
    store = qw["store"]
    proj = qw["proj_a"]
    pinned = correlate.latest_snapshot(store, proj)
    # a later snapshot whose import FAILED: junk attempt + error import row
    cur = store.conn.execute(
        "INSERT INTO orx_snapshots(project_key,taken_at,schema_version,"
        "source_path) VALUES (?,?,?,?)",
        (proj, qw["taken_b"], "9", "synthetic"))
    broken = cur.lastrowid
    store.conn.execute(
        "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,role,"
        "profile,driver,harness,result,run_id) VALUES "
        "(?,270,11,'T001','worker','zcode-worker','host','zcode',"
        "'completed','R001')", (broken,))
    store.conn.execute(
        "INSERT INTO orx_usage_obs(snapshot_id,id,attempt_id,profile,run_id,"
        "task_id,input_tokens,output_tokens,cached_input,source,accuracy,"
        "created_at) VALUES (?,?,270,'zcode-worker','R001','T001',999999,"
        "99999,0,'native_cli','exact',?)",
        (broken, 370, qw["taken_b"]))
    store.conn.execute(
        "INSERT INTO import_runs(source_id, started_at, finished_at, "
        "status, counts_json, error) VALUES (?,?,?,'error',?,?)",
        (f"orx:{proj}", qw["taken_b"], qw["taken_b"],
         json.dumps({"orx_attempts": 1}), "synthetic failure"))
    store.conn.commit()
    assert correlate.latest_snapshot(store, proj) == broken  # fallback bait
    resp = query_model.runs_get(store, proj, "R001", now=FIXED_NOW)
    assert resp["snapshots"][proj]["snapshot_id"] == pinned
    assert resp["usage"]["tokens"]["input_tokens"] == 405249
    listed = query_model.runs_list(store, run_id="R001", project=proj,
                                   now=FIXED_NOW)
    assert listed["runs"][0]["consumption"]["tokens"][
        "input_tokens"] == 405249


# ------------------------------------------------------------- overview -----

def test_overview_active_attention_health(qw):
    store = qw["store"]
    now = _fresh_now(qw)
    resp = query_model.overview(store, now=now, stale_after=90.0)
    active_ids = [(a["project"], a["run_id"]) for a in resp["active"]]
    # R099 (unsupported-schema project) is isolated, never listed
    assert set(active_ids) == {(qw["proj_a"], "R010"),
                               (qw["proj_a"], "R011"),
                               (qw["proj_a"], "R012")}
    kinds = {(a["kind"], a["run_id"], a.get("task_id"))
             for a in resp["attention"]}
    # blocked run + a CURRENT failed task; the recovered rev1 T001 and the
    # retry-in-progress T102 stay out of the attention list
    assert kinds == {("blocked_run", "R011", None),
                     ("failed_task", "R010", "T101")}
    recent = [rid for _, rid in
              ((r["project"], r["run_id"]) for r in resp["recent_completed"])]
    assert recent == ["R021", "R020", "R001", "R001"]
    health = {h["project"]: h["data_health"]["health"]
              for h in resp["project_health"]}
    assert health[qw["proj_a"]] == display.HEALTH_OK
    assert health[qw["proj_b"]] == display.HEALTH_DELAYED
    assert health[qw["proj_c"]] == display.HEALTH_UNSUPPORTED
    assert health[qw["proj_d"]] == display.HEALTH_UNREADABLE
    codes = {w["code"] for w in resp["warnings"]}
    assert {"unknown-run-status", "unsupported-snapshot-schema",
            "no-snapshot"} <= codes


def test_overview_unknown_status_is_never_terminal(qw):
    resp = query_model.overview(qw["store"], now=FIXED_NOW,
                                stale_after=90.0)
    r012 = [a for a in resp["active"] if a["run_id"] == "R012"]
    assert r012 and r012[0]["status"]["display"] == display.UNKNOWN_LABEL
    assert r012[0]["status"]["raw"] == "weird"


# -------------------------------------------------------- projects & health --

def test_projects_list_reports_health_and_access(qw):
    store = qw["store"]
    resp = query_model.projects_list(store, now=_fresh_now(qw),
                                     stale_after=90.0)
    by_key = {p["project"]: p for p in resp["projects"]}
    assert set(by_key) == {qw["proj_a"], qw["proj_b"], qw["proj_c"],
                           qw["proj_d"], qw["proj_e"]}
    assert [p["project"] for p in resp["projects"]] == sorted(by_key)
    assert by_key[qw["proj_c"]]["data_health"]["health"] == \
        display.HEALTH_UNSUPPORTED
    assert by_key[qw["proj_d"]]["data_health"]["health"] == \
        display.HEALTH_UNREADABLE
    assert by_key[qw["proj_a"]]["data_health"]["health"] == display.HEALTH_OK
    assert by_key[qw["proj_b"]]["last_snapshot"]["taken_at"] == \
        qw["taken_b"]
    assert by_key[qw["proj_a"]]["runs_known"] == 6
    assert resp["page"]["total_matched"] == 5


def test_health_reports_protocol_sources_interfaces(qw):
    store = qw["store"]
    resp = query_model.health(store, now=_fresh_now(qw),
                              stale_after=90.0)
    assert resp["op"] == "health"
    assert set(query_model.OPS) <= set(resp["interfaces"])
    assert resp["reader_version"] and resp["store_version"]
    assert resp["engine"]["implemented"] is False
    proj = {p["project"]: p for p in resp["projects"]}
    assert proj[qw["proj_c"]]["data_health"]["health"] == \
        display.HEALTH_UNSUPPORTED
    assert isinstance(resp["global_sources"], list)
    for s in resp["global_sources"]:
        assert {"source_id", "kind", "location"} <= set(s)


# -------------------------------------------------------- not found & safe --

def test_not_found_is_structured_never_a_crash(qw):
    store = qw["store"]
    missing = query_model.runs_get(store, qw["proj_a"], "R404",
                                   now=FIXED_NOW)
    assert missing["found"] is False
    assert any(w["code"] == "run-not-found" for w in missing["warnings"])
    task = query_model.tasks_get(store, qw["proj_a"], "R001", 11, "T404",
                                 now=FIXED_NOW)
    assert task["found"] is False
    isolated = query_model.runs_get(store, qw["proj_c"], "R099",
                                    now=FIXED_NOW)
    assert isolated["found"] is False
    assert any(w["code"] == "unsupported-snapshot-schema"
               for w in isolated["warnings"])


def test_runs_list_validation_errors(qw):
    store = qw["store"]
    with pytest.raises(ValueError):
        query_model.runs_list(store, limit=-1)
    with pytest.raises(ValueError):
        query_model.runs_list(store, offset=-2)
    with pytest.raises(ValueError):
        query_model.runs_list(store, sort="bogus")
    with pytest.raises(ValueError):
        query_model.runs_list(store, order="sideways")


# ------------------------------------------------------- refresh contract ---

def test_refresh_request_contract_frozen():
    contract = query_model.refresh_contract()
    req = contract["request"]
    assert req["op"] == query_model.REFRESH_OP
    assert req["protocol_version"] == query_model.PROTOCOL_VERSION
    resp = contract["response"]
    assert resp["op"] == "refresh.accepted"
    assert "job_id" in resp["fields"]
    assert "merged_with" in resp["fields"]
    assert "unsupported_protocol" in resp["errors"]


def test_validate_refresh_request():
    ok = query_model.validate_refresh_request(
        {"op": "refresh.request", "protocol_version": 1,
         "projects": ["/a"], "reason": "panel-open"})
    assert ok == {"op": "refresh.request", "protocol_version": 1,
                  "projects": ["/a"], "reason": "panel-open"}
    minimal = query_model.validate_refresh_request(
        {"op": "refresh.request"})
    assert minimal["projects"] is None and minimal["reason"] is None
    with pytest.raises(ValueError):
        query_model.validate_refresh_request({"op": "overview"})
    with pytest.raises(ValueError):
        query_model.validate_refresh_request(
            {"op": "refresh.request", "protocol_version": 99})
    with pytest.raises(ValueError):
        query_model.validate_refresh_request(
            {"op": "refresh.request", "projects": [7]})


# ------------------------------------------------- lifecycle layering (G011) ---
# docs/diagnostics/g011/lifecycle-matrix.md: the Goal control state and the
# Run execution facts are two explicit layers. Grouping, attention, counting
# and badges follow the matrix; the raw statuses are never rewritten.


@pytest.fixture()
def lw(tmp_path):
    """A lifecycle world: one project whose snapshot carries every matrix
    cell as its own goal — paused, cancelling, cancelled, done and an
    unrecognized control value, each with run/task/attempt facts."""
    from orxanalytics.store import Store

    proj = tmp_path / "ProjL"
    (proj / ".orx").mkdir(parents=True)
    make_orx_db(proj / ".orx" / "state.db")
    store = Store(tmp_path / "analytics-home")
    import_orx_project(store, str(proj))
    registry.add_project(store, str(proj))
    seed_lifecycle(store, str(proj))
    return {"store": store, "proj": str(proj)}


def seed_lifecycle(store, proj: str) -> None:
    snap = correlate.latest_snapshot(store, proj)
    conn = store.conn
    goals = [
        ("G100", "lifecycle paused goal", "paused"),
        ("G101", "lifecycle cancelling goal", "cancelling"),
        ("G102", "lifecycle cancelled goal", "cancelled"),
        ("G103", "lifecycle done goal", "done"),
        ("G104", "lifecycle unknown control goal", "hibernating"),
    ]
    conn.executemany(
        "INSERT INTO orx_goals VALUES (?,?,?,?,?,?,?,?,?)",
        [(snap, gid, obj, st, "", "[]", "[]", T(0), T(150))
         for gid, obj, st in goals])
    # one run per matrix cell; goal G001/R001 from the base fixture stays
    # as the active-goal reference (done run)
    runs = [
        # paused/running and paused/done
        ("R100", "G100", "running", T(0), T(121), T(100), None),
        ("R101", "G100", "done", T(0), T(145), T(130), T(140)),
        # cancelling with an UNCLOSED execution; cancelling/done is history
        ("R110", "G101", "running", T(0), T(122), T(101), None),
        ("R111", "G101", "done", T(0), T(146), T(130), T(141)),
        # cancelled: blocked run with a failed task, and a done run
        ("R120", "G102", "blocked", T(0), T(123), T(102), None),
        ("R121", "G102", "done", T(0), T(147), T(130), T(142)),
        # done/done
        ("R130", "G103", "done", T(0), T(148), T(130), T(143)),
        # unknown control value over a running execution fact
        ("R140", "G104", "running", T(0), T(124), T(103), None),
    ]
    conn.executemany("INSERT INTO orx_runs VALUES (?,?,?,?,?,?,?,?)",
                     [(snap, *row) for row in runs])
    conn.executemany(
        "INSERT INTO orx_revisions VALUES (?,?,?,?,?,?,?,?)",
        [(snap, rid, run, 1, 1, "planner-l", "active", T(90))
         for rid, run in ((50, "R100"), (51, "R101"), (52, "R110"),
                          (53, "R111"), (54, "R120"), (55, "R121"),
                          (56, "R130"), (57, "R140"))])
    tasks = [
        # the cancelling goal's unconfirmed worker execution: task running,
        # attempt still open (ended_at NULL, result NULL)
        (140, 52, "T110", "unclosed execution under cancelling", "running",
         None, T(91), T(122)),
        # the cancelled goal's failed task: a REAL historical failure with
        # usage, never a current attention fact
        (150, 54, "T120", "failed task under cancelled goal", "failed",
         "boom", T(91), T(120)),
    ]
    conn.executemany("INSERT INTO orx_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                     [(snap, *t) for t in tasks])
    attempts = [
        (340, 52, "T110", "worker", "zcode-worker", "host", "zcode", "m9",
         T(92), None, None, None, "R110", None),
        (350, 54, "T120", "worker", "zcode-worker", "host", "zcode", "m9",
         T(92), T(115), "failed", "boom", "R120", None),
    ]
    conn.executemany(
        "INSERT INTO orx_attempts(snapshot_id,id,revision_id,task_id,role,"
        "profile,driver,harness,model,started_at,ended_at,result,"
        "failure_reason,run_id,usage_missing_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(snap, *row) for row in attempts])
    conn.execute(
        "INSERT INTO orx_usage_obs(snapshot_id,id,attempt_id,profile,run_id,"
        "task_id,input_tokens,output_tokens,cached_input,source,accuracy,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (snap, 360, 350, "zcode-worker", "R120", "T120", 4000, 400, 0,
         "native_cli", "exact", T(115)))
    conn.commit()


def _lifecycle_overview(lw):
    return query_model.overview(lw["store"], now=FIXED_NOW, stale_after=90.0)


def test_overview_groups_by_the_lifecycle_matrix(lw):
    resp = _lifecycle_overview(lw)
    active_ids = {a["run_id"] for a in resp["active"]}
    # paused/running, cancelling/unclosed and unknown-control/running stay
    # current executions; the base fixture's done R001 is recent, not active
    assert active_ids == {"R100", "R110", "R140"}
    recent_ids = {r["run_id"] for r in resp["recent_completed"]}
    # paused/done (R101) and done/done (R130) are completions; cancelling/
    # done (R111) and both cancelled runs (R120, R121) never aggregate as
    # completions — they are history only
    assert recent_ids == {"R001", "R101", "R130"}
    listed = active_ids | recent_ids
    assert {"R111", "R120", "R121"}.isdisjoint(listed)
    # summary mirrors the lists exactly — one bucket per run, no double count
    assert resp["summary"]["active_runs"] == len(resp["active"])
    assert resp["summary"]["recent_completed"] == len(resp["recent_completed"])
    assert resp["summary"]["attention"] == len(resp["attention"])


def test_active_cards_carry_both_layers(lw):
    resp = _lifecycle_overview(lw)
    by_run = {a["run_id"]: a for a in resp["active"]}
    # paused/running: execution fact 执行中, control badge 已暂停 — the two
    # dimensions travel together and neither rewrites the other
    assert by_run["R100"]["status"]["display"] == "执行中"
    assert by_run["R100"]["goal"]["status"]["raw"] == "paused"
    assert by_run["R100"]["goal"]["status"]["display"] == "已暂停"
    # cancelling keeps the unclosed execution visible with its control state
    assert by_run["R110"]["status"]["raw"] == "running"
    assert by_run["R110"]["goal"]["status"]["display"] == "取消中"
    # an unrecognized control value stays unknown, never guessed terminal
    assert by_run["R140"]["goal"]["status"]["known"] is False
    assert by_run["R140"]["goal"]["status"]["display"] == display.UNKNOWN_LABEL
    codes = {w["code"] for w in resp["warnings"]}
    assert "unknown-goal-status" in codes


def test_run_done_during_pause_is_not_goal_done(lw):
    resp = _lifecycle_overview(lw)
    recent = {r["run_id"]: r for r in resp["recent_completed"]}
    # paused/done: the run completed as an execution fact, but the item
    # still carries the paused control layer — it never masquerades as a
    # goal completion (R130 is the only done-goal completion here)
    assert recent["R101"]["status"]["display"] == "完成"
    assert recent["R101"]["goal"]["status"]["raw"] == "paused"
    assert recent["R101"]["goal"]["status"]["display"] == "已暂停"
    assert recent["R130"]["goal"]["status"]["raw"] == "done"


def test_cancelling_keeps_the_unclosed_execution_facts(lw):
    resp = _lifecycle_overview(lw)
    card = next(a for a in resp["active"] if a["run_id"] == "R110")
    assert card["goal"]["status"]["raw"] == "cancelling"
    detail = query_model.runs_get(lw["store"], lw["proj"], "R110",
                                  now=FIXED_NOW)
    task = next(t for t in detail["tasks"] if t["task_id"] == "T110")
    assert task["status"]["raw"] == "running"
    open_atts = usage_readmodel.task_usage(
        lw["store"], lw["proj"], "R110", 52, "T110")["attempts"]
    assert open_atts and open_atts[0]["result"] is None


def test_cancelled_goal_never_earns_attention(lw):
    resp = _lifecycle_overview(lw)
    # R120 is a blocked run with a genuinely failed task, but the goal is
    # cancelled: no blocked_run, no failed_task, nothing to notify
    for item in resp["attention"]:
        assert item["run_id"] not in {"R120", "R121", "R111"}
    assert resp["attention"] == []


def test_cancelled_history_failures_and_usage_stay_queryable(lw):
    store, proj = lw["store"], lw["proj"]
    blocked = query_model.runs_list(store, project=proj, status="blocked",
                                    now=FIXED_NOW)
    assert [r["run_id"] for r in blocked["runs"]] == ["R120"]
    assert blocked["runs"][0]["goal"]["status"]["display"] == "已取消"
    detail = query_model.runs_get(store, proj, "R120", now=FIXED_NOW)
    task = next(t for t in detail["tasks"] if t["task_id"] == "T120")
    assert task["status"]["raw"] == "failed"
    assert task["failure_reason"] == "boom"
    assert task["consumption"]["tokens"]["input_tokens"] == 4000
    task_detail = query_model.tasks_get(store, proj, "R120", 54, "T120",
                                        now=FIXED_NOW)
    assert task_detail["retry"]["failing_attempt_ids"] == [350]
    assert task_detail["usage"]["tokens"]["input_tokens"] == 4000
    # the raw statuses are untouched by every read
    row = store.conn.execute(
        "SELECT status FROM orx_runs WHERE snapshot_id=("
        "SELECT MAX(snapshot_id) FROM orx_runs) AND id='R120'").fetchone()
    assert row["status"] == "blocked"


def test_runs_list_shows_both_dimensions_for_lifecycle_rows(lw):
    resp = query_model.runs_list(lw["store"], project=lw["proj"],
                                 now=FIXED_NOW)
    by_run = {r["run_id"]: r for r in resp["runs"]}
    # run blocked under an active control state is 受阻 (execution layer);
    # the control layer rides along as its own status block
    assert by_run["R120"]["status"]["display"] == "受阻"
    assert by_run["R120"]["goal"]["status"]["display"] == "已取消"
    assert by_run["R100"]["status"]["display"] == "执行中"
    assert by_run["R100"]["goal"]["status"]["display"] == "已暂停"
