"""Watch: register collection demand on the coordination engine.

Redesigned for the menu-bar design §7 (2026-10-16): ``watch start`` no
longer runs an exclusive collection loop. It (1) refuses while a legacy
standalone watcher is still alive, reporting migration guidance
(docs/watch-migration.md) and never signaling that process; (2) ensures
the data-dir's engine is serving, spawning the module engine process
when none is (an incompatible live engine is reported, never taken
over); and (3) spawns a detached demand holder that keeps one socket
connection and renews the CLI's own "background" lease. The engine owns
collection cadence (30/60/300s tiers, lowered from 5/15/60s by the G012
user-feedback cadence fix, coalesced refresh jobs).

- ``watch stop`` releases exactly the CLI's demand — the engine keeps
  serving the app's demand (App/CLI coexistence, exiting one side never
  stops the other). It stops a live legacy watcher only after verifying
  the process identity from ``ps``; an unknown or recycled PID is
  refused, never blindly signaled.
- ``watch status`` reports engine state, the CLI's own demand, and any
  detected legacy watcher.
- ``watch once`` keeps the old synchronous one-round path (the isolated
  test path): incremental imports + correlation + anomaly rules +
  finding upserts + one notification for new findings. Imports are
  idempotent upserts keyed by natural primary keys, so repeated rounds,
  refreshes and restarts never double count.

The legacy pidfile ``<data_dir>/watch.pid`` is written only by the OLD
loop and read only for detection/migration; this module never writes it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .store import Store, now_iso

DEFAULT_INTERVAL_SEC = 600   # legacy compat: `watch once --interval`

# holder / spawn tuning. Each renewal is one refresh.request that BOTH
# renews the background lease (360s in the engine) and submits one
# coalesced background job, so the cadence is pinned to the engine's
# active tier (60s): it must neither outpace the tier nor let the lease
# lapse (tests assert both invariants).
HOLDER_RENEW_SEC = 60.0
HOLDER_CONNECT_TIMEOUT = 5.0
HOLDER_RECONNECT_WINDOW = 300.0    # engine unreachable this long => give up
HOLDER_STOP_WAIT = 5.0
ENGINE_START_TIMEOUT = 15.0

# a process is "ours" only if its full command line carries the module
# name and the subcommand marker (see docs/watch-migration.md)
_LEGACY_MARKERS = ("orxanalytics.watch", "_loop")
_HOLDER_MARKERS = ("orxanalytics.watch", "_hold")


def _state_path(data_dir: Path) -> Path:
    return data_dir / "watch.state.json"


def _pid_path(data_dir: Path) -> Path:
    """The LEGACY loop's pidfile (written by old versions, never by us)."""
    return data_dir / "watch.pid"


def _demand_pid_path(data_dir: Path) -> Path:
    return data_dir / "watch-demand.pid"


def _demand_state_path(data_dir: Path) -> Path:
    return data_dir / "watch-demand.state.json"


def _read_state(data_dir: Path) -> dict[str, Any] | None:
    p = _state_path(data_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"corrupt": True}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_state(data_dir: Path, state: dict[str, Any]) -> None:
    p = _state_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(p)


# ---------------------------------------------------- process identity ---

def process_command(pid: int) -> str | None:
    """Full command line of ``pid`` via ps, or None when unknown.

    ``-ww`` avoids width truncation so markers late in a long command
    line (``--data-dir`` paths) still match. Unknown must mean "refuse
    to signal", never "assume it is ours"."""
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    line = out.stdout.strip()
    return line or None


def _identity_matches(pid: int, *markers: str) -> bool:
    cmd = process_command(pid)
    return cmd is not None and all(m in cmd for m in markers)


def _reap_if_child(pid: int) -> None:
    """Reap a terminated child so a zombie never masquerades as alive
    (only ever succeeds for our own children; others raise)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        _reap_if_child(pid)
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# ------------------------------------------------------ engine presence ---

def _health_over_socket(data_dir: Path, timeout: float) -> dict:
    """One health request over the engine socket; raises OSError/ValueError
    when no engine answers (separated out so tests can stub the wire)."""
    from .engine import EngineClient, socket_path
    client = EngineClient(socket_path(data_dir), timeout=timeout)
    try:
        return client.request({"op": "health"})
    finally:
        client.close()


def engine_status(data_dir: Path, timeout: float = 2.0) -> dict:
    """Engine facts for this data-dir: running, compatible, essentials."""
    from .engine import socket_path
    sock = socket_path(data_dir)
    if not sock.exists():
        return {"running": False, "compatible": False,
                "reason": "engine socket not present",
                "socket_path": str(sock)}
    try:
        resp = _health_over_socket(data_dir, timeout)
    except (OSError, ValueError) as e:
        return {"running": False, "compatible": False,
                "reason": f"engine not answering on {sock}: "
                          f"{type(e).__name__}: {e}",
                "socket_path": str(sock)}
    facts = resp.get("engine") or {}
    out: dict[str, Any] = {"running": True, "socket_path": str(sock)}
    if resp.get("ok"):
        out.update({"compatible": True,
                    "pid": facts.get("pid"),
                    "protocol_version": facts.get("protocol_version"),
                    "tier": facts.get("tier"),
                    "lanes": facts.get("lanes"),
                    "clients": facts.get("clients"),
                    "data_dir": facts.get("data_dir")})
    else:
        # a live engine that refuses our protocol is reported, never
        # taken over (design §7: 协议不兼容时报告版本问题)
        out.update({"compatible": False, "error": resp.get("error")})
    return out


def _spawn_engine_process(data_dir: Path):
    """Detach the module engine for this data-dir (the same entry the app
    launches). The engine's single-instance file lock arbitrates any
    race; our spawned loser exits, the winner serves the socket."""
    cmd = [sys.executable, "-m", "orxanalytics.engine",
           "--data-dir", str(data_dir)]
    return subprocess.Popen(
        cmd, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_engine(data_dir: Path, *, timeout: float = ENGINE_START_TIMEOUT,
                  spawn_engine: Callable[[Path], Any] | None = None) -> dict:
    """An engine serving this data-dir: use the running one, else spawn.

    A running-but-incompatible engine is returned as-is (never replaced,
    never taken over)."""
    st = engine_status(data_dir)
    if st["running"]:
        return {**st, "spawned": False}
    spawn = spawn_engine or _spawn_engine_process
    spawn(data_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = engine_status(data_dir)
        if st["running"]:
            return {**st, "spawned": True}
        time.sleep(0.1)
    return {"running": False, "compatible": False, "spawned": True,
            "reason": f"engine did not become healthy within {timeout}s "
                      f"on {socket_path_str(data_dir)}"}


def socket_path_str(data_dir: Path) -> str:
    from .engine import socket_path
    return str(socket_path(data_dir))


# --------------------------------------------------------------- start ---

def _holder_pid(data_dir: Path) -> int | None:
    p = _demand_pid_path(data_dir)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _demand_status(data_dir: Path) -> dict:
    pid = _holder_pid(data_dir)
    held = pid is not None and _pid_alive(pid)
    out: dict[str, Any] = {"held": bool(held),
                           "pid": pid if held else None}
    p = _demand_state_path(data_dir)
    if p.exists():
        try:
            state = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            state = {"corrupt": True}
        if isinstance(state, dict):
            out.update(state)
    return out


def start(data_dir: Path, *, spawn_engine: Callable[[Path], Any] | None = None,
          engine_timeout: float = ENGINE_START_TIMEOUT) -> dict:
    """Register a persistent CLI collection demand on the engine.

    Refuses (with migration guidance, never a signal) while a legacy
    standalone watcher is alive; spawns the engine when none serves this
    data-dir; then detaches the demand holder that renews the CLI's own
    background lease. The engine — not this CLI — owns collection."""
    from .engine import detect_legacy_watcher
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy = detect_legacy_watcher(data_dir)
    if legacy is not None:
        return {"started": False,
                "reason": "a legacy standalone watcher is still running; "
                          "migrate first (docs/watch-migration.md): "
                          + legacy["guidance"],
                "legacy_watcher": legacy}
    holder = _holder_pid(data_dir)
    if holder and _pid_alive(holder):
        return {"started": False,
                "reason": f"CLI collection demand already held "
                          f"(pid {holder})",
                "engine": engine_status(data_dir),
                "demand": _demand_status(data_dir)}
    eng = ensure_engine(data_dir, timeout=engine_timeout,
                        spawn_engine=spawn_engine)
    if not (eng.get("running") and eng.get("compatible")):
        return {"started": False,
                "reason": eng.get("reason")
                          or "engine is running but protocol-incompatible; "
                             "refusing to take it over",
                "engine": eng}
    cmd = [sys.executable, "-m", "orxanalytics.watch", "_hold",
           "--data-dir", str(data_dir)]
    subprocess.Popen(cmd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if _demand_pid_path(data_dir).exists():
            break
        time.sleep(0.1)
    return {"started": True, "engine": eng,
            "engine_spawned": bool(eng.get("spawned")),
            "demand": _demand_status(data_dir),
            "legacy_watcher": None}


# ---------------------------------------------------------------- stop ---

def stop(data_dir: Path) -> dict:
    """Stop the CLI's own demand holder — or, explicitly, a live legacy
    watcher (the user-confirmed migration step).

    The engine is NEVER stopped here: it keeps serving other clients
    (the app's demand survives the CLI's exit and vice versa). A signal
    is sent only to a pid from our own pidfiles AND only after the ps
    command line matches the watcher signature; anything else is refused
    with manual guidance (no blind kill by PID)."""
    from .engine import detect_legacy_watcher
    eng = engine_status(data_dir)
    legacy = detect_legacy_watcher(data_dir)
    if legacy is not None:
        pid = legacy["pid"]
        if not _identity_matches(pid, *_LEGACY_MARKERS):
            return {"stopped": False, "pid": pid, "legacy": True,
                    "reason": f"pid {pid} is alive but its command line "
                              "does not match the orx-analytics watcher "
                              "signature (expected 'orxanalytics.watch "
                              "_loop'); refusing to signal an unknown "
                              "process — inspect `ps -ww -p "
                              f"{pid} -o command=` and stop it manually "
                              "once you confirm what it is",
                    "legacy_watcher": legacy, "engine": eng}
        os.kill(pid, signal.SIGTERM)
        dead = _wait_dead(pid, HOLDER_STOP_WAIT)
        if dead:
            _pid_path(data_dir).unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "legacy": True,
                    "reason": "legacy standalone watcher stopped "
                              "(user-confirmed migration step)",
                    "next": "run `orx-analytics watch start` to register "
                            "collection with the engine",
                    "engine": engine_status(data_dir)}
        return {"stopped": False, "pid": pid, "legacy": True,
                "reason": "legacy watcher did not exit after SIGTERM; "
                          "inspect it manually",
                "legacy_watcher": legacy, "engine": eng}
    pidfile = _demand_pid_path(data_dir)
    if not pidfile.exists():
        # no legacy is alive (detected above), so any leftover watch.pid
        # is stale from a dead old loop: clear it so future detection is
        # not polluted
        _pid_path(data_dir).unlink(missing_ok=True)
        return {"stopped": False, "reason": "not running", "engine": eng}
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pidfile.unlink(missing_ok=True)
        _pid_path(data_dir).unlink(missing_ok=True)
        return {"stopped": False, "reason": "corrupt pidfile removed",
                "engine": eng}
    if not _pid_alive(pid):
        pidfile.unlink(missing_ok=True)
        _pid_path(data_dir).unlink(missing_ok=True)
        _reap_if_child(pid)
        return {"stopped": False,
                "reason": f"pid {pid} was already dead "
                          "(stale pidfile removed)",
                "engine": eng}
    if not _identity_matches(pid, *_HOLDER_MARKERS):
        return {"stopped": False, "pid": pid,
                "reason": f"pid {pid} is alive but does not look like the "
                          "demand holder (expected 'orxanalytics.watch "
                          "_hold'); refusing to signal an unknown process "
                          "— inspect `ps -ww -p "
                          f"{pid} -o command=` manually",
                "engine": eng}
    os.kill(pid, signal.SIGTERM)
    dead = _wait_dead(pid, HOLDER_STOP_WAIT)
    if dead:
        pidfile.unlink(missing_ok=True)
        return {"stopped": True, "pid": pid, "engine_stopped": False,
                "reason": "CLI collection demand released; the engine "
                          "keeps serving other clients",
                "engine": engine_status(data_dir)}
    return {"stopped": False, "pid": pid,
            "reason": "demand holder did not exit after SIGTERM; inspect "
                      "it manually",
            "engine": engine_status(data_dir)}


# -------------------------------------------------------------- status ---

def status(data_dir: Path) -> dict:
    """Engine state, the CLI's own demand, legacy watcher, and the last
    `once`-round state (compat keys: rounds / last_round_at / ...)."""
    from .engine import detect_legacy_watcher
    state = _read_state(data_dir) or {}
    return {"engine": engine_status(data_dir),
            "demand": _demand_status(data_dir),
            "legacy_watcher": detect_legacy_watcher(data_dir),
            **state}


# ------------------------------------------------------- notifications ---


def _notify(rows: list[dict]) -> int:
    """One mac notification for newly inserted findings.

    The loop runs detached with no terminal, so a finding that is only
    recorded changes nothing until someone thinks to look (To2050
    2026-10-06: a stall finding would have sat unread for hours). Rows
    whose payload self-describes as confidence "low" (no_progress:
    gap may be think-time) stay silent; the rest notify once —
    fingerprint dedupe upstream keeps rounds from repeating themselves.
    Best-effort: any failure returns 0 and the round continues.
    Kill switch: ORX_ANALYTICS_NOTIFY=0.
    """
    if os.environ.get("ORX_ANALYTICS_NOTIFY") == "0" or not rows:
        return 0
    notable: list[dict] = []
    for row in rows:
        try:
            conf = json.loads(row["payload_json"]).get("confidence")
        except (json.JSONDecodeError, TypeError, KeyError):
            conf = None
        if conf != "low":
            notable.append(row)
    if not notable:
        return 0
    lines = [f'{r["rule"]}: {r["title"]}' for r in notable[:5]]
    if len(notable) > 5:
        lines.append(f'… +{len(notable) - 5} more')
    msg = "\n".join(lines)[:400].replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title '
             f'"orx-analytics watch" subtitle '
             f'"{len(notable)} new finding(s)"'],
            timeout=10, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0
    return len(notable)


# ------------------------------------------------------- the once round ---

def run_one_round(data_dir: Path, interval: int,
                  once: bool = False) -> None:
    """The synchronous `once` round body (isolated test path). Each round
    is bounded: incremental imports + rule evaluation only; nothing sends
    history anywhere. Idempotent upserts mean repeated rounds never
    double count. This path never claims the legacy ``watch.pid``."""
    from . import anomalies, correlate, findings
    from .collect import collect
    from .registry import list_projects

    state = _read_state(data_dir) or {
        "started_at": now_iso(), "rounds": 0, "last_round_at": None,
        "last_error": None, "findings_upserted": 0}
    while True:
        round_started = now_iso()
        error: str | None = None
        new_findings: list[dict] = []
        try:
            store = Store(data_dir)
            try:
                res = collect(store)
                errors = [r for r in res["global"] + res["orx"]
                          if "error" in r]
                if errors:
                    error = json.dumps(errors, ensure_ascii=False)[:400]
                keys = [p["project_key"] for p in list_projects(store)
                        if p["orx_enabled"]]
                for pk in keys:
                    correlate.correlate(store, pk)
                results = anomalies.run_all(store, keys)
                for r in results:
                    row = findings.upsert_finding(store, **r.upsert_args())
                    if not row.get("merged"):
                        new_findings.append(row)
                state["findings_upserted"] = \
                    len(findings.list_findings(store))
            finally:
                store.close()
        except Exception as e:  # noqa: BLE001 - round must survive
            error = f"{type(e).__name__}: {e}"
        notified = _notify(new_findings)
        state["rounds"] = state.get("rounds", 0) + 1
        state["last_round_at"] = round_started
        state["last_error"] = error
        state["notified_last_round"] = notified
        state["pid"] = os.getpid()
        _write_state(data_dir, state)
        if once:
            return
        time.sleep(interval)


# ---------------------------------------------------------- demand holder ---

def _run_holder(data_dir: Path, *,
                renew_sec: float = HOLDER_RENEW_SEC,
                lost_window: float = HOLDER_RECONNECT_WINDOW,
                connect_timeout: float = HOLDER_CONNECT_TIMEOUT,
                install_signals: bool = True) -> int:
    """The detached demand holder body (``watch _hold``).

    Keeps ONE connection to the engine and renews the CLI's background
    lease well before it expires; the engine releases exactly this
    demand when the connection dies. Exits 0 on SIGTERM/SIGINT, 3 when
    the engine stays unreachable (or protocol-incompatible) past
    ``lost_window`` — a bounded give-up, never an infinite spin."""
    from .engine import EngineClient, socket_path
    data_dir.mkdir(parents=True, exist_ok=True)
    pidfile = _demand_pid_path(data_dir)
    pidfile.write_text(str(os.getpid()))
    state: dict[str, Any] = {"pid": os.getpid(), "started_at": now_iso(),
                             "last_renew_at": None, "renew_ok": False,
                             "last_error": None}

    def _save() -> None:
        p = _demand_state_path(data_dir)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1) + "\n")
        tmp.replace(p)

    stop_flag = {"flag": False}

    def _on_term(signum, frame) -> None:
        stop_flag["flag"] = True

    previous: dict = {}
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, _on_term)
            except (OSError, ValueError):
                pass
    client = None
    lost_since: float | None = None
    try:
        _save()
        while not stop_flag["flag"]:
            try:
                if client is None:
                    client = EngineClient(socket_path(data_dir),
                                          timeout=connect_timeout)
                resp = client.request({"op": "refresh.request",
                                       "reason": "background"})
                ok = bool(resp.get("ok"))
                state["renew_ok"] = ok
                state["last_renew_at"] = now_iso()
                state["last_error"] = (None if ok else json.dumps(
                    resp.get("error"), default=str)[:300])
                if ok:
                    lost_since = None
                elif lost_since is None:
                    lost_since = time.monotonic()
            except (OSError, ValueError) as e:
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass
                    client = None
                state["renew_ok"] = False
                state["last_error"] = f"{type(e).__name__}: {e}"
                if lost_since is None:
                    lost_since = time.monotonic()
            _save()
            if lost_since is not None and \
                    time.monotonic() - lost_since >= lost_window:
                state["engine_lost"] = True
                state["gave_up_at"] = now_iso()
                return 3
            end = time.monotonic() + renew_sec
            while not stop_flag["flag"] and time.monotonic() < end:
                time.sleep(min(0.25, max(0.01, end - time.monotonic())))
        state["stopped"] = True
        state["stopped_at"] = now_iso()
        return 0
    finally:
        _save()
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        pidfile.unlink(missing_ok=True)
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


# ------------------------------------------------------------------ main ---

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="orx-analytics-watch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "stop", "status", "once", "_hold"):
        p = sub.add_parser(name)
        p.add_argument("--data-dir", default=None)
        p.add_argument("--interval", type=int,
                       default=DEFAULT_INTERVAL_SEC,
                       help="legacy: only the once round's sleep; the "
                            "engine owns collection cadence")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    home = data_dir or Store().data_dir
    if args.cmd == "start":
        out = start(home)
    elif args.cmd == "stop":
        out = stop(home)
    elif args.cmd == "status":
        out = status(home)
    elif args.cmd == "once":
        run_one_round(home, args.interval, once=True)
        out = {"round": "done", "state": status(home)}
    else:            # _hold: the detached demand holder entry point
        return _run_holder(home)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
