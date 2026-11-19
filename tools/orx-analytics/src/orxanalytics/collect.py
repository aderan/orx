"""One collect pass: global sources + per-project ORX snapshots."""

from __future__ import annotations

from pathlib import Path

from .importers import import_codex, import_cursor, import_zcode
from .importers.orxstate import import_orx_project
from .registry import list_projects
from .store import Store


def collect(store: Store, projects: list[str] | None = None,
            skip_global: bool = False) -> dict:
    results: dict[str, dict] = {"global": [], "orx": []}
    if not skip_global:
        for fn in (import_zcode, import_codex, import_cursor):
            results["global"].append(fn(store))
    wanted = None if not projects else {str(Path(p).expanduser().resolve())
                                         for p in projects}
    for p in list_projects(store):
        if wanted is not None and p["project_key"] not in wanted:
            continue
        if not p["orx_enabled"]:
            continue
        results["orx"].append(import_orx_project(store, p["project_key"]))
    return results


def provenance(store: Store) -> list[dict]:
    """Latest finished import per source: what, when, which reader."""
    import json
    rows = store.conn.execute(
        "SELECT source_id, MIN(started_at) first_import, "
        "i.started_at, i.finished_at, counts_json, error FROM import_runs i "
        "WHERE i.id IN (SELECT MAX(id) FROM import_runs "
        "GROUP BY source_id) ORDER BY source_id").fetchall()
    return [{"source_id": r["source_id"],
             "first_import": r["first_import"],
             "last_import_at": r["finished_at"] or r["started_at"],
             "counts": json.loads(r["counts_json"] or "{}"),
             "last_error": r["error"]}
            for r in rows]
