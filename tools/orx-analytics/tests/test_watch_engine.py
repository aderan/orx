"""watch.py engine integration (design §7 进程协作, G002 T007).

``watch start`` no longer runs an exclusive collection loop: it registers
a persistent collection demand on the coordination engine (spawning the
engine when none serves this data-dir) and refuses while a legacy
standalone watcher is still alive — reporting migration guidance, never
signaling the process. ``watch status`` reports engine state and the
CLI's own demand. ``watch stop`` releases exactly the CLI's demand (the
engine keeps serving other clients) and stops a legacy watcher only after
verifying the process identity — never a blind kill by PID. ``watch
once`` keeps the synchronous isolated round. Repeated collection,
refreshes and engine restarts never double count (idempotent imports).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import pytest

from orxanalytics import engine as eng
from orxanalytics import collect as collect_mod
from orxanalytics import query_model, registry
from orxanalytics import watch
from orxanalytics.importers import import_zcode as _import_zcode
from orxanalytics.store import Store

_seq = itertools.count()


# ----------------------------------------------------------------- helpers ---

class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def home():
    """A data dir whose path fits an AF_UNIX socket path."""
    root = Path("/tmp") / f"orx-watch-test-{os.getpid()}-{next(_seq)}"
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def register(store_dir: Path, *roots: Path) -> None:
    store = Store(store_dir)
    try:
        for root in roots:
            registry.add_project(store, str(root))
    finally:
        store.close()


def spawn_marker_process(*markers: str) -> subprocess.Popen:
    """A long-sleeping process whose ``ps`` command line carries the
    markers (argv after ``-c`` shows up in ``ps -o command=``)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", *markers],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _eventually(cond, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def engine_up(data_dir: Path, scheduler: bool = False) -> eng.Engine:
    """A real in-process engine serving the socket (scheduler off by
    default: pure coordination, no collection in tests)."""
    e = eng.Engine(data_dir, clock=FakeClock())
    e.acquire()
    e.start_server()
    if scheduler:
        import threading
        t = threading.Thread(target=e.run, daemon=True)
        t.start()
    return e


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# =========================================================== watch start ===

def test_start_refuses_while_legacy_watcher_alive(home):
    """旧 watcher 活体：start 只报告迁移指引，绝不发信号."""
    legacy = spawn_marker_process("orxanalytics.watch", "_loop")
    try:
        (home / "watch.pid").write_text(str(legacy.pid))
        out = watch.start(home)
        assert out["started"] is False
        assert "legacy" in out["reason"].lower()
        lw = out["legacy_watcher"]
        assert lw["detected"] is True and lw["pid"] == legacy.pid
        assert "watch stop" in lw["guidance"]
        # never signaled, nothing spawned
        assert pid_alive(legacy.pid)
        assert not watch._demand_pid_path(home).exists()
        assert not eng.socket_path(home).exists()
    finally:
        legacy.terminate()
        legacy.wait(timeout=10)


def test_start_registers_demand_with_running_engine(home):
    """start 向引擎注册持续采集需求，而非独占循环."""
    e = engine_up(home)
    try:
        out = watch.start(home)
        assert out["started"] is True, out
        assert out["engine_spawned"] is False      # engine was already up
        # the CLI's background demand raises the engine tier
        assert _eventually(lambda: e.tier()[0] == "active")
        st = watch.status(home)
        assert st["engine"]["running"] is True
        assert st["engine"]["compatible"] is True
        assert st["engine"]["protocol_version"] == query_model.PROTOCOL_VERSION
        assert st["demand"]["held"] is True
        assert pid_alive(st["demand"]["pid"])
        assert st["legacy_watcher"] is None
        assert _eventually(lambda: watch.status(home)["demand"]["renew_ok"]
                           is True)
    finally:
        watch.stop(home)
        e.shutdown()
    assert not watch._demand_pid_path(home).exists()


def test_start_spawns_engine_when_not_running(home):
    """引擎未运行时按迁移指引拉起，然后注册需求."""
    box: dict = {}

    def fake_spawn(data_dir: Path):
        box["dir"] = data_dir
        box["engine"] = engine_up(data_dir)

    try:
        out = watch.start(home, spawn_engine=fake_spawn)
        assert out["started"] is True, out
        assert out["engine_spawned"] is True
        assert box["dir"] == home
        st = watch.status(home)
        assert st["engine"]["running"] is True
        assert st["demand"]["held"] is True
        assert _eventually(lambda: box["engine"].tier()[0] == "active")
    finally:
        watch.stop(home)
        if "engine" in box:
            box["engine"].shutdown()


def test_engine_spawn_command_is_the_module_engine(home, monkeypatch):
    """拉起命令必须是分离的模块引擎进程."""
    recorded: dict = {}

    class FakeProc:
        pid = 424242

    def fake_popen(cmd, **kw):
        recorded["cmd"] = cmd
        recorded["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(watch.subprocess, "Popen", fake_popen)
    watch._spawn_engine_process(home)
    assert recorded["cmd"] == [sys.executable, "-m", "orxanalytics.engine",
                               "--data-dir", str(home)]
    assert recorded["kw"]["start_new_session"] is True      # detached
    assert recorded["kw"]["stdout"] is subprocess.DEVNULL


def test_ensure_engine_reports_honest_failure(home):
    out = watch.ensure_engine(home, timeout=0.4,
                              spawn_engine=lambda d: None)
    assert out["running"] is False and out["spawned"] is True
    assert "healthy" in out["reason"]


def test_start_is_idempotent_single_holder(home):
    e = engine_up(home)
    try:
        first = watch.start(home)
        assert first["started"] is True
        pid1 = watch._holder_pid(home)
        second = watch.start(home)
        assert second["started"] is False
        assert "already" in second["reason"].lower()
        assert watch._holder_pid(home) == pid1
        # engine clients see exactly one background holder connection
        facts = e.engine_facts()
        holders = facts["clients"]["demand"].get("background", [])
        assert len(holders) == 1
    finally:
        watch.stop(home)
        e.shutdown()


def test_start_never_takes_over_incompatible_engine(home, monkeypatch):
    """协议不兼容时报告版本问题，不强行接管."""
    eng.socket_path(home).touch()      # a live engine is listening there
    monkeypatch.setattr(
        watch, "_health_over_socket",
        lambda data_dir, timeout: {
            "ok": False,
            "error": {"code": "unsupported_protocol", "requested": 99,
                      "supported": query_model.PROTOCOL_VERSION}})
    spawned: list[Path] = []
    out = watch.start(home, spawn_engine=lambda d: spawned.append(d))
    assert out["started"] is False
    assert spawned == []        # live engine never replaced
    assert "protocol" in out["reason"].lower()
    assert not watch._demand_pid_path(home).exists()


# ========================================================== watch status ===

def test_status_cold_reports_engine_demand_and_legacy(home):
    st = watch.status(home)
    assert st["engine"]["running"] is False
    assert st["demand"]["held"] is False
    assert st["legacy_watcher"] is None
    # the CLI wiring prints the same report
    rc = watch.main(["status", "--data-dir", str(home)])
    assert rc == 0


def test_status_reports_legacy_watcher_with_guidance(home):
    legacy = spawn_marker_process("orxanalytics.watch", "_loop",
                                  "--interval", "600")
    try:
        (home / "watch.pid").write_text(str(legacy.pid))
        st = watch.status(home)
        lw = st["legacy_watcher"]
        assert lw is not None and lw["pid"] == legacy.pid
        assert "watch stop" in lw["guidance"]
    finally:
        legacy.terminate()
        legacy.wait(timeout=10)


def test_status_keeps_once_round_state(home):
    watch.run_one_round(home, 1, once=True)
    st = watch.status(home)
    assert st["rounds"] >= 1
    assert "engine" in st and "demand" in st and "legacy_watcher" in st


# =========================================================== watch stop ===

def test_stop_releases_own_demand_engine_and_app_unaffected(home):
    """App 与 CLI 共存：stop 只释放 CLI 自己的需求."""
    e = engine_up(home)
    app = eng.EngineClient(eng.socket_path(home))
    try:
        app.request({"op": "refresh.request", "reason": "panel-open"})
        assert watch.start(home)["started"] is True
        assert _eventually(lambda: e.tier()[0] == "ui")   # app strongest
        app.close()
        assert _eventually(lambda: e.tier()[0] == "active")  # holder remains

        out = watch.stop(home)
        assert out["stopped"] is True
        assert out["engine_stopped"] is False
        assert _eventually(lambda: e.tier()[0] == "idle")
        # the engine itself keeps serving other clients
        check = eng.EngineClient(eng.socket_path(home))
        try:
            assert check.request({"op": "health"})["ok"] is True
        finally:
            check.close()
    finally:
        watch.stop(home)
        app.close()
        e.shutdown()


def test_stop_legacy_user_confirmed_path(home):
    """用户确认后的停旧：stop 终止旧 watcher，引擎不动."""
    e = engine_up(home)
    legacy = spawn_marker_process("orxanalytics.watch", "_loop",
                                  "--data-dir", str(home))
    try:
        (home / "watch.pid").write_text(str(legacy.pid))
        out = watch.stop(home)
        assert out["stopped"] is True
        assert out["legacy"] is True and out["pid"] == legacy.pid
        assert "watch start" in out["next"]
        legacy.wait(timeout=10)                # actually terminated
        assert not (home / "watch.pid").exists()
        # the engine survived the migration stop
        assert _eventually(lambda: watch.status(home)["engine"]["running"])
    finally:
        if legacy.poll() is None:
            legacy.terminate()
            legacy.wait(timeout=10)
        e.shutdown()


def test_stop_refuses_unknown_process(home):
    """身份不明的活体 pid：拒绝发信号（不按 PID 盲杀）."""
    stranger = spawn_marker_process("definitely", "not-our-watcher")
    try:
        (home / "watch.pid").write_text(str(stranger.pid))
        (home / "watch-demand.pid").write_text(str(stranger.pid))
        out = watch.stop(home)
        assert out["stopped"] is False
        assert "refus" in out["reason"].lower()
        assert pid_alive(stranger.pid)          # never signaled
        assert (home / "watch.pid").exists()    # pidfile left for inspection
    finally:
        stranger.terminate()
        stranger.wait(timeout=10)
        (home / "watch.pid").unlink(missing_ok=True)
        (home / "watch-demand.pid").unlink(missing_ok=True)


def test_stop_cleans_stale_pidfiles(home):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    (home / "watch.pid").write_text(str(dead.pid))
    (home / "watch-demand.pid").write_text("999999999")
    out = watch.stop(home)
    assert out["stopped"] is False
    assert not (home / "watch.pid").exists()
    assert not (home / "watch-demand.pid").exists()


# ============================================================== holder ===

def test_holder_gives_up_when_engine_stays_lost(home):
    """引擎长期不可达：持有进程有限重连后退出."""
    rc = watch._run_holder(home, renew_sec=0.02, lost_window=0.3,
                           connect_timeout=0.2, install_signals=False)
    assert rc == 3
    state = json.loads((home / "watch-demand.state.json").read_text())
    assert state["engine_lost"] is True
    assert state["last_error"]
    assert not watch._demand_pid_path(home).exists()


def test_holder_renewal_paces_background_jobs_to_the_new_cadence():
    """G012：CLI 持有进程的续约节奏与新引擎节律一致（不放大采集频率）.

    Each holder renewal is one ``refresh.request`` that BOTH renews the
    CLI's background lease and submits one coalesced refresh job (which
    runs the state lane when claimed). The renewal cadence therefore must
    not outpace the engine's active tier, and it must renew well inside
    the (now longer) background lease so ordinary jitter never lets the
    demand lapse."""
    assert watch.HOLDER_RENEW_SEC >= eng.TIER_INTERVALS["active"], \
        "holder renewals must not outpace the engine's active tier"
    assert 2 * watch.HOLDER_RENEW_SEC \
        <= eng.DEMAND_LEASE_SECONDS["background"], \
        "one missed renewal must never let the background demand lapse"


# ============================ 幂等：采集/刷新/重启不重复计数 ===

def _zcode_totals(home: Path) -> tuple:
    store = Store(home)
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(input_tokens), 0) i, "
            "COALESCE(SUM(output_tokens), 0) o FROM zc_turns").fetchone()
        return (row["c"], row["i"], row["o"])
    finally:
        store.close()


def _all_snapshot_usage(home: Path, project_key: str) -> set[tuple]:
    """Usage facts of EVERY snapshot: each re-import of the same source
    data must produce an identically-shaped snapshot (snapshot rows are
    append-only — that is the audit trail, not double counting)."""
    store = Store(home)
    try:
        rows = store.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(input_tokens), 0) i, "
            "COALESCE(SUM(output_tokens), 0) o FROM orx_usage_obs "
            "GROUP BY snapshot_id").fetchall()
        return {(row["c"], row["i"], row["o"]) for row in rows}
    finally:
        store.close()


def _run_task_totals(engine: eng.Engine, project_key: str) -> dict:
    resp = engine.handle_request({"op": "runs.get", "project": project_key,
                                  "run_id": "R001"})
    assert resp["ok"] is True, resp
    return {(t["revision_id"], t["task_id"]):
            (t["consumption"]["observed_total"],
             t["consumption"]["tokens"]["input_tokens"],
             t["consumption"]["tokens"]["output_tokens"],
             t["attempt_count"])
            for t in resp["tasks"]}


@pytest.fixture()
def zcode_patched(world, monkeypatch):
    """Route the engine's usage lane at the synthetic zcode db (in-process
    engines see the same patch the conftest autouse fixture installs)."""
    monkeypatch.setattr(collect_mod, "import_zcode",
                        partial(_import_zcode, db=world["zcode_db"]))


def test_repeated_collection_refresh_and_once_never_double_count(
        home, world, zcode_patched):
    """重复采集、刷新不重复计数（幂等回归）."""
    proj = Path(world["proj_a"])
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        key = str(proj)
        snaps = _all_snapshot_usage(home, key)
        assert len(snaps) == 1              # exactly one snapshot so far
        base_usage = snaps.pop()
        base = (_zcode_totals(home), _run_task_totals(e, key))
        assert base[0][0] > 0                      # synthetic usage landed
        assert set(base[1]) == {(11, "T001"), (11, "T002"), (11, "T003"),
                                (12, "T001"), (12, "T003")}
        # five refresh requests coalesce into one bounded job
        for _ in range(5):
            r = e.handle_request({"op": "refresh.request",
                                  "reason": "manual"}, client_id="t")
            assert r["ok"] is True
        clock.advance(1)
        e.tick()
        # periodic full re-check lane (change detection must not add counts)
        e.request_history_audit()
        clock.advance(1)
        e.tick()
        clock.advance(e.FULL_RECHECK_INTERVAL + 120)
        e.tick()
        clock.advance(120)
        e.tick()
        assert (_zcode_totals(home), _run_task_totals(e, key)) == base
        # every re-import produced an identically-shaped snapshot
        assert _all_snapshot_usage(home, key) == {base_usage}
        # the CLI `once` round (a second, synchronous collector over the
        # same store) stays idempotent
        watch.run_one_round(home, 600, once=True)
        assert (_zcode_totals(home), _run_task_totals(e, key)) == base
        assert _all_snapshot_usage(home, key) == {base_usage}
    finally:
        e.release()


def test_engine_restart_never_double_counts(home, world, zcode_patched):
    """重启引擎（含再次手动刷新）不重复计数."""
    proj = Path(world["proj_a"])
    register(home, proj)
    key = str(proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock)
    e.acquire()
    try:
        e.tick()
        snaps = _all_snapshot_usage(home, key)
        assert len(snaps) == 1
        base_usage = snaps.pop()
        base = (_zcode_totals(home), _run_task_totals(e, key))
    finally:
        e.release()
    e2 = eng.Engine(home, clock=FakeClock())
    e2.acquire()
    try:
        e2.tick()
        for _ in range(3):
            assert e2.handle_request({"op": "refresh.request",
                                      "reason": "manual"},
                                     client_id="t")["ok"] is True
        clock2 = FakeClock(clock.now)
        clock2.advance(e2.FULL_RECHECK_INTERVAL + 60)
        e2.tick()
        e2.tick()
        assert (_zcode_totals(home), _run_task_totals(e2, key)) == base
        assert _all_snapshot_usage(home, key) == {base_usage}
    finally:
        e2.release()
