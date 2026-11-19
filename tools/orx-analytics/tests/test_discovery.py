"""Discovery layer (design §6, G002 T005).

Three read-only sources merged by canonical path — the ORX user-level
project index (consumed per docs/orx-integration.md), the analytics
registry, and a bounded scan of user-chosen roots. Identity is never
guessed: same directory NAME never merges two paths, a moved project is
re-associated only through the explicit relocate operation, and a missing
index (legacy ORX) degrades with the 自动发现范围受限 notice instead of
failing. Deleted/unreadable projects and unsupported schemas surface as
data health states with history kept; the scan is depth- and
budget-bounded, prunes dependency dirs and .git, and never follows
directory symlinks, so it cannot loop or wander into a full-disk walk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_orx_db
from orxanalytics import cli, discovery, display, query_model, registry
from orxanalytics.importers.orxstate import import_orx_project

UTC = timezone.utc
FIXED_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


def canon(p) -> str:
    return display.project_key(p)


def make_min_orx_db(path: Path, version: str = "9") -> None:
    """Smallest state.db the read side can honestly probe: meta only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
    conn.commit()
    conn.close()


def write_index(path: Path, projects: dict, version: int = 1) -> Path:
    """A well-formed ORX projects-index.json (T004 writer shape)."""
    entries = {}
    for key, extra in projects.items():
        k = canon(key)
        entries[k] = {
            "canonical_path": k,
            "name": extra.get("name", Path(k).name),
            "first_seen_at": extra.get("first_seen_at",
                                       "2026-10-15T00:00:00+00:00"),
            "last_seen_at": extra.get("last_seen_at",
                                      "2026-10-16T00:00:00+00:00"),
            "last_run_id": extra.get("last_run_id"),
            "has_state_db": extra.get("has_state_db", True),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"index_version": version, "updated_at": "2026-10-16T00:00:00+00:00",
         "projects": entries}, ensure_ascii=False, indent=1) + "\n")
    return path


def _orx_register(index_path: Path, root: Path, run_id: str | None) -> None:
    """The ORX-side registration algorithm (T004): lock beside the index,
    load, update, atomic replace. Replicated here so the consumer is tested
    against the real write behavior without importing the ORX package."""
    import fcntl

    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock = index_path.with_suffix(".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if index_path.exists():
            data = json.loads(index_path.read_text())
        else:
            data = {"index_version": 1, "projects": {}}
        key = str(root.resolve())
        prev = data["projects"].get(key, {})
        data["projects"][key] = {
            "canonical_path": key,
            "name": root.name,
            "first_seen_at": prev.get("first_seen_at",
                                      "2026-10-16T00:00:00+00:00"),
            "last_seen_at": "2026-10-16T00:00:01+00:00",
            "last_run_id": run_id,
            "has_state_db": (root / ".orx" / "state.db").exists(),
        }
        tmp = index_path.parent / (index_path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, index_path)
    finally:
        os.close(fd)


# ------------------------------------------------------------- index read ---

def test_index_path_resolution_follows_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "orxdata"))
    assert discovery.orx_index_path() == (
        tmp_path / "orxdata" / "projects-index.json")
    monkeypatch.delenv("ORX_DATA_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert discovery.orx_index_path() == (
        tmp_path / "xdg" / "orx" / "projects-index.json")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert str(discovery.orx_index_path()).endswith(
        os.path.join(".local", "share", "orx", "projects-index.json"))


def test_read_index_missing_unreadable_unsupported(tmp_path):
    missing = discovery.read_index(tmp_path / "absent.json")
    assert missing["status"] == "missing"

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert discovery.read_index(corrupt)["status"] == "unreadable"

    future = write_index(tmp_path / "future.json", {}, version=99)
    res = discovery.read_index(future)
    assert res["status"] == "unsupported"
    assert res["index_version"] == 99
    assert res["projects"] == {}


def test_read_index_ok_normalizes_symlinked_keys(tmp_path):
    real = tmp_path / "realproj"
    (real / ".orx").mkdir(parents=True)
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    write_index(tmp_path / "ix.json", {str(alias): {"last_run_id": "R001"}})
    res = discovery.read_index(tmp_path / "ix.json")
    assert res["status"] == "ok"
    assert list(res["projects"]) == [str(real)]
    assert res["projects"][str(real)]["last_run_id"] == "R001"


# ------------------------------------------------------ merge & identity ---

def test_three_sources_merge_and_dedupe_by_canonical_path(world, tmp_path):
    store = world["store"]
    registry.add_project(store, world["proj_a"])

    # the index lists proj_a through a symlink alias plus proj_b directly
    alias = tmp_path / "alias-a"
    os.symlink(world["proj_a"], alias)
    ix = write_index(tmp_path / "ix.json", {
        str(alias): {"has_state_db": True},
        world["proj_b"]: {"has_state_db": False, "last_run_id": "R001"},
    })

    # the scan finds a fresh project under a user-chosen root
    proj_c = tmp_path / "work" / "ProjC"
    make_min_orx_db(proj_c / ".orx" / "state.db")

    res = discovery.discover(store, index_file=ix,
                             scan_roots=[tmp_path / "work"], now=FIXED_NOW)
    by_key = {p["project"]: p for p in res["projects"]}
    assert set(by_key) == {canon(world["proj_a"]), canon(world["proj_b"]),
                           canon(proj_c)}
    assert by_key[canon(world["proj_a"])]["sources"] == ["registry", "index"]
    assert by_key[canon(world["proj_b"])]["sources"] == ["index"]
    assert by_key[canon(proj_c)]["sources"] == ["scan"]
    assert by_key[canon(proj_c)]["found_by_scan"] is True
    assert res["counts"]["merged"] == 3
    assert sorted(res["new_projects"]) == [canon(world["proj_b"]),
                                           canon(proj_c)]


def test_same_directory_name_never_merges_two_paths(tmp_path):
    store_dir = tmp_path / "home"
    from orxanalytics.store import Store
    store = Store(store_dir)
    p1 = tmp_path / "x" / "proj"
    p2 = tmp_path / "y" / "deep" / "proj"
    make_min_orx_db(p1 / ".orx" / "state.db")
    make_min_orx_db(p2 / ".orx" / "state.db")
    registry.add_project(store, str(p1))
    ix = write_index(tmp_path / "ix.json", {str(p2): {"last_run_id": "R001"}})

    res = discovery.discover(store, index_file=ix, now=FIXED_NOW)
    assert len(res["projects"]) == 2
    assert {p["project"] for p in res["projects"]} == {canon(p1), canon(p2)}
    # the shared last_run_id is a hint per project, never an identity key
    assert res["projects"][1]["index"]["last_run_id"] == "R001"


def test_concurrent_orx_registration_loses_no_project(tmp_path):
    """ORX-side writers serialize on the lock and replace atomically; the
    consumer's merged view must retain every registered project."""
    ix = tmp_path / "ix" / "projects-index.json"
    roots = []
    for i in range(16):
        r = tmp_path / f"proj{i:02d}"
        make_min_orx_db(r / ".orx" / "state.db")
        roots.append(r)

    def worker(chunk):
        for r, rid in chunk:
            _orx_register(ix, r, rid)

    threads = [threading.Thread(
        target=worker,
        args=([(r, f"R{i:03d}") for i, r in enumerate(roots[k::4])],))
        for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    res = discovery.read_index(ix)
    assert res["status"] == "ok"
    assert len(res["projects"]) == 16

    from orxanalytics.store import Store
    store = Store(tmp_path / "home")
    merged = discovery.discover(store, index_file=ix, now=FIXED_NOW)
    assert len(merged["projects"]) == 16
    assert len(merged["new_projects"]) == 16


def test_concurrent_registry_add_loses_no_project(tmp_path):
    from orxanalytics.store import Store
    home = tmp_path / "home"
    roots = []
    for i in range(8):
        r = tmp_path / f"reg{i}"
        r.mkdir()
        roots.append(r)

    def worker(r):
        s = Store(home)
        try:
            registry.add_project(s, str(r))
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(r,)) for r in roots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store = Store(home)
    keys = {p["project_key"] for p in registry.list_projects(store)}
    assert keys == {str(r) for r in roots}


# ----------------------------------------------------- health & degrade ---

def test_missing_index_degrades_with_explicit_notice(world, tmp_path):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    res = discovery.discover(store, index_file=tmp_path / "absent.json",
                             now=FIXED_NOW)
    assert res["index"]["status"] == "missing"
    assert any("自动发现范围受限" in n for n in res["notices"])
    assert [p["project"] for p in res["projects"]] == [canon(world["proj_a"])]


def test_unreadable_index_isolated_registry_unaffected(world, tmp_path):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    bad = tmp_path / "corrupt.json"
    bad.write_text("{{{{")
    res = discovery.discover(store, index_file=bad, now=FIXED_NOW)
    assert res["index"]["status"] == "unreadable"
    assert any("索引" in n for n in res["notices"])
    assert [p["project"] for p in res["projects"]] == [canon(world["proj_a"])]


def test_unsupported_index_version_isolated(world, tmp_path):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    future = write_index(tmp_path / "future.json", {world["proj_a"]: {}},
                         version=2)
    res = discovery.discover(store, index_file=future, now=FIXED_NOW)
    assert res["index"]["status"] == "unsupported"
    # registry facts still serve; the index source is just not merged
    assert [p["project"] for p in res["projects"]] == [canon(world["proj_a"])]
    assert res["projects"][0]["sources"] == ["registry"]


def test_unsupported_orx_schema_isolated_not_crash(tmp_path):
    from orxanalytics.store import Store
    store = Store(tmp_path / "home")
    old = tmp_path / "old-orx"
    make_min_orx_db(old / ".orx" / "state.db", version="7")
    ix = write_index(tmp_path / "ix.json", {str(old): {}})
    res = discovery.discover(store, index_file=ix, now=FIXED_NOW)
    assert len(res["projects"]) == 1
    entry = res["projects"][0]
    assert entry["data_health"]["health"] == display.HEALTH_UNSUPPORTED
    assert entry["data_health"]["schema_supported"] is False


def test_deleted_project_keeps_history_marks_unavailable(world):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    import_orx_project(store, canon(world["proj_a"]))

    import shutil
    shutil.rmtree(world["proj_a"])
    res = discovery.discover(store, now=FIXED_NOW)
    entry = res["projects"][0]
    assert entry["project"] == canon(world["proj_a"])
    assert entry["data_health"]["health"] == display.HEALTH_UNREADABLE
    assert entry["has_history"] is True
    # history is still physically in the store — nothing was cleared
    n = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_snapshots WHERE project_key=?",
        (canon(world["proj_a"]),)).fetchone()["c"]
    assert n >= 1
    codes = {w["code"] for w in res["warnings"]}
    assert "project-directory-missing" in codes


def test_unreadable_state_db_marks_unreadable(tmp_path):
    from orxanalytics.store import Store
    store = Store(tmp_path / "home")
    broken = tmp_path / "broken"
    db = broken / ".orx" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database at all")
    ix = write_index(tmp_path / "ix.json", {str(broken): {}})
    res = discovery.discover(store, index_file=ix, now=FIXED_NOW)
    assert res["projects"][0]["data_health"]["health"] == (
        display.HEALTH_UNREADABLE)


def test_healthy_project_with_fresh_snapshot_reports_ok(world):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    import_orx_project(store, canon(world["proj_a"]))
    fresh = (FIXED_NOW - timedelta(seconds=5)).isoformat()
    store.conn.execute(
        "UPDATE orx_snapshots SET taken_at=? WHERE project_key=?",
        (fresh, canon(world["proj_a"])))
    store.conn.commit()
    res = discovery.discover(store, now=FIXED_NOW)
    entry = res["projects"][0]
    assert entry["data_health"]["health"] == display.HEALTH_OK
    assert entry["runs_known"] >= 1
    assert entry["last_snapshot"]["schema_version"] == "9"


def test_index_entry_without_state_db_is_basic_only(world, tmp_path):
    store = world["store"]
    ix = write_index(tmp_path / "ix.json",
                     {world["proj_b"]: {"has_state_db": False}})
    res = discovery.discover(store, index_file=ix, now=FIXED_NOW)
    entry = res["projects"][0]
    assert entry["index"]["has_state_db"] is False
    assert entry["state_db_present"] is False
    assert entry["data_health"]["health"] == display.HEALTH_UNREADABLE
    codes = {w["code"] for w in res["warnings"]}
    assert "no-orx-state-db" in codes


# ----------------------------------------------------------- bounded scan ---

def test_scan_finds_orx_projects_only(tmp_path):
    good = tmp_path / "good"
    make_min_orx_db(good / ".orx" / "state.db")
    plain = tmp_path / "plain"
    plain.mkdir()
    res = discovery.scan_for_projects([tmp_path])
    assert res["projects"] == [str(good.resolve())]
    assert res["truncated"] is False


def test_scan_skips_dependency_dirs_and_git_internals(tmp_path):
    make_min_orx_db(tmp_path / "good" / ".orx" / "state.db")
    for skip in ("node_modules", ".venv", "venv", "__pycache__",
                 ".git", "target", "Pods", "DerivedData"):
        make_min_orx_db(tmp_path / skip / "hidden" / ".orx" / "state.db")
    res = discovery.scan_for_projects([tmp_path])
    assert res["projects"] == [canon(tmp_path / "good")]
    pruned = res["pruned_dirs"]
    for skip in ("node_modules", ".venv", ".git"):
        assert pruned.get(skip, 0) >= 1


def test_scan_never_follows_symlink_loops(tmp_path):
    proj = tmp_path / "proj"
    make_min_orx_db(proj / ".orx" / "state.db")
    os.symlink(proj, proj / "self")          # self loop
    os.symlink(tmp_path, proj / "parent")    # cycle to the scan root
    outside = tmp_path / "outside"
    make_min_orx_db(outside / ".orx" / "state.db")
    os.symlink(outside, tmp_path / "escape")  # link out of the root

    res = discovery.scan_for_projects([proj])
    assert res["projects"] == [canon(proj)]
    assert canon(outside) not in res["projects"]
    assert res["truncated"] is False


def test_scan_root_symlink_is_resolved(tmp_path):
    real = tmp_path / "real"
    make_min_orx_db(real / ".orx" / "state.db")
    link = tmp_path / "link"
    os.symlink(real, link)
    res = discovery.scan_for_projects([link])
    assert res["projects"] == [canon(real)]


def test_scan_depth_limit(tmp_path):
    at2 = tmp_path / "a" / "proj2"
    at3 = tmp_path / "a" / "b" / "proj3"
    make_min_orx_db(at2 / ".orx" / "state.db")
    make_min_orx_db(at3 / ".orx" / "state.db")
    res = discovery.scan_for_projects([tmp_path], max_depth=2)
    assert res["projects"] == [canon(at2)]
    deep = discovery.scan_for_projects([tmp_path], max_depth=3)
    assert canon(at3) in deep["projects"]


def test_scan_dir_budget_bounds_the_walk(tmp_path):
    for i in range(6):
        make_min_orx_db(tmp_path / f"d{i}" / ".orx" / "state.db")
    res = discovery.scan_for_projects([tmp_path], max_dirs=3)
    assert res["truncated"] is True
    assert res["scanned_dirs"] <= 3
    assert len(res["projects"]) <= 3


def test_scan_missing_root_reported_not_raised(tmp_path):
    res = discovery.scan_for_projects([tmp_path / "not-there"])
    assert res["projects"] == []
    assert res["missing_roots"] == [str(tmp_path / "not-there")]


# -------------------------------------------------- register & relocate ---

def test_register_discovered_is_explicit_and_idempotent(world, tmp_path):
    store = world["store"]
    registry.add_project(store, world["proj_a"])
    proj_c = tmp_path / "work" / "ProjC"
    make_min_orx_db(proj_c / ".orx" / "state.db")

    results = discovery.register_discovered(
        store, [canon(proj_c), canon(world["proj_a"])])
    by_key = {r["project"]: r for r in results}
    assert by_key[canon(proj_c)]["action"] == "registered"
    assert by_key[canon(world["proj_a"])]["action"] == "already-registered"

    keys = {p["project_key"] for p in registry.list_projects(store)}
    assert canon(proj_c) in keys
    again = discovery.register_discovered(store, [canon(proj_c)])
    assert again[0]["action"] == "already-registered"
    assert len(registry.list_projects(store)) == 2


def test_relocate_project_explicitly_rekeys_history(world, tmp_path):
    import shutil

    store = world["store"]
    registry.add_project(store, world["proj_a"])
    import_orx_project(store, canon(world["proj_a"]))

    moved = tmp_path / "moved" / "Elsewhere"      # a DIFFERENT directory name
    moved.mkdir(parents=True)
    shutil.move(world["proj_a"], str(moved))

    res = discovery.relocate_project(store, world["proj_a"], moved)
    assert res["relocated"] is True
    keys = {p["project_key"] for p in registry.list_projects(store)}
    assert canon(moved) in keys and canon(world["proj_a"]) not in keys
    n = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_snapshots WHERE project_key=?",
        (canon(moved),)).fetchone()["c"]
    assert n >= 1
    view = discovery.discover(store, now=FIXED_NOW)
    assert [p["project"] for p in view["projects"]] == [canon(moved)]
    assert view["projects"][0]["has_history"] is True

    # same canonical path is a no-op, unknown dir and occupied target refuse
    assert discovery.relocate_project(store, moved, moved)["relocated"] is False
    with pytest.raises(discovery.DiscoveryError):
        discovery.relocate_project(store, moved, tmp_path / "not-there")
    other = tmp_path / "other"
    other.mkdir()
    registry.add_project(store, str(other))
    with pytest.raises(discovery.DiscoveryError):
        discovery.relocate_project(store, moved, other)


def test_discovery_never_writes_the_index_file(world, tmp_path):
    store = world["store"]
    ix = write_index(tmp_path / "ix.json", {world["proj_a"]: {}})
    before = ix.read_bytes()
    discovery.discover(store, index_file=ix, now=FIXED_NOW)
    assert ix.read_bytes() == before
    assert not (tmp_path / "ix.lock").exists()


# ------------------------------------------------------------------- CLI ---

def test_cli_discover_reports_and_registers(tmp_path, monkeypatch, capsys):
    home = tmp_path / "analytics-home"
    monkeypatch.setenv("ORX_ANALYTICS_HOME", str(home))
    proj = tmp_path / "work" / "ProjC"
    make_min_orx_db(proj / ".orx" / "state.db")
    ix = write_index(tmp_path / "ix.json", {str(proj): {"last_run_id": "R002"}})

    rc = cli.main(["discover", "--index", str(ix),
                   "--scan", str(tmp_path / "work"), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["status"] == "ok"
    assert [p["project"] for p in out["projects"]] == [canon(proj)]
    assert out["projects"][0]["sources"] == ["index", "scan"]
    assert out["new_projects"] == [canon(proj)]

    rc = cli.main(["discover", "--index", str(ix), "--register", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["registered"][0]["action"] == "registered"

    rc = cli.main(["discover", "--index", str(ix)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "index: ok" in text
    assert canon(proj) in text
    assert "registry" in text

    from orxanalytics.store import Store
    store = Store(home)
    assert [p["project_key"] for p in registry.list_projects(store)] == [
        canon(proj)]


def test_cli_discover_text_notices_missing_index(tmp_path, monkeypatch,
                                                 capsys):
    monkeypatch.setenv("ORX_ANALYTICS_HOME", str(tmp_path / "home"))
    rc = cli.main(["discover", "--index",
                   str(tmp_path / "absent" / "projects-index.json")])
    assert rc == 0
    text = capsys.readouterr().out
    assert "自动发现范围受限" in text
