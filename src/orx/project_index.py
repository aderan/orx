"""User-level project index: ORX registers projects it executes.

The index feeds orx-analytics discovery — a separate, read-only consumer
(ADR-0001); the per-project ``.orx/state.db`` remains the only source of
truth for runs and usage. The consumer contract lives in orx-analytics
``docs/orx-integration.md`` (项目索引消费契约).

Registration is best-effort observation: every failure is recorded in the
returned dict and never raised — an index problem must never block
execution. Concurrent updates serialize on a lock file beside the index;
the index itself is replaced atomically and never loses other projects'
entries. A corrupt, wrong-shaped, or future-versioned index is refused
(registered False), never rewritten.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

INDEX_VERSION = 1
INDEX_NAME = "projects-index.json"

_LOCK_RETRIES = 10
_LOCK_RETRY_SLEEP_SEC = 0.05


def index_path() -> Path:
    """Index location via ORX data-dir resolution (ORX_DATA_DIR >
    XDG_DATA_HOME/orx > ~/.local/share/orx)."""
    from .config import user_data_dir

    return user_data_dir() / INDEX_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_project(root: Path | str, run_id: str | None = None) -> dict:
    """Register one project (canonical path + last-seen facts). Returns a
    small result dict; never raises."""
    try:
        return _register(Path(root), run_id)
    except Exception as e:  # noqa: BLE001 — the index never blocks execution
        return {"registered": False, "error": f"{type(e).__name__}: {e}"}


def _register(root: Path, run_id: str | None) -> dict:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_suffix(".lock")),
                 os.O_CREAT | os.O_RDWR, 0o644)
    try:
        locked = False
        for _ in range(_LOCK_RETRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                time.sleep(_LOCK_RETRY_SLEEP_SEC)
        if not locked:
            return {"registered": False, "error": "index lock busy",
                    "path": str(path)}
        data = _load(path)
        if data is None:
            return {"registered": False, "path": str(path),
                    "error": "index corrupt or wrong shape"
                             " (or future index_version); refusing to"
                             " overwrite"}
        key = str(root.resolve())
        now = _now()
        prev = data["projects"].get(key, {})
        data["projects"][key] = {
            "canonical_path": key,
            "name": Path(key).name,
            "first_seen_at": prev.get("first_seen_at", now),
            "last_seen_at": now,
            "last_run_id": (run_id if run_id is not None
                            else prev.get("last_run_id")),
            "has_state_db": (Path(key) / ".orx" / "state.db").exists(),
        }
        data["updated_at"] = now
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, path)
        return {"registered": True, "path": str(path), "canonical_path": key}
    finally:
        os.close(fd)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return {"index_version": INDEX_VERSION, "updated_at": None,
                "projects": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if (not isinstance(data, dict)
            or data.get("index_version") != INDEX_VERSION
            or not isinstance(data.get("projects"), dict)):
        return None
    return data
