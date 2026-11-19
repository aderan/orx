"""Project discovery (design §6): merge three sources, never guess identity.

Sources — all read-only on the ORX side:

1. the ORX user-level project index, consumed exactly per the contract in
   docs/orx-integration.md (ORX_DATA_DIR > XDG_DATA_HOME/orx >
   ~/.local/share/orx, ``index_version`` gate, lock file ignored);
2. the analytics registry (manual ``projects add`` lives there);
3. a bounded scan of user-chosen working roots — depth- and budget-limited,
   dependency directories and ``.git`` pruned, directory symlinks never
   followed, so it can neither loop nor turn into a full-disk walk.

Merge identity is the canonical path (``Path.expanduser().resolve()``) —
the same key the registry uses. Two directories with the same NAME are
never merged (design §2); a moved project is re-associated only through
the explicit :func:`relocate_project`, which re-keys OUR store and touches
no ORX data. A missing index (legacy ORX) degrades with the explicit
notice 自动发现范围受限; a corrupt or future-versioned index is isolated as
its own source status instead of failing discovery. Deleted or unreadable
projects keep their history: discovery lists them with 来源不可读 and
deletes nothing. Data health reuses the frozen display contract
(:func:`orxanalytics.display.data_health`) and the schema gate
``probe.SUPPORTED_ORX_SCHEMA``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import display, query_model, registry
from .probe import SUPPORTED_ORX_SCHEMA

SUPPORTED_INDEX_VERSION = 1
INDEX_NAME = "projects-index.json"

#: bounded-scan defaults: at most 4 directory levels below each root and
#: at most 5000 visited directories per scan call — never a full-disk walk.
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_DIRS = 5000

#: directory names never descended into: dependency/output trees and VCS
#: internals (``.git`` is pruned whole, so its internals are never read).
SCAN_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", ".tox", ".nox", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
    "target", "build", "dist", "Pods", "DerivedData", ".bundle",
    ".terraform", ".next", ".cache", "vendor",
})

_MISSING_INDEX_NOTICE = (
    "自动发现范围受限：未找到 ORX 用户级项目索引（旧版 ORX 不提供）；"
    "仅已登记项目与手动扫描来源可用")


class DiscoveryError(Exception):
    pass


def _now_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ index read ---

def orx_index_path(orx_data_dir: Path | str | None = None) -> Path:
    """Index location per the consumption contract: ORX_DATA_DIR >
    XDG_DATA_HOME/orx > ~/.local/share/orx. ``orx_data_dir`` overrides the
    whole resolution (tests, explicit callers)."""
    if orx_data_dir is not None:
        return Path(orx_data_dir) / INDEX_NAME
    env = os.environ.get("ORX_DATA_DIR")
    if env:
        return Path(env) / INDEX_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "orx" / INDEX_NAME
    return Path.home() / ".local" / "share" / "orx" / INDEX_NAME


def read_index(path: Path | str | None = None) -> dict:
    """Read-only consumption of the ORX projects index (never writes,
    never locks, ignores the writer's lock file).

    status:
      ``ok``          — merged normally;
      ``missing``     — legacy ORX without an index: discovery degrades
                        with the 自动发现范围受限 notice, never an error;
      ``unreadable``  — absent/unreadable/corrupt file: source isolated;
      ``unsupported`` — a future ``index_version``: source isolated.

    Keys are re-normalized with ``Path(...).expanduser().resolve()`` per
    the contract (ORX already resolves, consumers align). Malformed
    individual entries are skipped and reported, never fatal.
    """
    p = Path(orx_index_path() if path is None else path).expanduser()
    base = {"path": str(p), "index_version": None, "updated_at": None,
            "projects": {}}
    if not p.exists():
        return {**base, "status": "missing",
                "detail": "no projects-index.json (legacy ORX provides "
                          "no index)"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {**base, "status": "unreadable",
                "detail": f"cannot read index: {e}"}
    if not isinstance(data, dict):
        return {**base, "status": "unreadable",
                "detail": "index is not a JSON object"}
    version = data.get("index_version")
    if version != SUPPORTED_INDEX_VERSION:
        return {**base, "status": "unsupported", "index_version": version,
                "detail": f"index_version {version!r} not in supported "
                          f"{{{SUPPORTED_INDEX_VERSION}}}; index source "
                          f"isolated"}
    raw = data.get("projects")
    if not isinstance(raw, dict):
        return {**base, "status": "unreadable", "index_version": version,
                "detail": "index projects is not an object"}
    projects: dict[str, dict] = {}
    skipped: list[str] = []
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            skipped.append(str(key))
            continue
        canon = display.project_key(key)
        has_db = entry.get("has_state_db")
        shaped = {
            "canonical_path": canon,
            "name": entry.get("name") or Path(canon).name,
            "first_seen_at": entry.get("first_seen_at"),
            "last_seen_at": entry.get("last_seen_at"),
            "last_run_id": entry.get("last_run_id"),   # hint, never a key
            "has_state_db": has_db if isinstance(has_db, bool) else None,
        }
        prev = projects.get(canon)
        # two raw keys normalizing to one canonical path ARE one project;
        # keep the facts with the later last_seen_at
        if prev is None or (shaped["last_seen_at"] or "") >= (
                prev["last_seen_at"] or ""):
            projects[canon] = shaped
    out = {**base, "status": "ok", "index_version": version,
           "updated_at": data.get("updated_at"),
           "projects": projects,
           "detail": f"{len(projects)} project(s)"}
    if skipped:
        out["skipped_entries"] = skipped
        out["detail"] += f"; skipped {len(skipped)} malformed entr(y/ies)"
    return out


# --------------------------------------------------------- bounded scan ---

def scan_for_projects(roots, *, max_depth: int = DEFAULT_MAX_DEPTH,
                      max_dirs: int = DEFAULT_MAX_DIRS) -> dict:
    """Bounded scan of user-chosen roots for ORX projects (a directory
    containing ``.orx/state.db``).

    Bounds and safety: directory symlinks are NEVER followed (symlink
    loops cannot happen and the walk stays inside the given roots), names
    in :data:`SCAN_SKIP_DIRS` are pruned whole, depth is capped at
    ``max_depth`` levels below each (resolved) root, and the walk stops
    after ``max_dirs`` visited directories, reporting ``truncated``. No
    root means no scan; this function never walks anything else.
    """
    found: set[str] = set()
    pruned: dict[str, int] = {}
    missing_roots: list[str] = []
    unreadable = 0
    visited = 0
    truncated = False
    resolved_roots: list[str] = []

    def on_error(_e):
        nonlocal unreadable
        unreadable += 1

    for root in roots or []:
        base = Path(root).expanduser().resolve()     # loop-safe real path
        if not base.is_dir():
            missing_roots.append(str(Path(root).expanduser()))
            continue
        resolved_roots.append(str(base))

    for base in resolved_roots:
        if truncated:
            break
        for dirpath, dirnames, _files in os.walk(base, followlinks=False,
                                                 onerror=on_error):
            here = Path(dirpath)
            visited += 1
            if (here / ".orx" / "state.db").exists():
                found.add(str(here))
            if visited >= max_dirs:
                truncated = True
                break
            depth = len(here.relative_to(base).parts)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            keep = []
            for name in dirnames:
                if name in SCAN_SKIP_DIRS:
                    pruned[name] = pruned.get(name, 0) + 1
                    continue
                keep.append(name)
            dirnames[:] = keep
    return {
        "roots": resolved_roots,
        "missing_roots": missing_roots,
        "projects": sorted(found),
        "scanned_dirs": visited,
        "truncated": truncated,
        "unreadable_dirs": unreadable,
        "pruned_dirs": pruned,
        "max_depth": max_depth,
        "max_dirs": max_dirs,
    }


# ---------------------------------------------------------------- merge ---

def _index_source_status(idx: dict, notices: list[str]) -> None:
    status = idx["status"]
    if status == "missing":
        notices.append(_MISSING_INDEX_NOTICE)
    elif status == "unreadable":
        notices.append(f"ORX 项目索引不可读，索引来源已隔离：{idx['detail']}")
    elif status == "unsupported":
        notices.append(
            f"ORX 项目索引版本不支持（index_version={idx['index_version']!r}），"
            f"索引来源已隔离")


def discover(store, *, index_file: Path | str | None = None,
             scan_roots=None, max_depth: int = DEFAULT_MAX_DEPTH,
             max_dirs: int = DEFAULT_MAX_DIRS, now: datetime | None = None,
             stale_after: float | None
             = query_model.DEFAULT_STALE_AFTER_SECONDS) -> dict:
    """Merge the three discovery sources into one canonical-path keyed
    view. Read-only apart from nothing: this function writes neither the
    ORX index nor the registry (registration is the explicit
    :func:`register_discovered` / ``projects add`` step)."""
    now = now if now is not None else datetime.now(timezone.utc)
    warnings: list[dict] = []
    notices: list[str] = []

    idx = read_index(index_file)
    _index_source_status(idx, notices)

    scan_res = (scan_for_projects(scan_roots, max_depth=max_depth,
                                 max_dirs=max_dirs)
                if scan_roots else None)

    merged: dict[str, dict] = {}
    registered = {p["project_key"]: p
                  for p in registry.list_projects(store)}
    for key, row in registered.items():
        merged[key] = {"sources": ["registry"], "registry": row}
    if idx["status"] == "ok":
        for key, entry in idx["projects"].items():
            if key in merged:
                merged[key]["sources"].append("index")
                merged[key]["index"] = entry
            else:
                merged[key] = {"sources": ["index"], "index": entry}
    if scan_res:
        for key in scan_res["projects"]:
            if key in merged:
                merged[key]["sources"].append("scan")
            else:
                merged[key] = {"sources": ["scan"]}
            merged[key]["found_by_scan"] = True

    keys = sorted(merged)
    snap_map = query_model._read_snapshot_map(store, keys, warnings)
    projects_out = []
    for key in keys:
        m = merged[key]
        reg = m.get("registry")
        entry = m.get("index")
        snap = snap_map[key]
        runs_known = 0
        if snap is not None:
            runs_known = store.conn.execute(
                "SELECT COUNT(*) c FROM orx_runs WHERE snapshot_id=?",
                (snap["snapshot_id"],)).fetchone()["c"]
        projects_out.append({
            "project": key,
            "name": ((reg or {}).get("name")
                     or (entry or {}).get("name")
                     or Path(key).name),
            "sources": m["sources"],
            "registered": reg is not None,
            "registered_at": reg["registered_at"] if reg else None,
            "orx_enabled": reg["orx_enabled"] if reg else None,
            "index": entry,
            "found_by_scan": m.get("found_by_scan", False),
            "state_db_present": (Path(key) / ".orx" / "state.db").exists(),
            "has_history": snap is not None,
            "runs_known": runs_known,
            "last_snapshot": (None if snap is None else {
                "snapshot_id": snap["snapshot_id"],
                "taken_at": snap["taken_at"],
                "schema_version": snap["schema_version"],
                "supported": snap["schema_version"]
                in SUPPORTED_ORX_SCHEMA}),
            "data_health": query_model._project_health(
                key, snap, now, stale_after, warnings),
        })
    new_projects = [p["project"] for p in projects_out
                    if not p["registered"]]
    return {
        "generated_at": _now_iso(now),
        "index": {"status": idx["status"], "path": idx["path"],
                  "index_version": idx["index_version"],
                  "updated_at": idx["updated_at"],
                  "detail": idx["detail"]},
        "scan": scan_res,
        "notices": notices,
        "warnings": warnings,
        "projects": projects_out,
        "counts": {
            "merged": len(projects_out),
            "registered": len(registered),
            "from_index": (len(idx["projects"])
                           if idx["status"] == "ok" else 0),
            "from_scan": (len(scan_res["projects"]) if scan_res else 0),
            "new_candidates": len(new_projects),
        },
        "new_projects": new_projects,
    }


# ------------------------------------------------- explicit registration ---

def register_discovered(store, keys) -> list[dict]:
    """Explicit add of discovery candidates into the registry (design §6
    first-use paths 1+2). Idempotent; never removes or rewrites existing
    registrations. Uses registry.add_project, which keeps orx_enabled
    honest about a missing state.db."""
    registered = {p["project_key"] for p in registry.list_projects(store)}
    results = []
    for key in keys:
        if key in registered:
            results.append({"project": key,
                            "action": "already-registered"})
            continue
        try:
            row = registry.add_project(store, key)
        except registry.RegistryError as e:
            results.append({"project": key, "action": "error",
                            "error": str(e)})
            continue
        registered.add(key)
        results.append({"project": key, "action": "registered",
                        "orx_enabled": row["orx_enabled"]})
    return results


# ---------------------------------------------------- explicit relocation ---

def relocate_project(store, old_key: str, new_root: str | Path) -> dict:
    """Explicit 重新定位项目 association (design §2/§6): re-key one
    project's identity from ``old_key`` to the canonical path of
    ``new_root``. The caller names BOTH keys — identity is never guessed
    from a directory name. Every historical reference in OUR store moves
    in one transaction; ORX source data is untouched. Refuses when the
    target is already registered (no implicit merge of two identities)."""
    old = display.project_key(old_key)
    new = display.project_key(new_root)
    if old == new:
        return {"relocated": False, "from": old, "to": new,
                "reason": "same canonical path"}
    if not Path(new).is_dir():
        raise DiscoveryError(f"relocate target {new} is not a directory")
    registered = {p["project_key"] for p in registry.list_projects(store)}
    own_history = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_snapshots WHERE project_key=?",
        (old,)).fetchone()["c"]
    if old not in registered and own_history == 0:
        raise DiscoveryError(
            f"{old} is neither registered nor has history; nothing to "
            f"relocate")
    if new in registered:
        raise DiscoveryError(
            f"{new} is already registered; refusing the implicit merge of "
            f"two project identities — remove one explicitly first")
    c = store.conn
    moved = {}
    try:
        for table in ("projects", "orx_snapshots", "links", "findings"):
            cur = c.execute(
                f"UPDATE {table} SET project_key=? WHERE project_key=?",
                (new, old))
            moved[table] = cur.rowcount
        for table in ("import_state", "import_runs"):
            cur = c.execute(
                f"UPDATE {table} SET source_id=? WHERE source_id=?",
                (f"orx:{new}", f"orx:{old}"))
            moved[table] = cur.rowcount
        c.commit()
    except Exception:
        c.rollback()
        raise
    return {"relocated": True, "from": old, "to": new, "moved_rows": moved}
