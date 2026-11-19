"""Coordination engine (design §7/§8, G002 T006).

One engine per data-dir, guarded by a system file lock (fcntl) — a second
engine must never produce double collection. The engine serves T003's six
query interfaces plus ``refresh.request`` over a user-private Unix domain
socket with a versioned JSON protocol; App and CLI each hold their own
collection demand on their own connection, so one side exiting never stops
the other's collection. Refresh requests coalesce into a bounded pending
job; state refresh, usage import and anomaly analysis are scheduled on
separate cadences (30/60/300s tiers, lowered from 5/15/60s by the G012
user-feedback cadence fix); change detection lowers overhead with
periodic full re-checks; one project's failure never touches another's and
never drops its last successful data; sleep never produces stall verdicts
and wake forces a re-sync; crashes restart a bounded number of times with
backoff. A live legacy standalone watcher is detected and reported with
migration guidance — never killed by PID. Protocol incompatibilities are
reported, never force-taken-over.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_orx_db
from orxanalytics import engine as eng
from orxanalytics import query_model, registry
from orxanalytics.store import Store

_seq = itertools.count()


# ----------------------------------------------------------------- helpers ---

def seed_project(root: Path) -> Path:
    (root / ".orx").mkdir(parents=True, exist_ok=True)
    make_orx_db(root / ".orx" / "state.db")
    return root


class FakeClock:
    """A mutable wall clock the tests advance by hand."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def home():
    """A data dir whose path fits an AF_UNIX socket path (sun_path is
    ~104 bytes; pytest's tmp_path is longer on macOS)."""
    root = Path("/tmp") / f"orx-engine-test-{os.getpid()}-{next(_seq)}"
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def proj(tmp_path):
    return seed_project(tmp_path / "ProjA")


def register(store_dir: Path, *roots: Path) -> None:
    store = Store(store_dir)
    try:
        for root in roots:
            registry.add_project(store, str(root))
    finally:
        store.close()


def snapshot_count(store_dir: Path, project_key: str) -> int:
    store = Store(store_dir)
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) c FROM orx_snapshots WHERE project_key=?",
            (project_key,)).fetchone()
        return row["c"]
    finally:
        store.close()


# =========================================================== single instance ===

def test_second_engine_cannot_start_or_collect(home, proj):
    """单实例强保证：第二个引擎不产生双采集."""
    register(home, proj)
    e1 = eng.Engine(home, clock=FakeClock())
    e1.acquire()
    e2 = eng.Engine(home, clock=FakeClock())
    with pytest.raises(eng.EngineAlreadyRunning):
        e2.acquire()
    # the losing engine collected nothing and bound nothing
    assert not eng.socket_path(home).exists()
    with pytest.raises(RuntimeError):
        e2.tick()
    e1.release()
    # the lock died with the holder: a fresh engine takes over cleanly
    e3 = eng.Engine(home, clock=FakeClock())
    e3.acquire()
    e3.release()


def test_lock_is_a_system_file_lock_not_a_pid_file(home):
    """The flock survives PID-file tricks: a live *other process* holding
    the lock blocks us even though our pidfile knowledge says nothing."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import time\n"
         "from orxanalytics.engine import SingleInstanceLock\n"
         f"lock = SingleInstanceLock({str(home)!r})\n"
         "lock.acquire()\n"
         "print('held', flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        e = eng.Engine(home, clock=FakeClock())
        with pytest.raises(eng.EngineAlreadyRunning):
            e.acquire()
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # process death releases the kernel-held lock without any cleanup code
    e = eng.Engine(home, clock=FakeClock())
    e.acquire()
    e.release()


# ============================================================ socket protocol ===

def _eventually(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _started_engine(home, proj, **kw):
    register(home, proj)
    clock = kw.pop("clock", FakeClock())
    e = eng.Engine(home, clock=clock, **kw)
    e.acquire()
    e.start_server()
    e.tick()          # first round imports the synthetic project
    return e


def test_socket_serves_six_interfaces_over_json_lines(home, proj):
    e = _started_engine(home, proj)
    try:
        path = eng.socket_path(home)
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600, "socket must be user-private"
        client = eng.EngineClient(path)
        try:
            key = str(proj)
            for op, params, probe in (
                ("overview", {}, lambda r: r["summary"]["active_runs"] >= 0),
                ("runs.list", {}, lambda r: len(r["runs"]) == 1),
                ("runs.get", {"project": key, "run_id": "R001"},
                 lambda r: r["found"] is True),
                ("tasks.get", {"project": key, "run_id": "R001",
                               "revision_id": 11, "task_id": "T001"},
                 lambda r: r["found"] is True),
                ("projects.list", {}, lambda r: len(r["projects"]) == 1),
                ("health", {}, lambda r: r["engine"]["implemented"] is True),
            ):
                resp = client.request({"op": op, **params})
                assert resp["op"] == op, resp
                assert resp["ok"] is True, resp
                assert resp["protocol_version"] == query_model.PROTOCOL_VERSION
                for k in ("generated_at", "snapshots", "coverage", "warnings"):
                    assert k in resp
                assert probe(resp), resp
            # health reports live engine facts, not the pure layer's stub
            health = client.request({"op": "health"})
            facts = health["engine"]
            assert facts["protocol_version"] == query_model.PROTOCOL_VERSION
            assert facts["socket_path"] == str(path)
            assert facts["data_dir"] == str(home)
            assert "tier" in facts and "lanes" in facts
            assert facts["legacy_watcher"] is None
        finally:
            client.close()
    finally:
        e.shutdown()


def test_protocol_version_mismatch_reported_not_forced(home, proj):
    """socket 协议带版本，不兼容明确报告 (no take-over, engine stays up)."""
    e = _started_engine(home, proj)
    try:
        client = eng.EngineClient(eng.socket_path(home))
        try:
            bad = client.request({"op": "overview", "protocol_version": 99})
            assert bad["ok"] is False
            assert bad["error"]["code"] == "unsupported_protocol"
            assert bad["error"]["supported"] == query_model.PROTOCOL_VERSION
            assert bad["error"]["requested"] == 99
            # refresh too: an incompatible version is never accepted
            bad_refresh = client.request({"op": "refresh.request",
                                          "protocol_version": 0})
            assert bad_refresh["error"]["code"] == "unsupported_protocol"
            # the same engine keeps serving compatible clients afterwards
            ok = client.request({"op": "overview"})
            assert ok["ok"] is True
            assert ok["protocol_version"] == query_model.PROTOCOL_VERSION
        finally:
            client.close()
    finally:
        e.shutdown()


def test_unknown_op_and_bad_json_report_errors(home, proj):
    e = _started_engine(home, proj)
    try:
        client = eng.EngineClient(eng.socket_path(home))
        try:
            resp = client.request({"op": "nope"})
            assert resp["ok"] is False and resp["error"]["code"] == "unknown_op"
            assert set(resp["error"]["supported"]) >= set(query_model.OPS) | {
                query_model.REFRESH_OP}
            resp = client.request({"op": "runs.list", "limit": -1})
            assert resp["ok"] is False and resp["error"]["code"] == "invalid_request"
        finally:
            client.close()
        # raw non-JSON line over the socket
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(5)
        raw.connect(str(eng.socket_path(home)))
        try:
            raw.sendall(b"this is not json\n")
            line = raw.makefile("rb").readline()
            resp = json.loads(line)
            assert resp["ok"] is False and resp["error"]["code"] == "invalid_request"
        finally:
            raw.close()
    finally:
        e.shutdown()


# ============================================================== App/CLI 共存 ===

def test_app_cli_hold_their_own_demand(home, proj):
    """App/CLI 共存：各自持有采集需求，一方退出不停止另一方要求的采集."""
    e = _started_engine(home, proj)
    try:
        path = eng.socket_path(home)
        app = eng.EngineClient(path)
        cli = eng.EngineClient(path)
        try:
            assert e.tier()[0] == "idle"
            r1 = app.request({"op": "refresh.request", "reason": "panel-open"})
            assert r1["ok"] is True
            assert e.tier()[0] == "ui"
            r2 = cli.request({"op": "refresh.request", "reason": "background"})
            assert r2["ok"] is True
            assert e.tier()[0] == "ui"          # strongest demand wins
            # one side exits: only ITS demand goes away (the engine's
            # connection handler releases exactly that client's demand)
            app.close()
            assert _eventually(lambda: e.tier()[0] == "active")
            still = cli.request({"op": "health"})
            assert still["ok"] is True          # and is still served
            cli.close()
            assert _eventually(lambda: e.tier()[0] == "idle")
        finally:
            app.close()
            cli.close()
    finally:
        e.shutdown()


def test_refresh_requests_coalesce_bounded(home, proj):
    """refresh.request 合并有界: many requests -> one pending job."""
    e = _started_engine(home, proj)
    try:
        ids = set()
        for _ in range(10):
            r = e.handle_request(
                {"op": "refresh.request", "reason": "panel-open",
                 "projects": [str(proj)]}, client_id="c1")
            assert r["ok"] is True and r["op"] == "refresh.accepted"
            assert r["merged_with"] is not None or not ids
            if not ids:
                first = r["job_id"]
                assert r["merged_with"] is None
            else:
                assert r["job_id"] == first and r["merged_with"] == first
            ids.add(r["job_id"])
        assert ids == {first}
        pending = e.jobs.pending()
        assert pending is not None
        assert e.jobs.pending_count() == 1
        # a request whose projects are ALL rejected creates no job at all;
        # the earlier pending job stays untouched and bounded
        r = e.handle_request({"op": "refresh.request", "reason": "manual",
                              "projects": ["/nonexistent/proj"]},
                             client_id="c1")
        assert r["ok"] is True
        assert r["job_id"] is None
        assert e.jobs.pending_count() == 1
        # unregistered projects are rejected honestly
        assert any(x["project"] == "/nonexistent/proj" for x in r["rejected"])
    finally:
        e.shutdown()


def test_refresh_job_runs_once_not_stacked(home, proj):
    """单来源任务有界排队: the pending job imports the project once."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        base = snapshot_count(home, str(proj))
        for _ in range(5):
            e.handle_request({"op": "refresh.request", "reason": "manual"},
                             client_id="c1")
        clock.advance(1)
        e.tick()
        assert snapshot_count(home, str(proj)) == 1
        # while a job was pending, per-source queueing stayed bounded
        assert e.jobs.pending_count() == 0    # job consumed by the tick
    finally:
        e.release()


# ================================================================ scheduling ===

def test_scheduler_tiers_and_lane_cadences(home, proj):
    """调度分级 30/60/300s (G012 降低默认频率); 状态/用量/异常分别调度;
    历史核对低频或手动."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        # the NEW default tiers, verbatim (G012 用户反馈 4: 刷新过于频繁)
        assert eng.TIER_INTERVALS == {"ui": 30.0, "active": 60.0,
                                      "idle": 300.0}
        assert e.tier() == ("idle", 300.0)
        e.hold_demand("cli", "background")
        assert e.tier() == ("active", 60.0)
        e.hold_demand("app", "ui")
        assert e.tier() == ("ui", 30.0)

        report = e.tick()
        lanes = report["lanes"]
        assert set(lanes) >= {"state", "usage", "anomalies"}
        assert lanes["state"]["runs"] == 1
        # separate cadences: at ui tier, the next state round is due in 30s
        # while usage waits 4x and anomalies 2x that
        clock.advance(30)
        report = e.tick()
        assert report["lanes"]["state"]["runs"] == 2
        assert report["lanes"]["usage"]["runs"] == 1
        assert report["lanes"]["anomalies"]["runs"] == 1
        clock.advance(30)
        report = e.tick()
        assert report["lanes"]["state"]["runs"] == 3
        assert report["lanes"]["anomalies"]["runs"] == 2
        assert report["lanes"]["usage"]["runs"] == 1
        clock.advance(60)               # t=120: usage due (4x ui tier)
        report = e.tick()
        assert report["lanes"]["state"]["runs"] == 4   # t=0/30/60/120
        assert report["lanes"]["usage"]["runs"] == 2
        # history audit is low-frequency: not due for a long time
        assert report["lanes"]["history_audit"]["runs"] == 0
        assert e.lane_next_due("history_audit") - clock() > 900
        # manual trigger runs it immediately
        e.request_history_audit()
        clock.advance(30)
        report = e.tick()
        assert report["lanes"]["history_audit"]["runs"] == 1
    finally:
        e.release()


def test_lower_default_cadence_controlled_clock_comparison(home, proj):
    """G012 用户反馈 4：受控时钟对比同一时间窗口内旧、新默认节律.

    The OLD defaults (ui 5s / active 15s / idle 60s with 30s/90s leases)
    are simulated with the engine's injectable intervals; the NEW defaults
    are the module constants. Over the SAME 300s window at the ui tier the
    new cadence must run every lane strictly fewer times. The clock is
    fake — no test sleeps for the window.
    """
    old_tiers = {"ui": 5.0, "active": 15.0, "idle": 60.0}

    def lane_runs(window: float, step: float, intervals: dict | None,
                  leases: dict | None) -> dict[str, int]:
        register(home, proj)
        clock = FakeClock()
        kwargs: dict = {"clock": clock}
        if intervals is not None:
            kwargs["intervals"] = intervals
        e = eng.Engine(home, **kwargs)
        if leases is not None:
            e.demand = eng.DemandTracker(leases)
        e.acquire()
        try:
            runs = {"state": 0, "usage": 0, "anomalies": 0}
            t = 0.0
            while t <= window:
                e.hold_demand("app", "ui")   # a client that renews on time
                report = e.tick()
                for name in runs:
                    if report["lanes"][name].get("ran"):
                        runs[name] += 1
                clock.advance(step)
                t += step
            return runs
        finally:
            e.release()

    old = lane_runs(300, 1.0, old_tiers, {"ui": 30.0, "background": 90.0})
    new = lane_runs(300, 1.0, None, None)
    # same window, strictly fewer automatic rounds on every lane
    for name in ("state", "usage", "anomalies"):
        assert new[name] < old[name], (name, old[name], new[name])
    # and the exact new expectations at the ui tier (30/120/60s lanes)
    assert old["state"] == 61 and new["state"] == 11     # 5s -> 30s
    assert old["usage"] == 16 and new["usage"] == 3      # 20s -> 120s
    assert old["anomalies"] == 31 and new["anomalies"] == 6   # 10s -> 60s


def test_demand_leases_aligned_with_lower_cadence(home, proj):
    """G012：需求租约与新节律一致.

    A UI client renews its lease once per refresh round (default 30s
    now). The lease must comfortably outlive the interval — a single
    missed round must not drop the tier — while still expiring when the
    client really goes away without renewal."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        # alignment invariants: lease covers at least two full intervals
        assert eng.DEMAND_LEASE_SECONDS["ui"] >= 2 * eng.TIER_INTERVALS["ui"]
        assert (eng.DEMAND_LEASE_SECONDS["background"]
                >= 2 * eng.TIER_INTERVALS["active"])
        # behavior: one hold survives past the whole ui interval
        e.hold_demand("app", "ui")
        clock.advance(eng.TIER_INTERVALS["ui"] + 1)
        assert e.tier()[0] == "ui", "one missed renewal must not drop the tier"
        # ...but a client that never renews eventually releases the tier
        clock.advance(eng.DEMAND_LEASE_SECONDS["ui"])
        assert e.tier()[0] == "idle", "unrenewed demand must expire"
        # same for the CLI holder's background demand
        e.hold_demand("cli", "background")
        clock.advance(eng.TIER_INTERVALS["active"] + 1)
        assert e.tier()[0] == "active"
        clock.advance(eng.DEMAND_LEASE_SECONDS["background"])
        assert e.tier()[0] == "idle"
    finally:
        e.release()


def test_change_detection_and_periodic_full_recheck(home, proj):
    """变化检测降开销 + 定期完整核对 (not file-time-forever)."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        assert snapshot_count(home, str(proj)) == 1
        # unchanged files: skipped round, no new snapshot (idle tier 300s)
        clock.advance(300)
        report = e.tick()
        action = report["lanes"]["state"]["projects"][str(proj)]["action"]
        assert action == "unchanged"
        assert snapshot_count(home, str(proj)) == 1
        # the periodic full re-check eventually re-imports unchanged files
        clock.advance(e.FULL_RECHECK_INTERVAL + 60)
        report = e.tick()
        action = report["lanes"]["state"]["projects"][str(proj)]["action"]
        assert action == "full-recheck"
        assert snapshot_count(home, str(proj)) == 1
        # a real change re-imports (signature gate: stat, not content)
        # the signature is WAL-aware: touching -wal changes it even when
        # state.db itself is untouched
        sig1 = e._source_signature(str(proj))
        (proj / ".orx" / "state.db-wal").write_bytes(b"x" * 32)
        assert e._source_signature(str(proj)) != sig1
        (proj / ".orx" / "state.db-wal").unlink()
        os.utime(proj / ".orx" / "state.db", None)
        clock.advance(300)
        report = e.tick()
        action = report["lanes"]["state"]["projects"][str(proj)]["action"]
        assert action == "imported"
        assert snapshot_count(home, str(proj)) == 1
    finally:
        e.release()


def test_per_project_failure_isolation_keeps_last_success(home, tmp_path):
    """单项目失败不影响其他项目，失败保留最后成功数据附失败原因与旧数据时间."""
    good_a = seed_project(tmp_path / "ProjA")
    good_b = seed_project(tmp_path / "ProjB")
    register(home, good_a, good_b)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        assert snapshot_count(home, str(good_a)) == 1
        assert snapshot_count(home, str(good_b)) == 1

        from orxanalytics import collect as collect_mod
        real = collect_mod.import_orx_project

        def flaky(store, key):
            if key == str(good_b):
                raise RuntimeError("boom: source became unreadable")
            return real(store, key)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(collect_mod, "import_orx_project", flaky)
        try:
            clock.advance(60)
            # a manual refresh for the failing project bypasses the
            # change-detection gate and reaches the failing importer
            e.handle_request({"op": "refresh.request", "reason": "manual",
                              "projects": [str(good_b)]}, client_id="t")
            report = e.tick()   # must not raise: one project cannot kill the round
        finally:
            monkeypatch.undo()
        state = report["lanes"]["state"]["projects"]
        assert state[str(good_a)]["action"] in ("imported", "unchanged")
        assert state[str(good_b)]["action"] == "error"
        status = e.project_status()[str(good_b)]
        assert "boom" in status["last_error"]
        assert status["error_at"] is not None
        assert status["last_success_at"] is not None   # old data time kept
        # the failure keeps the last successful snapshot servable
        resp = e.handle_request({"op": "runs.get", "project": str(good_b),
                                 "run_id": "R001"})
        assert resp["ok"] is True and resp["found"] is True
        # and the other project kept collecting
        assert snapshot_count(home, str(good_a)) >= 1
    finally:
        e.release()


def test_active_runs_raise_the_tier_without_any_client(home, proj):
    """后台存在活动执行 -> ~15s even with no client demand."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        assert e.tier()[0] == "idle"        # synthetic run R001 is 'done'
        # make the live project's only run active in a fresh snapshot
        import sqlite3
        db = proj / ".orx" / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE runs SET status='running'")
        conn.commit()
        conn.close()
        os.utime(db, None)                 # signature change -> re-import
        clock.advance(300)                 # idle tier is 300s now
        e.tick()
        assert e.tier()[0] == "active"
    finally:
        e.release()


def test_cancelled_goal_does_not_hold_the_active_tier(home, proj):
    """G011 分层计数：cancelled Goal 下的 Run 不再算活动执行.

    The tier's active-run count follows the lifecycle matrix — a run whose
    GOAL control state is cancelled leaves the active cadence even while
    its raw run status is still 'running' (the status itself is never
    rewritten)."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        import sqlite3
        db = proj / ".orx" / "state.db"

        def set_state(goal_status: str | None, run_status: str | None) -> None:
            conn = sqlite3.connect(db)
            if goal_status is not None:
                conn.execute("UPDATE goals SET status=?", (goal_status,))
            if run_status is not None:
                conn.execute("UPDATE runs SET status=?", (run_status,))
            conn.commit()
            conn.close()
            os.utime(db, None)             # signature change -> re-import

        e.tick()
        assert e.tier()[0] == "idle"        # base fixture: R001 done
        # run running under an active goal -> active cadence (the first
        # re-check happens at the idle interval, 300s now)
        set_state("active", "running")
        clock.advance(300)
        e.tick()
        assert e.tier()[0] == "active"
        # paused control state: the run keeps executing (in-flight work is
        # not starved) -> still the active cadence (60s tier)
        set_state("paused", None)
        clock.advance(60)
        e.tick()
        assert e.tier()[0] == "active"
        # goal cancelled while the run row still says running -> the run is
        # history, not a current execution; the raw run status is untouched
        set_state("cancelled", None)
        clock.advance(60)
        e.tick()
        assert e.tier()[0] == "idle"
        conn = sqlite3.connect(db)
        raw = conn.execute("SELECT status FROM runs").fetchone()[0]
        conn.close()
        assert raw == "running"             # never rewritten by analytics
    finally:
        e.release()


def test_sleep_wake_no_stall_verdict_and_resync(home, proj):
    """睡眠期间不判停摆、唤醒后重新同步."""
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        assert e.last_wake() is None
        base_snaps = snapshot_count(home, str(proj))
        # a normal gap is not a wake
        clock.advance(60)
        report = e.tick()
        assert report["wake"] is False
        assert report["lanes"]["anomalies"] is not None
        # an 8-hour gap looks like system sleep: re-sync now, and the
        # anomaly lane stays silent for that round (no stall verdicts)
        clock.advance(8 * 3600)
        report = e.tick()
        assert report["wake"] is True
        assert report["lanes"]["anomalies"]["skipped"] == "wake-resync"
        assert report["lanes"]["state"]["forced"] is True
        assert e.last_wake() is not None
        assert snapshot_count(home, str(proj)) >= base_snaps
        # after the re-sync, anomaly scheduling resumes normally
        clock.advance(60)
        report = e.tick()
        assert report["wake"] is False
        assert report["lanes"]["anomalies"]["skipped"] is None
    finally:
        e.release()


# ============================================================ legacy watcher ===

def test_legacy_live_watcher_reported_never_killed(home):
    """检测旧版 watcher 活体给出迁移指引且不杀进程."""
    (home).mkdir(parents=True, exist_ok=True)
    # our own pid is the simplest certainly-alive process; killing it would
    # destroy the test run, which is exactly what must NOT happen
    (home / "watch.pid").write_text(str(os.getpid()))
    detected = eng.detect_legacy_watcher(home)
    assert detected is not None
    assert detected["detected"] is True
    assert detected["pid"] == os.getpid()
    assert "watch stop" in detected["guidance"]
    os.kill(os.getpid(), 0)   # still alive: the engine never sent a signal


def test_legacy_dead_pidfile_not_reported(home, tmp_path):
    home.mkdir(parents=True, exist_ok=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    (home / "watch.pid").write_text(str(dead.pid))
    assert eng.detect_legacy_watcher(home) is None


def test_health_carries_legacy_watcher_guidance(home, proj):
    register(home, proj)          # creates the data dir first
    (home / "watch.pid").write_text(str(os.getpid()))
    e = eng.Engine(home, clock=FakeClock())
    e.acquire()
    try:
        resp = e.handle_request({"op": "health"})
        lw = resp["engine"]["legacy_watcher"]
        assert lw is not None and lw["detected"] is True
        assert "watch stop" in lw["guidance"]
        os.kill(os.getpid(), 0)
    finally:
        e.release()


# ============================================================== crash restart ===

def test_supervisor_limited_restarts_with_backoff(home):
    """引擎崩溃有限重启+退避."""
    sleeps: list[float] = []
    attempts = {"n": 0}

    class Flaky:
        def run(self):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("crash")
            return "ok"

    sup = eng.EngineSupervisor(lambda: Flaky(), sleep=sleeps.append,
                               max_restarts=5, state_dir=home)
    report = sup.run()
    assert report["gave_up"] is False
    assert report["restarts"] == 2
    assert report["last_result"] == "ok"
    assert sleeps == [1.0, 2.0]           # exponential backoff


def test_supervisor_gives_up_after_limit_with_diagnostics(home):
    sleeps: list[float] = []

    class AlwaysCrash:
        def run(self):
            raise RuntimeError("fatal loop")

    sup = eng.EngineSupervisor(lambda: AlwaysCrash(), sleep=sleeps.append,
                               max_restarts=3, state_dir=home)
    report = sup.run()
    assert report["gave_up"] is True
    assert report["restarts"] == 3
    assert "fatal loop" in report["last_error"]
    assert sleeps == [1.0, 2.0, 4.0]
    diag = json.loads((home / "engine.state.json").read_text())
    assert diag["gave_up"] is True
    assert diag["restarts"] == 3
    assert "fatal loop" in diag["last_error"]
    # backoff is capped
    assert eng.EngineSupervisor.backoff(9) == eng.BACKOFF_CAP


# ================================================================= threading ===

def test_engine_start_serves_and_shuts_down_cleanly(home, proj):
    register(home, proj)
    e = eng.Engine(home, clock=FakeClock())
    e.acquire()
    e.start_server()
    try:
        client = eng.EngineClient(eng.socket_path(home))
        try:
            resp = client.request({"op": "health"})
            assert resp["ok"] is True
        finally:
            client.close()
    finally:
        e.shutdown()
    assert not eng.socket_path(home).exists()
    # after shutdown the instance slot is free again
    e2 = eng.Engine(home, clock=FakeClock())
    e2.acquire()
    e2.release()


# ============================================================ module entry ===

def test_module_entry_serves_the_socket_subprocess(home, proj):
    """``python -m orxanalytics.engine`` must serve the socket, not just
    run the scheduler — this is the real spawn path of ``watch start``
    and of a bundle-less install. Regression: main() used to acquire and
    loop without ever binding the socket (surfaced by T012 packaging)."""
    import subprocess
    import sys
    import time as _time
    proc = subprocess.Popen(
        [sys.executable, "-m", "orxanalytics.engine", "--data-dir", str(home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        path = eng.socket_path(home)
        deadline = _time.time() + 20
        while not path.exists() and _time.time() < deadline:
            assert proc.poll() is None, "engine subprocess exited early"
            _time.sleep(0.1)
        assert path.exists(), "engine.sock never bound by the module entry"
        client = eng.EngineClient(path)
        try:
            resp = client.request({"op": "health"})
            assert resp["ok"] is True
            assert resp["protocol_version"] == query_model.PROTOCOL_VERSION
        finally:
            client.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
