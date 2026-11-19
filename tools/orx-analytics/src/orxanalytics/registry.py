"""Project registration. Identity is the resolved absolute root path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .store import Store, now_iso


class RegistryError(Exception):
    pass


def add_project(store: Store, root: str, name: str | None = None,
                orx_enabled: bool = True) -> dict:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise RegistryError(f"{path} is not a directory")
    key = str(path)
    has_orx = (path / ".orx" / "state.db").exists()
    if orx_enabled and not has_orx:
        orx_enabled = False   # honest: no state.db -> basic analysis only
    row = {
        "project_key": key,
        "name": name or path.name,
        "orx_enabled": 1 if orx_enabled else 0,
        "registered_at": now_iso(),
    }
    store.conn.execute(
        "INSERT OR REPLACE INTO projects(project_key, name, orx_enabled, "
        "registered_at) VALUES (:project_key, :name, :orx_enabled, "
        ":registered_at)", row)
    store.conn.commit()
    row["orx_enabled"] = bool(row["orx_enabled"])
    row["note"] = ("" if has_orx or not orx_enabled or not orx_enabled else "")
    if orx_enabled is False and has_orx:
        row["note"] = "state.db exists but ORX analysis disabled at registration"
    elif not has_orx:
        row["note"] = "no .orx/state.db: registered for basic session analysis"
    return row


def remove_project(store: Store, key: str) -> bool:
    path = str(Path(key).expanduser().resolve())
    cur = store.conn.execute(
        "DELETE FROM projects WHERE project_key=?", (path,))
    store.conn.commit()
    return cur.rowcount > 0


def list_projects(store: Store) -> list[dict]:
    rows = store.conn.execute(
        "SELECT project_key, name, orx_enabled, registered_at "
        "FROM projects ORDER BY project_key").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["orx_enabled"] = bool(d["orx_enabled"])
        d["state_db_present"] = (
            Path(d["project_key"]) / ".orx" / "state.db").exists()
        out.append(d)
    return out


def get_project(store: Store, key: str) -> dict | None:
    for p in list_projects(store):
        if p["project_key"] == key or p["name"] == key:
            return p
    return None
