"""Read-only database access helpers with provenance.

Every open goes through :func:`open_ro` (mode=ro + query_only). WAL sidecars
are read by SQLite itself; we never checkpoint, never delete -wal/-shm, and
never copy only the main file. For a private consistent copy we use the
backup API (:func:`snapshot_to`), which is a read on the source.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SourceUnavailable(Exception):
    """The source exists but cannot be read in the requested mode."""


def open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file in place, read-only, WAL-aware.

    mode=ro reads committed WAL frames without checkpointing. immutable=1
    would skip the WAL and is wrong for live databases, so it is never used.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def snapshot_to(src: Path, dest: Path) -> Path:
    """Consistent private copy via the backup API. Source stays read-only."""
    if dest.exists():
        raise FileExistsError(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = open_ro(src)
    d = sqlite3.connect(dest)
    s.backup(d)
    d.close()
    s.close()
    return dest


def file_digest(path: Path) -> str:
    """SHA-256 of a file, for read-only verification in tests and bundles."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Provenance:
    """Everything a consumer needs to know about where a reading came from."""

    source_kind: str                 # zcode | codex | cursor | orx
    location: str                    # resolved path
    reader_version: str
    snapshot_at: str                 # ISO-8601 UTC
    method: str = "in-place read-only"   # or "backup snapshot"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "location": self.location,
            "reader_version": self.reader_version,
            "snapshot_at": self.snapshot_at,
            "method": self.method,
            **({"extra": self.extra} if self.extra else {}),
        }
