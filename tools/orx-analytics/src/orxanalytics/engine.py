"""Coordination engine: one collector per data-dir, a versioned local
JSON protocol, and tiered scheduling (design §7/§8).

The engine owns collection so the menu-bar app and the CLI never double
collect. Guarantees implemented here:

- **Single instance per data-dir** via a system file lock (``fcntl.flock``
  on ``<data_dir>/engine.lock``). A second engine cannot acquire the lock,
  cannot bind the socket and cannot run a single collection round. The
  kernel releases the lock when the holder dies, so no PID bookkeeping is
  required for correctness (a pidfile alone would not be).
- **User-private Unix domain socket** (``<data_dir>/engine.sock``, mode
  0600, newline-delimited JSON) serving T003's six query interfaces plus
  ``refresh.request``. Every request may carry ``protocol_version``; a
  mismatch is REPORTED (``unsupported_protocol`` with the engine's
  supported version) — the engine never force-takes-over or shuts down.
- **App/CLI coexistence**: every connected client holds its own
  collection demand (``refresh.request`` reasons ``panel-open`` /
  ``detail-open`` / ``background`` renew a per-connection lease). A client
  disconnect releases exactly its own demand, never the other side's.
- **Tiered scheduling**: UI open -> ~30s, background activity (demand or
  observed active runs) -> ~60s, idle -> ~300s (G012 用户反馈 4: the
  original 5/15/60s tiers refreshed far too often for real usage); the
  history audit runs at a low fixed cadence or manually. State refresh,
  usage import and anomaly analysis are separate lanes with separate
  cadences.
- **Coalesced, bounded refresh**: all pending ``refresh.request`` calls
  merge into ONE pending job (union of projects); each source appears at
  most once, so no source can queue unboundedly.
- **Change detection + periodic full re-check**: a WAL-aware stat
  signature of the project's ``state.db`` skips unchanged imports, but a
  full re-check re-imports periodically — file times alone never decide
  "forever unchanged".
- **Failure isolation**: one project's import failure is recorded (with
  the failure reason and the last successful data time) and never
  touches other projects; the query layer keeps serving that project's
  last successful snapshot.
- **Sleep/wake**: a wall-clock gap far beyond the scheduled interval is
  treated as system sleep — the engine re-syncs immediately and skips
  the anomaly lane for that round (no stall verdicts across sleep).
- **Legacy watcher**: a live ``<data_dir>/watch.pid`` (the old standalone
  ``watch start`` process) is detected and reported with migration
  guidance. The engine NEVER signals that PID.
- **Crash handling**: :class:`EngineSupervisor` restarts a crashing
  engine a bounded number of times with exponential backoff, then gives
  up and writes diagnostics to ``<data_dir>/engine.state.json``.

All source databases are read through the existing read-only importers
(observability contract); the engine never writes an ORX source.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import anomalies, collect as _collect, correlate, display, findings
from . import probe as _probe
from . import discovery, query_model, registry
from .query_model import PROTOCOL_VERSION, REFRESH_OP
from .paths import analytics_data_directory
from .store import Store

# ----------------------------------------------------------------- tuning ---

TIER_INTERVALS: dict[str, float] = {"ui": 30.0, "active": 60.0,
                                    "idle": 300.0}
USAGE_MULTIPLIER = 4          # usage import cadence = 4x the state tier
ANOMALY_MULTIPLIER = 2        # anomaly analysis cadence = 2x the state tier
HISTORY_AUDIT_INTERVAL = 1800.0   # low-frequency full reconciliation
DISCOVERY_INTERVAL = 300.0       # bounded index/registry/scan reconciliation
FULL_RECHECK_INTERVAL = 600.0     # max age of a change-detected "unchanged"
WAKE_SLACK = 30.0             # gap beyond the tier interval => treat as sleep
# Leases are sized against the clients' renewal cadence (the app's panel
# loop renews per refresh round, the CLI holder per HOLDER_RENEW_SEC):
# at least two full intervals of headroom, so one missed renewal never
# drops the tier, while a client that truly goes away releases quickly.
DEMAND_LEASE_SECONDS = {"ui": 180.0, "background": 360.0}
MAX_RESTARTS = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

LANES = ("state", "usage", "anomalies", "history_audit")
UI_REASONS = {"panel-open", "detail-open"}
BACKGROUND_REASONS = {"background"}
FORCE_REASONS = {"manual", "history-audit"}   # bypass change detection

_LEGACY_GUIDANCE = (
    "A legacy standalone watcher is running (pid {pid}). Stop it with "
    "`orx-analytics watch stop`, then let the engine own collection. "
    "The engine never sends a signal to that process.")


class EngineAlreadyRunning(RuntimeError):
    """Another engine holds the data-dir's single-instance lock."""


class EngineNotRunning(RuntimeError):
    """The engine was asked to work before acquiring its instance lock."""


def socket_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "engine.sock"


def lock_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "engine.lock"


def state_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "engine.state.json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # exists, owned by someone else


def detect_legacy_watcher(data_dir: Path | str) -> dict | None:
    """Facts about a live legacy standalone watcher, or None.

    The old ``watch start`` loop leaves ``<data_dir>/watch.pid``. A LIVE
    pid there means the legacy collector is still running: report it with
    migration guidance. This function never signals the process — killing
    by PID is exactly what the design forbids.
    """
    pidfile = Path(data_dir) / "watch.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    return {"detected": True, "pid": pid, "pidfile": str(pidfile),
            "guidance": _LEGACY_GUIDANCE.format(pid=pid)}


# --------------------------------------------------------- single instance ---

class SingleInstanceLock:
    """Kernel-held exclusive lock, one engine per data-dir.

    ``fcntl.flock`` locks the open file description: the lock disappears
    when the owning process dies, whatever the on-disk file says. The
    file carries pid/started_at only as diagnostics for error messages.
    """

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.path = lock_path(self.data_dir)
        self._fd: int | None = None

    def holder(self) -> dict | None:
        try:
            info = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) else None

    def acquire(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            info = self.holder()
            os.close(fd)
            detail = f"another engine holds {self.path}"
            if info and info.get("pid"):
                detail += f" (pid {info['pid']})"
            raise EngineAlreadyRunning(detail) from None
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(
            {"pid": os.getpid(), "started_at": _iso(time.time())}).encode())
        try:
            os.fsync(fd)
        except OSError:
            pass

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------- client demand ---

class DemandTracker:
    """Per-connection collection demand with expiring leases.

    Each App/CLI connection holds its own demand; a disconnect releases
    only that connection's leases, so one side exiting never stops the
    other side's collection.
    """

    def __init__(self, leases: dict[str, float] | None = None):
        self._leases = leases or dict(DEMAND_LEASE_SECONDS)
        self._by_client: dict[str, dict[str, float]] = {}

    def hold(self, client_id: str, kind: str, now: float) -> None:
        leases = self._by_client.setdefault(client_id, {})
        leases[kind] = now + self._leases[kind]

    def release_client(self, client_id: str) -> None:
        self._by_client.pop(client_id, None)

    def kinds_active(self, now: float) -> set[str]:
        active: set[str] = set()
        for cid in list(self._by_client):
            leases = {k: exp for k, exp in self._by_client[cid].items()
                      if exp > now}
            if leases:
                self._by_client[cid] = leases
                active |= set(leases)
            else:
                del self._by_client[cid]
        return active

    def holders(self, now: float) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for cid, leases in self._by_client.items():
            for kind, exp in leases.items():
                if exp > now:
                    out.setdefault(kind, []).append(cid)
        return out


# ------------------------------------------------------------ refresh jobs ---

class RefreshQueue:
    """Coalesced refresh jobs: ONE pending slot, union-merged.

    Every request merges into the single pending job (a null project set
    absorbs everything), so both the job count and the per-source queue
    are bounded by construction. Jobs finish into a small ring history.
    """

    def __init__(self, max_history: int = 32):
        self._seq = 0
        self._max_history = max_history
        self._pending: dict | None = None
        self._running: dict | None = None
        self._history: list[dict] = []
        self._mu = threading.Lock()

    def submit(self, projects: list[str] | None, reason: str | None,
               client_id: str, rejected: list[dict],
               now: float) -> dict[str, Any]:
        with self._mu:
            merged_with = None
            if projects is not None and not projects:
                # every explicit project was rejected: no job is created
                job_id = None
            elif self._pending is None:
                self._seq += 1
                job_id = f"job-{self._seq}"
                self._pending = {
                    "id": job_id,
                    "projects": None if projects is None else set(projects),
                    "reasons": {reason} if reason else set(),
                    "requested_by": {client_id},
                    "created_at": _iso(now),
                    "state": "pending",
                }
            else:
                job_id = merged_with = self._pending["id"]
                if projects is None:
                    self._pending["projects"] = None
                elif self._pending["projects"] is not None:
                    self._pending["projects"] |= set(projects)
                if reason:
                    self._pending["reasons"].add(reason)
                self._pending["requested_by"].add(client_id)
            pending = self._pending
            return {
                "job_id": job_id,
                "merged_with": merged_with,
                "projects": (None if pending is None or pending["projects"] is None
                             else sorted(pending["projects"])),
                "rejected": rejected,
            }

    def claim(self, now: float) -> dict | None:
        with self._mu:
            if self._pending is None:
                return None
            job = self._pending
            job["state"] = "running"
            job["claimed_at"] = _iso(now)
            self._running = job
            self._pending = None
            return dict(job)

    def finish(self, result: dict, now: float) -> None:
        with self._mu:
            job = self._running
            self._running = None
            if job is None:
                return
            job["state"] = "done"
            job["finished_at"] = _iso(now)
            job["result"] = result
            self._history.append(job)
            del self._history[:-self._max_history]

    def pending(self) -> dict | None:
        with self._mu:
            if self._pending is None:
                return None
            job = dict(self._pending)
            if job["projects"] is not None:
                job["projects"] = sorted(job["projects"])
            job["reasons"] = sorted(job["reasons"])
            job["requested_by"] = sorted(job["requested_by"])
            return job

    def pending_count(self) -> int:
        with self._mu:
            return 1 if self._pending is not None else 0

    def recent(self, limit: int = 8) -> list[dict]:
        with self._mu:
            return [dict(j) for j in self._history[-limit:]]


# ------------------------------------------------------------------ engine ---

class Engine:
    """One collector per data-dir: lock, socket protocol, scheduler."""

    FULL_RECHECK_INTERVAL = FULL_RECHECK_INTERVAL
    WAKE_SLACK = WAKE_SLACK

    def __init__(self, data_dir: Path | str, *,
                 clock: Callable[[], float] | None = None,
                 intervals: dict[str, float] | None = None,
                 index_file: Path | str | None = None,
                 scan_roots: list[Path | str] | None = None):
        self.data_dir = Path(data_dir)
        self._clock = clock or time.time
        self._intervals = dict(TIER_INTERVALS)
        if intervals:
            self._intervals.update(intervals)
        self._lock = SingleInstanceLock(self.data_dir)
        self.demand = DemandTracker()
        self.jobs = RefreshQueue()
        self._mu = threading.RLock()
        self._acquired = False
        self._started_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_discovery_at: float | None = None
        self._discovery_status: dict = {}
        self._index_file = index_file
        self._scan_roots = (scan_roots if scan_roots is not None else [
            p for p in os.environ.get("ORX_ANALYTICS_SCAN_ROOTS", "").split(os.pathsep) if p])
        self._last_wake: str | None = None
        self._active_runs = 0
        self._lanes = {name: {"last_run_at": None, "runs": 0, "armed": False}
                       for name in LANES}
        self._pstatus: dict[str, dict] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._conn_seq = 0
        self._legacy = None

    # ------------------------------------------------- single instance ---

    def acquire(self) -> "Engine":
        if self._acquired:
            return self
        self._lock.acquire()          # raises EngineAlreadyRunning
        self._acquired = True
        self._started_at = self._clock()
        # a live legacy watcher is recorded and REPORTED; never killed
        self._legacy = detect_legacy_watcher(self.data_dir)
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        self._lock.release()
        self._acquired = False

    # ------------------------------------------------------------ socket ---

    def start_server(self) -> None:
        """Bind the user-private socket and serve connections (threads)."""
        if not self._acquired:
            raise EngineNotRunning("acquire() before start_server()")
        path = socket_path(self.data_dir)
        if path.exists():
            path.unlink()             # stale by construction: we hold the lock
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)         # user-private (design §7)
        srv.listen(8)
        self._listener = srv
        t = threading.Thread(target=self._accept_loop, daemon=True,
                             name="orx-engine-accept")
        t.start()
        self._threads.append(t)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break                 # listener closed by shutdown()
            with self._mu:
                self._conn_seq += 1
                client_id = f"conn-{self._conn_seq}"
            t = threading.Thread(target=self._handle_connection,
                                 args=(conn, client_id), daemon=True,
                                 name=f"orx-engine-{client_id}")
            t.start()
            self._threads.append(t)

    def _handle_connection(self, conn: socket.socket,
                           client_id: str) -> None:
        try:
            f = conn.makefile("rwb")
            for line in f:
                try:
                    payload = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    resp = self._error(None, "invalid_request",
                                       "request line is not valid JSON")
                else:
                    resp = self.handle_request(payload, client_id)
                f.write(json.dumps(resp, default=str).encode() + b"\n")
                f.flush()
        except OSError:
            pass
        finally:
            # this client's demand dies with its connection — and ONLY
            # this client's (App/CLI coexistence)
            self.demand.release_client(client_id)
            try:
                conn.close()
            except OSError:
                pass

    def start(self, *, server: bool = True, scheduler: bool = True) -> None:
        """Full service: lock + socket + scheduler thread."""
        self.acquire()
        if server:
            self.start_server()
        if scheduler:
            t = threading.Thread(target=self.run, daemon=True,
                                 name="orx-engine-scheduler")
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        for t in self._threads:
            t.join(timeout=5.0)
        self._threads.clear()
        path = socket_path(self.data_dir)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        self.release()

    # ---------------------------------------------------------- protocol ---

    @staticmethod
    def _error(op: Any, code: str, message: str,
               **extra: Any) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        err.update(extra)
        return {"op": op if isinstance(op, str) else None, "ok": False,
                "protocol_version": PROTOCOL_VERSION, "error": err}

    def handle_request(self, payload: Any,
                       client_id: str = "local") -> dict[str, Any]:
        """One protocol request -> one response dict (socket or in-proc)."""
        if not isinstance(payload, dict):
            return self._error(None, "invalid_request",
                               "request must be a JSON object")
        op = payload.get("op")
        version = payload.get("protocol_version", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            # report clearly; never force-take-over or shut down
            return self._error(
                op, "unsupported_protocol",
                f"protocol version {version!r} is not supported by this "
                f"engine (supported: {PROTOCOL_VERSION}); the request is "
                f"refused, not taken over",
                requested=version, supported=PROTOCOL_VERSION)
        if not isinstance(op, str):
            return self._error(None, "invalid_request", "op is required")
        if op == REFRESH_OP:
            try:
                norm = query_model.validate_refresh_request(payload)
            except ValueError as e:
                msg = str(e)
                code = ("unsupported_protocol"
                        if msg.startswith("unsupported_protocol")
                        else "invalid_request")
                return self._error(op, code, msg)
            return self._refresh(norm, client_id)
        if op not in query_model.OPS:
            return self._error(op, "unknown_op",
                               f"op {op!r} is not served by this engine",
                               supported=list(query_model.OPS) + [REFRESH_OP])
        try:
            resp = self._dispatch_query(op, payload)
        except (ValueError, KeyError, TypeError) as e:
            return self._error(op, "invalid_request", str(e))
        resp["ok"] = True
        return resp

    _RUNS_LIST_PARAMS = ("project", "text", "run_id", "task_id", "status",
                         "model", "since", "until", "sort", "order")

    def _dispatch_query(self, op: str, payload: dict) -> dict:
        store = Store(self.data_dir)
        try:
            if op == "overview":
                return query_model.overview(store)
            if op == "runs.list":
                kw = {k: payload[k] for k in self._RUNS_LIST_PARAMS
                      if k in payload}
                for k in ("limit", "offset"):
                    if k in payload:
                        kw[k] = int(payload[k])
                return query_model.runs_list(store, **kw)
            if op == "runs.get":
                return query_model.runs_get(store, payload["project"],
                                            payload["run_id"])
            if op == "tasks.get":
                return query_model.tasks_get(
                    store, payload["project"], payload["run_id"],
                    int(payload["revision_id"]), payload["task_id"])
            if op == "projects.list":
                kw = {}
                for k in ("limit", "offset"):
                    if k in payload:
                        kw[k] = int(payload[k])
                return query_model.projects_list(store, **kw)
            if op == "health":
                resp = query_model.health(store)
                resp["engine"] = self.engine_facts()
                return resp
            raise ValueError(f"unhandled op {op!r}")   # pragma: no cover
        finally:
            store.close()

    def _refresh(self, norm: dict, client_id: str) -> dict:
        now = self._clock()
        reason = norm.get("reason")
        if reason in UI_REASONS:
            self.demand.hold(client_id, "ui", now)
        elif reason in BACKGROUND_REASONS:
            self.demand.hold(client_id, "background", now)
        if reason == "history-audit":
            self.request_history_audit()
        projects, rejected = self._refresh_targets(norm.get("projects"))
        accepted = self.jobs.submit(projects, reason, client_id, rejected,
                                    now)
        self._wake.set()             # scheduler: run the job soon
        return {"op": "refresh.accepted", "ok": True,
                "protocol_version": PROTOCOL_VERSION, **accepted}

    def _refresh_targets(self, projects: list[str] | None
                         ) -> tuple[list[str] | None, list[dict]]:
        """Validate an explicit project list: registered, readable, gated."""
        if projects is None:
            return None, []
        store = Store(self.data_dir)
        try:
            registered = {p["project_key"]
                          for p in registry.list_projects(store)}
        finally:
            store.close()
        ok: list[str] = []
        rejected: list[dict] = []
        for key in projects:
            if key not in registered:
                rejected.append({"project": key, "reason": "not registered"})
                continue
            db = Path(key) / ".orx" / "state.db"
            if not db.exists():
                rejected.append({"project": key,
                                 "reason": "no snapshot source"})
                continue
            pr = _probe.probe_orx(Path(key))
            if pr.ok:
                ok.append(key)
                continue
            gaps = " ".join(pr.gaps or [])
            if "schema gate" in gaps:
                reason = "unsupported schema"
            else:
                reason = "unreadable"
            rejected.append({"project": key, "reason": reason})
        return ok, rejected

    # --------------------------------------------------------- scheduling ---

    def tier(self, now: float | None = None) -> tuple[str, float]:
        """(tier, interval): ui 30s < active 60s < idle 300s (design §8,
        cadence lowered by G012 用户反馈 4)."""
        now = self._clock() if now is None else now
        kinds = self.demand.kinds_active(now)
        if "ui" in kinds:
            return "ui", self._intervals["ui"]
        if "background" in kinds or self._active_runs > 0:
            return "active", self._intervals["active"]
        return "idle", self._intervals["idle"]

    def hold_demand(self, client_id: str, kind: str) -> None:
        self.demand.hold(client_id, kind, self._clock())

    def request_history_audit(self) -> None:
        """Arm a manual full reconciliation on the next tick."""
        with self._mu:
            self._lanes["history_audit"]["armed"] = True
        self._wake.set()

    def _lane_interval(self, name: str, now: float) -> float:
        if name == "history_audit":
            return HISTORY_AUDIT_INTERVAL
        _, interval = self.tier(now)
        if name == "state":
            return interval
        if name == "usage":
            return max(20.0, interval * USAGE_MULTIPLIER)
        return interval * ANOMALY_MULTIPLIER

    def _lane_due(self, name: str, now: float) -> bool:
        lane = self._lanes[name]
        if lane["armed"]:
            return True
        if name == "history_audit" and lane["last_run_at"] is None:
            return now >= (self._started_at or now) + HISTORY_AUDIT_INTERVAL
        if lane["last_run_at"] is None:
            return True
        return now >= lane["last_run_at"] + self._lane_interval(name, now)

    def lane_next_due(self, name: str, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        lane = self._lanes[name]
        if lane["armed"]:
            return now
        if lane["last_run_at"] is None:
            base = self._started_at if self._started_at is not None else now
            if name == "history_audit":
                return base + HISTORY_AUDIT_INTERVAL
            return now
        return lane["last_run_at"] + self._lane_interval(name, now)

    def tick(self, now: float | None = None) -> dict:
        """One scheduler round. Returns a report; never raises for a
        single project's failure (isolation is per project and per lane)."""
        if not self._acquired:
            raise EngineNotRunning("acquire() before tick()")
        now = self._clock() if now is None else now
        with self._mu:
            tier_now, interval = self.tier(now)
            wake = (self._last_tick_at is not None
                    and now - self._last_tick_at > interval + self.WAKE_SLACK)
            due = {name: self._lane_due(name, now) for name in LANES}
            # one bounded pending slot: claiming it here can never stack
            # per-source work (each source appears at most once)
            job = self.jobs.claim(now)
        report: dict[str, Any] = {
            "tick_at": _iso(now), "tier": tier_now, "interval": interval,
            "wake": wake, "lanes": {}, "job": job.get("id") if job else None}
        lanes_report = report["lanes"]
        store: Store | None = None
        active_runs: int | None = None
        try:
            discovered = False
            if (self._last_discovery_at is None
                    or now - self._last_discovery_at >= DISCOVERY_INTERVAL):
                store = store or Store(self.data_dir)
                try:
                    result = discovery.discover(
                        store, index_file=self._index_file,
                        scan_roots=self._scan_roots)
                    candidates = [p["project"] for p in result["projects"]
                                  if not p["registered"] and p["state_db_present"]]
                    registered = discovery.register_discovered(store, candidates)
                    discovered = any(p["action"] == "registered" for p in registered)
                    self._discovery_status = {
                        "checked_at": _iso(now), "index": result["index"],
                        "registered": registered, "notices": result["notices"],
                        "scan": result["scan"]}
                except Exception as exc:  # isolate discovery; expose failure in health
                    self._discovery_status = {"checked_at": _iso(now),
                                              "error": f"{type(exc).__name__}: {exc}"}
                self._last_discovery_at = now
                report["discovery"] = self._discovery_status
            job_force = bool(job and job["reasons"] & FORCE_REASONS)
            # a forced job (manual / history-audit) bypasses change
            # detection for ITS projects only; everything else stays
            # gated so a refresh never becomes a full-history sweep
            force_all = bool(job and job_force and job["projects"] is None)
            force_keys = (set(job["projects"])
                          if job and job_force and job["projects"] is not None
                          else None)
            if wake or due["state"] or job is not None or discovered:
                store = store or Store(self.data_dir)
                force = wake or force_all
                lanes_report["state"] = self._run_state_lane(
                    store, now, force=force, force_keys=force_keys)
                lanes_report["state"]["forced"] = force or bool(force_keys)
                active_runs = self._count_active_runs(store)
            if wake or due["usage"]:
                store = store or Store(self.data_dir)
                lanes_report["usage"] = self._run_usage_lane(store, now)
            if due["anomalies"] or job is not None:
                if wake:
                    # no stall verdicts across sleep: skip exactly this
                    # round, resync first, verdicts resume on the next one
                    lanes_report["anomalies"] = {
                        "runs": self._lanes["anomalies"]["runs"],
                        "ran": False, "skipped": "wake-resync"}
                else:
                    store = store or Store(self.data_dir)
                    lanes_report["anomalies"] = self._run_anomaly_lane(
                        store, now)
            if due["history_audit"]:
                store = store or Store(self.data_dir)
                lanes_report["history_audit"] = self._run_history_audit(
                    store, now)
                active_runs = self._count_active_runs(store)
            if job is not None:
                state_report = lanes_report.get("state") or {}
                self.jobs.finish({
                    "projects": state_report.get("projects", {}),
                    "wake": wake}, now)
        finally:
            if store is not None:
                store.close()
        with self._mu:
            self._last_tick_at = now
            if wake:
                self._last_wake = _iso(now)
            if active_runs is not None:
                self._active_runs = active_runs
            for name, lane_report in lanes_report.items():
                if lane_report.get("ran"):
                    self._lanes[name]["last_run_at"] = now
                    self._lanes[name]["runs"] += 1
                    self._lanes[name]["armed"] = False
        # uniform lane view: every lane reports its counters even when idle
        for name in LANES:
            lane = lanes_report.setdefault(
                name, {"runs": self._lanes[name]["runs"], "ran": False})
            lane.setdefault("skipped", None)
            lane.setdefault("runs", self._lanes[name]["runs"])
        return report

    def _run_state_lane(self, store: Store, now: float, *,
                        force: bool = False,
                        force_keys: set[str] | None = None) -> dict:
        """Import ORX snapshots for due projects.

        Change detection: a WAL-aware stat signature skips unchanged
        projects, but only until FULL_RECHECK_INTERVAL old — then a full
        re-check imports regardless (file times never decide "forever
        unchanged"). One project's failure is recorded and never stops
        the others; its last successful data stays servable.
        """
        out: dict[str, dict] = {}
        for p in registry.list_projects(store):
            key = p["project_key"]
            if not p["orx_enabled"]:
                continue
            with self._mu:
                st = self._pstatus.setdefault(key, {
                    "last_success_at": None, "last_error": None,
                    "error_at": None, "signature": None,
                    "last_full_check_at": None, "checks": 0, "imports": 0})
                signature = st["signature"]
                last_full = st["last_full_check_at"]
            sig = self._source_signature(key)
            proj_force = force or (force_keys is not None
                                   and key in force_keys)
            if (not proj_force and sig == signature and last_full is not None
                    and now - last_full < self.FULL_RECHECK_INTERVAL):
                with self._mu:
                    st["checks"] += 1
                out[key] = {"action": "unchanged"}
                continue
            try:
                res = _collect.import_orx_project(store, key)
            except Exception as e:  # noqa: BLE001 - isolate per project
                self._record_failure(key, f"{type(e).__name__}: {e}", now)
                out[key] = {"action": "error",
                            "error": f"{type(e).__name__}: {e}"}
                continue
            if "error" in res:
                self._record_failure(key, str(res["error"]), now)
                out[key] = {"action": "error", "error": str(res["error"])}
                continue
            action = ("imported" if sig != signature or signature is None
                      else "full-recheck")
            with self._mu:
                st["signature"] = sig
                st["last_full_check_at"] = now
                st["last_success_at"] = _iso(now)
                st["last_error"] = None
                st["error_at"] = None
                st["imports"] += 1
            out[key] = {"action": action,
                        "snapshot_id": res.get("snapshot_id")}
        return {"ran": True, "projects": out}

    def _record_failure(self, key: str, error: str, now: float) -> None:
        """Keep the failure reason AND the old data time; never wipe the
        last successful snapshot identity."""
        with self._mu:
            st = self._pstatus.setdefault(key, {
                "last_success_at": None, "last_error": None,
                "error_at": None, "signature": None,
                "last_full_check_at": None, "checks": 0, "imports": 0})
            st["last_error"] = error
            st["error_at"] = _iso(now)

    def _run_usage_lane(self, store: Store, now: float) -> dict:
        """Global usage import on its own slower cadence. Called through
        the collect module namespace so test isolation patches apply."""
        results = []
        for name in ("import_zcode", "import_codex", "import_cursor"):
            fn = getattr(_collect, name)
            try:
                res = fn(store)
            except Exception as e:  # noqa: BLE001 - lane must survive
                res = {"error": f"{type(e).__name__}: {e}"}
            results.append({"source": name, "ok": "error" not in res,
                            "error": res.get("error")})
        return {"ran": True, "sources": results}

    def _run_anomaly_lane(self, store: Store, now: float) -> dict:
        """Correlation + anomaly rules + finding upserts (analysis is
        scheduled separately from state and usage). The engine NEVER pops
        notifications — presentation is the app's job (design §4)."""
        keys = [p["project_key"] for p in registry.list_projects(store)
                if p["orx_enabled"]]
        for pk in keys:
            try:
                correlate.correlate(store, pk)
            except Exception as e:  # noqa: BLE001 - isolate per project
                self._record_failure(pk, f"correlate: {e}", now)
        new_findings = 0
        try:
            results = anomalies.run_all(store, keys)
            for r in results:
                row = findings.upsert_finding(store, **r.upsert_args())
                if not row.get("merged"):
                    new_findings += 1
        except Exception as e:  # noqa: BLE001 - lane must survive
            return {"ran": True, "projects": keys, "new_findings": 0,
                    "error": f"{type(e).__name__}: {e}", "skipped": None}
        return {"ran": True, "projects": keys, "new_findings": new_findings}

    def _run_history_audit(self, store: Store, now: float) -> dict:
        """Low-frequency full reconciliation: force a full import of every
        project, ignoring change detection (design §8 完整核对)."""
        return self._run_state_lane(store, now, force=True)

    def _source_signature(self, project_key: str) -> tuple:
        """WAL-aware stat signature: state.db plus its -wal file.
        Committed changes can live in the WAL while the main file's mtime
        stands still, so the WAL is part of the signature. The shm file contains reader
        coordination state: even read-only connections may change its mtime."""
        root = Path(project_key) / ".orx"
        parts = []
        for name in sorted(("state.db", "state.db-wal")):
            p = root / name
            try:
                st = p.stat()
            except OSError:
                continue
            parts.append((name, st.st_mtime_ns, st.st_size))
        return tuple(parts)

    def _count_active_runs(self, store: Store) -> int:
        """Non-terminal runs across every project's latest SUCCESSFUL
        snapshot, classified by the SAME lifecycle matrix as the overview
        (display.lifecycle_view): a run under a KNOWN cancelled Goal is
        history and does not hold the active cadence, while paused and
        cancelling Goals keep their in-flight runs counting — the raw run
        status itself is never rewritten (unknown statuses are never
        terminal — not a verdict)."""
        n = 0
        for sid in query_model._successful_snapshot_ids(store).values():
            goals = {r["id"]: r["status"] for r in store.conn.execute(
                "SELECT id, status FROM orx_goals WHERE snapshot_id=?",
                (sid,))}
            for r in store.conn.execute(
                    "SELECT goal_id, status FROM orx_runs WHERE snapshot_id=?",
                    (sid,)):
                goal_st = (display.goal_status(goals.get(r["goal_id"]))
                           if r["goal_id"] else None)
                view = display.lifecycle_view(
                    goal_st, display.run_status(r["status"]))
                if view["bucket"] == "active":
                    n += 1
        return n

    def run(self) -> dict:
        """Serve the scheduler until shutdown(); the supervisor restarts
        whatever escapes here as a crash. Wakes at most 1s late so a
        ``refresh.request`` (which sets the wake event) is picked up
        quickly without a busy loop."""
        while not self._stop.is_set():
            self.tick()
            now = self._clock()
            next_due = min(self.lane_next_due(name, now) for name in LANES)
            timeout = min(max(0.05, next_due - now), 1.0)
            self._stop.wait(timeout)
            if self._wake.is_set():
                self._wake.clear()   # a refresh job is waiting: tick now
        return {"stopped": True, "ticks": sum(
            l["runs"] for l in self._lanes.values())}

    # -------------------------------------------------------------- facts ---

    def project_status(self) -> dict[str, dict]:
        with self._mu:
            return {k: {"last_success_at": v["last_success_at"],
                        "last_error": v["last_error"],
                        "error_at": v["error_at"]}
                    for k, v in self._pstatus.items()}

    def last_wake(self) -> str | None:
        return self._last_wake

    def engine_facts(self) -> dict:
        """Live engine facts for the health interface (the pure layer
        honestly reports implemented=False; the engine overrides)."""
        now = self._clock()
        tier_now, interval = self.tier(now)
        supervisor = None
        diag = state_path(self.data_dir)
        if diag.exists():
            try:
                supervisor = json.loads(diag.read_text())
            except (OSError, ValueError):
                supervisor = None
        return {
            "implemented": True,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "started_at": (_iso(self._started_at)
                           if self._started_at is not None else None),
            "data_dir": str(self.data_dir),
            "socket_path": str(socket_path(self.data_dir)),
            "lock_path": str(lock_path(self.data_dir)),
            "tier": {"current": tier_now, "interval_sec": interval,
                     "active_runs": self._active_runs},
            "clients": {"demand": self.demand.holders(now)},
            "refresh": {"pending": self.jobs.pending(),
                        "recent_jobs": self.jobs.recent()},
            "lanes": {name: {"last_run_at": (
                                _iso(l["last_run_at"])
                                if l["last_run_at"] is not None else None),
                            "runs": l["runs"]}
                      for name, l in self._lanes.items()},
            "legacy_watcher": self._legacy or detect_legacy_watcher(
                self.data_dir),
            "last_wake": self._last_wake,
            "discovery": dict(self._discovery_status),
            "projects": self.project_status(),
            "supervisor": supervisor,
        }


# -------------------------------------------------------------- supervisor ---

class EngineSupervisor:
    """Bounded crash restarts with exponential backoff (design §8).

    Restarts the engine up to ``max_restarts`` times, sleeping
    ``min(BACKOFF_CAP, BACKOFF_BASE * 2**(n-1))`` between attempts. When
    the budget is exhausted it gives up, returns the diagnostics report
    and writes it to ``<data_dir>/engine.state.json`` (the diagnostics
    entry the app surfaces), never retrying forever.
    """

    def __init__(self, engine_factory: Callable[[], Any], *,
                 sleep: Callable[[float], None] = time.sleep,
                 max_restarts: int = MAX_RESTARTS,
                 state_dir: Path | str | None = None):
        self._factory = engine_factory
        self._sleep = sleep
        self._max_restarts = max_restarts
        self._state_dir = Path(state_dir) if state_dir else None

    @staticmethod
    def backoff(n: int) -> float:
        return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (n - 1)))

    def run(self) -> dict:
        restarts = 0
        last_error: str | None = None
        while True:
            engine = self._factory()
            try:
                result = engine.run()
            except Exception as e:  # noqa: BLE001 - crash path by design
                last_error = f"{type(e).__name__}: {e}"
                if restarts >= self._max_restarts:
                    report = {"gave_up": True, "restarts": restarts,
                              "last_error": last_error, "last_result": None}
                    self._write_diagnostics(report)
                    return report
                restarts += 1
                self._sleep(self.backoff(restarts))
                continue
            return {"gave_up": False, "restarts": restarts,
                    "last_error": last_error, "last_result": result}

    def _write_diagnostics(self, report: dict) -> None:
        if self._state_dir is None:
            return
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            state_path(self._state_dir).write_text(
                json.dumps(report, indent=1) + "\n")
        except OSError:
            pass


# ----------------------------------------------------------------- client ---

class EngineClient:
    """Minimal newline-JSON client for the engine socket (used by the
    tests, the CLI watch integration and handy for debugging)."""

    def __init__(self, path: Path | str, timeout: float = 5.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(path))
        self._file = self._sock.makefile("rwb")

    def request(self, payload: dict) -> dict:
        self._file.write(json.dumps(payload).encode() + b"\n")
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("engine closed the connection")
        return json.loads(line)

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()


def main(argv: list[str] | None = None) -> int:
    """Run the supervised engine as a standalone process (``python -m
    orxanalytics.engine [--data-dir DIR]``) — the entry the app and
    ``watch start`` launch. Full service: lock + socket server + the
    supervised scheduler loop (regression: this entry used to acquire and
    run the scheduler without ever binding the socket)."""
    import argparse
    ap = argparse.ArgumentParser(prog="orx-analytics-engine")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)
    data_dir = analytics_data_directory(args.data_dir)

    def make_engine() -> Engine:
        engine = Engine(data_dir)
        engine.acquire()
        engine.start_server()   # the socket must be live before run()
        return engine

    sup = EngineSupervisor(make_engine,
                           state_dir=data_dir)
    report = sup.run()
    print(json.dumps(report, indent=1, default=str))
    return 1 if report["gave_up"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
