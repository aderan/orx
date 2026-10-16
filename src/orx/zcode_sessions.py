"""Read-only zcode session discovery for host worker identity.

Why this exists: a host worker is a subagent the Controller's session
spawns. ORX never sees the spawn, and zcode exports no session id into the
subagent's shell environment — so ``attempts.session_ref`` stayed NULL and
the recovery contract ("check the original subagent, handle =
session_ref") had no handle to use.

Two anchors, strongest first:

- **Nonce (schema v11)**: ORX mints ``orx-assignment:<uuid4>`` per attempt
  and embeds it as the first lines of the dispatch prompt. A subagent
  session whose first text part carries the token IS the dispatched
  assignment — exact, unguessable, and immune to the controller-side prompt
  paraphrasing that erased every "attempt N" marker in R006 (5 of 7 worker
  prompts lost the marker; one carried a closed attempt's number from
  stale evidence lineage). When an attempt has a nonce, the nonce is the
  only authority: a marker hit is not consulted, because stale lineage
  references are exactly the failure mode the nonce exists to prevent.
- **Marker (legacy)**: attempts from before v11 have no nonce; their
  discovery still matches a human-readable "attempt N" in the first text
  part plus directory and subagent structure.

The zcode database is opened read-only (mode=ro + query_only), never
migrated or checkpointed. Default location can be overridden with
ORX_ZCODE_DB for tests and alternate installs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

_MARKER = re.compile(r"\battempt (\d+)\b")
# ORX_ASSIGNMENT=orx-assignment:<uuid4> — the identity line dispatch prompts
# carry at their very top (first lines survive the 2000-char first-part
# truncation any reader applies).
_NONCE = re.compile(
    r"ORX_ASSIGNMENT=(orx-assignment:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


class ZcodeDbUnavailable(Exception):
    """The zcode database is absent or unreadable: discovery cannot run."""


@dataclass
class Candidate:
    session_id: str
    parent_id: str
    title: str
    created_ms: int
    matched_text: str | None = None
    first_part_preview: str = field(default="", repr=False)
    directory_matched: bool = True

    def payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "title": self.title,
            "created_ms": self.created_ms,
            "matched_text": self.matched_text,
            "first_part_preview": self.first_part_preview[:160],
            "directory_matched": self.directory_matched,
        }


def zcode_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ORX_ZCODE_DB", "").strip()
    return Path(env).expanduser() if env else DEFAULT_ZCODE_DB


def _open_ro(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise ZcodeDbUnavailable(f"no zcode database at {db}")
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def discover_attempt_session(attempt_id: int, project_root: Path,
                             db: str | None = None,
                             created_within_ms: int | None = None,
                             now_ms: int | None = None,
                             nonce: str | None = None) -> dict:
    """Find the zcode subagent session that executed this attempt, inside
    this project's directory.

    With ``nonce`` (a v11 attempt), the session is the one whose first text
    part carries ``ORX_ASSIGNMENT=<nonce>`` — the token ORX embedded in the
    dispatch prompt. Without one (legacy attempts), the marker fallback
    matches a first part naming "attempt <id>".

    Directory semantics differ by anchor strength (G010, from the
    orx-analytics G002/R002 evidence): a real subagent INHERITS the
    controller window's ``session.directory`` — controlling project B from a
    session rooted in project A left all 18 of that run's nonce-exact
    sessions recorded under A's directory, and the old required-directory
    predicate disqualified every one of them (session_ref NULL,
    host_session_unbound, 1,222 unrecorded requests). So:

    - **nonce**: the one-time token is the authority. A first-part nonce hit
      binds wherever the session's directory points; the directory fact is
      RECORDED per candidate (``directory_matched``) instead of filtered on.
      The structural guards stay: a parent-less ROOT session is never a
      candidate (the controller's own session, even if its first part
      quotes the nonce, is not the executor), and two nonce-carrying
      sessions are still ``ambiguous`` — no binding, no guess by proximity.
    - **marker (legacy)**: a human-readable "attempt N" is weak evidence and
      the directory stays REQUIRED, exactly as before.

    Directory matching resolves symlinks on both sides (G009 T009/T010):
    the project root arrives resolved (``/private/tmp/...`` on macOS) while
    the session records the spelling it was spawned with (``/tmp/...``).
    Both spellings of the same directory match; different directories
    never do.

    Returns {"candidates": [...], "decision": "unique"|"none"|"ambiguous",
             "basis": str, "directory_filter": "not_required"|"required"} —
    the caller decides what to store; this function never writes anything
    anywhere.
    """
    path = zcode_db_path(db)
    root = str(project_root.resolve())
    root_real = os.path.realpath(root)
    try:
        conn = _open_ro(path)
    except sqlite3.Error as e:  # pragma: no cover - environment dependent
        raise ZcodeDbUnavailable(f"cannot open {path} read-only: {e}")
    try:
        rows = conn.execute(
            "SELECT id, parent_id, title, time_created, directory "
            "FROM session WHERE parent_id IS NOT NULL "
            "ORDER BY time_created").fetchall()
        if created_within_ms is not None and now_ms is not None:
            rows = [r for r in rows
                    if now_ms - r["time_created"] <= created_within_ms]

        def directory_matches(directory) -> bool:
            if directory == root:
                return True
            return os.path.realpath(directory) == root_real

        cands: list[Candidate] = []
        for r in rows:
            part = conn.execute(
                "SELECT data FROM part WHERE session_id=? AND "
                "data LIKE '{\"type\":\"text\"%' ORDER BY sequence LIMIT 1",
                (r["id"],)).fetchone()
            if part is None:
                continue
            try:
                text = json.loads(part["data"]).get("text", "") or ""
            except (json.JSONDecodeError, AttributeError):
                continue
            if nonce is not None:
                # Nonce authority (G010): match first, ask the directory
                # afterward — the hit is recorded with its directory fact,
                # never filtered on it.
                hit = _NONCE.search(text)
                if hit and hit.group(1) == nonce:
                    cands.append(Candidate(
                        session_id=r["id"], parent_id=r["parent_id"],
                        title=r["title"], created_ms=r["time_created"],
                        matched_text=hit.group(0),
                        first_part_preview=text,
                        directory_matched=directory_matches(r["directory"])))
            else:
                # Legacy marker: the directory stays a required predicate.
                if not directory_matches(r["directory"]):
                    continue
                hit = _MARKER.search(text)
                if hit and int(hit.group(1)) == attempt_id:
                    cands.append(Candidate(
                        session_id=r["id"], parent_id=r["parent_id"],
                        title=r["title"], created_ms=r["time_created"],
                        matched_text=hit.group(0),
                        first_part_preview=text,
                        directory_matched=True))
        n = len(cands)
        decision = "unique" if n == 1 else ("none" if n == 0 else "ambiguous")
        if nonce is not None:
            basis = (
                "zcode subagent session whose first text part carries the "
                "attempt's one-time identity token "
                f"ORX_ASSIGNMENT={nonce} and a non-null parent (subagent "
                "structure); the nonce is the authority, so the session's "
                "directory is recorded per candidate (directory_matched) "
                "and not required — real subagents inherit the controller "
                "window's directory, which need not be this project's root "
                "(G010, orx-analytics G002/R002); a parent-less root "
                "session is never a candidate; read-only lookup, no writes"
            )
            directory_filter = "not_required"
        else:
            basis = (
                "zcode subagent session whose first text part names "
                f"'attempt {attempt_id}' with session.directory resolving to "
                "the same directory as the project root (symlink spellings "
                "match) and a non-null parent (subagent structure); "
                "read-only lookup, no writes"
            )
            directory_filter = "required"
        return {
            "attempt_id": attempt_id,
            "project_root": root,
            "zcode_db": str(path),
            "decision": decision,
            "candidates": [c.payload() for c in cands],
            "basis": basis,
            "directory_filter": directory_filter,
        }
    finally:
        conn.close()


def first_session_id(result: dict) -> str | None:
    """The single candidate's id, or None for none/ambiguous."""
    if result.get("decision") == "unique":
        return result["candidates"][0]["session_id"]
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def session_facts(session_id: str, db: str | None = None) -> dict:
    """Read-only terminal-fact observation for ONE zcode session (G009 T006).

    What the a130 incident (2026-10-07) needed and lacked: an independently
    callable read of the session's own facts — created, last activity,
    archived, model-request count, Edit/Write tool-call counts — every fact
    carrying its source and its ORIGINAL value (epoch milliseconds;
    timezone-neutral as recorded, never re-zoned by this reader).

    This is observation, never judgement:

    - ``verdict`` is always None. No liveness claim, no death claim; "the
      session has had no activity since X" is a fact, not "the worker is
      dead", and silence in the ORX heartbeat channel is a DIFFERENT fact
      that this function does not even read.
    - The zcode database is opened read-only (mode=ro + query_only) and is
      never written, migrated, or checkpointed.
    - Schemas vary (real installs and test stand-ins differ): a fact whose
      column or table the installed database lacks is named in
      ``unreadable`` and simply absent from ``facts`` — it is never guessed
      or filled from another source.

    Raises ZcodeDbUnavailable when the database is absent/unreadable; a
    session id that matches no row returns ``found: False`` with empty
    facts (an honest "no such session", not an error).
    """
    path = zcode_db_path(db)
    conn = _open_ro(path)
    try:
        session_cols = _columns(conn, "session")
        part_cols = _columns(conn, "part")
        row = conn.execute(
            "SELECT * FROM session WHERE id = ?", (session_id,)
        ).fetchone() if "id" in session_cols else None
        base = {
            "session_id": session_id,
            "verdict": None,
            "basis": (
                "read-only lookup on the zcode session database (mode=ro,"
                " query_only): observation facts with sources, never a"
                " liveness or death judgment"
            ),
        }
        if row is None:
            return {**base, "found": False, "facts": {}, "unreadable": []}
        facts: dict[str, dict] = {}
        unreadable: list[str] = []
        for name, column in (
            ("created_ms", "time_created"),
            ("last_activity_ms", "time_updated"),
            ("archived_ms", "time_archived"),
        ):
            if column in session_cols:
                # A readable NULL (e.g. never archived) stays a fact with
                # value None; only a missing column is 'unreadable'.
                facts[name] = {"value": row[column],
                               "source": f"zcode.session.{column}"}
            else:
                unreadable.append(name)
        if "time_created" in part_cols:
            last_part = conn.execute(
                "SELECT MAX(time_created) AS m FROM part WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            facts["last_part_ms"] = {
                "value": last_part["m"] if last_part is not None else None,
                "source": "zcode.part.time_created (max)",
            }
        else:
            unreadable.append("last_part_ms")
        if "data" in part_cols:
            try:
                counts: dict[str, int] = {}
                for r in conn.execute(
                    "SELECT json_extract(data, '$.type') AS t,"
                    " json_extract(data, '$.tool') AS tool, count(*) AS n"
                    " FROM part WHERE session_id = ? GROUP BY t, tool",
                    (session_id,),
                ):
                    if r["t"] == "step-finish":
                        counts["model_steps"] = counts.get("model_steps", 0) + r["n"]
                    elif r["t"] == "tool" and r["tool"] == "Edit":
                        counts["edit_calls"] = counts.get("edit_calls", 0) + r["n"]
                    elif r["t"] == "tool" and r["tool"] == "Write":
                        counts["write_calls"] = counts.get("write_calls", 0) + r["n"]
                for name, source in (
                    ("model_steps", "zcode.part type=step-finish count"),
                    ("edit_calls", "zcode.part type=tool tool=Edit count"),
                    ("write_calls", "zcode.part type=tool tool=Write count"),
                ):
                    if name in counts:
                        facts[name] = {"value": counts[name], "source": source}
                    else:
                        facts[name] = {"value": 0, "source": source}
            except sqlite3.OperationalError:
                # json1 unavailable in this build: the counts are honestly
                # unreadable, never zero-filled.
                unreadable.extend(("model_steps", "edit_calls", "write_calls"))
        else:
            unreadable.extend(("model_steps", "edit_calls", "write_calls"))
        return {**base, "found": True, "facts": facts, "unreadable": unreadable}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# G009 T010: read-only usage aggregation over the zcode session tree.
#
# The incident audit needed per-book token attribution (controller root
# session vs worker/verifier subagent sessions) and the dedup rules that
# keep one request from being counted twice:
#
# - identity of one model request is (session_id, logical_request_id,
#   attempt_index); a row repeating that triple is a duplicate record of
#   the SAME request, dropped from every sum;
# - ZCode's cache_read is a SUBSET of input (computed_total = input +
#   output; see adapters.base.HARNESS_CACHE_SEMANTICS) so no derived total
#   here ever adds cache on top of input;
# - an error-status row whose token columns are all zero carries
#   placeholder zeros, not evidence of a free request: it is counted as a
#   request and named in ``error_rows_zero_tokens`` so readers can keep
#   the caveat the audit recorded;
# - turn_usage / message tokens are NOT added: model_usage is the
#   per-request authority and summing two granularities would double count.


def session_tree(session_id: str, db: str | None = None) -> dict:
    """One session plus every descendant (session.parent_id recursion).

    Read-only. Returns {"root": session_id, "sessions": [root, ...]} with
    descendants in discovery order. A session id that matches no row
    returns an empty list — never an error, never a guess about other
    sessions.
    """
    path = zcode_db_path(db)
    conn = _open_ro(path)
    try:
        rows = conn.execute(
            "WITH RECURSIVE tree(sid) AS ("
            "  SELECT id FROM session WHERE id = ?"
            "  UNION ALL"
            "  SELECT s.id FROM session s JOIN tree t ON s.parent_id = t.sid"
            ") SELECT sid FROM tree",
            (session_id,),
        ).fetchall()
        sessions = [r["sid"] for r in rows]
        return {"root": session_id, "sessions": sessions}
    except sqlite3.OperationalError:
        # Schema stand-ins without the recursive shape: the root alone is
        # the honest answer — no descendants are ever inferred.
        return {"root": session_id, "sessions": [session_id]}
    finally:
        conn.close()


def _zero_aggregate(session_ids) -> dict:
    return {
        "session_ids": list(session_ids),
        "requests": 0,
        "status_counts": {},
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "computed_total_tokens": None,
        "uncached_input_tokens": None,
        "max_request_input_tokens": None,
        "mean_request_input_tokens": None,
        "duplicate_request_rows_dropped": 0,
        "error_rows_zero_tokens": 0,
        "settling_request_rows": 0,
        "unreadable": [],
    }


# G010: which model_usage statuses carry FINAL numbers. Read from the real
# database on 2026-10-16 (read-only): the whole table holds exactly
# completed / error / cancelled. A row with any other status (a request
# still streaming when the reader ran) makes every sum provisional — the
# caller states it instead of recording a partial book.
TERMINAL_USAGE_STATUSES = frozenset({"completed", "error", "cancelled"})


def model_usage_aggregate(session_ids, db: str | None = None) -> dict:
    """Aggregate model_usage over an explicit set of sessions.

    Dedup: (session_id, logical_request_id, attempt_index) identifies one
    request; a repeated triple is dropped and counted in
    ``duplicate_request_rows_dropped``. Token sums never add cache on top of
    input (ZCode subset semantics): ``computed_total_tokens`` sums the
    provider's own column and ``uncached_input_tokens`` is input minus
    cache read. Missing columns/tables land in ``unreadable`` and their
    sums stay None — never zero. Rows whose status is outside the terminal
    vocabulary are counted in ``settling_request_rows``: their sums are
    provisional, and honest callers refuse to record them (G010).
    """
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return _zero_aggregate([])
    path = zcode_db_path(db)
    conn = _open_ro(path)
    try:
        columns = _columns(conn, "model_usage")
        if not columns:
            result = _zero_aggregate(ids)
            result["unreadable"].append("model_usage")
            return result
        required = {"session_id", "logical_request_id", "attempt_index",
                    "status"}
        missing = sorted(required - columns)
        if missing:
            result = _zero_aggregate(ids)
            result["unreadable"].extend(f"model_usage.{name}" for name in missing)
            return result
        qmarks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM model_usage WHERE session_id IN ({qmarks})",
            ids,
        ).fetchall()
        seen: set[tuple] = {}
        dropped = 0
        counted = []
        for row in rows:
            key = (row["session_id"], row["logical_request_id"],
                   row["attempt_index"])
            if key in seen:
                dropped += 1
                continue
            seen[key] = True
            counted.append(row)
        result = _zero_aggregate(ids)
        result["duplicate_request_rows_dropped"] = dropped
        result["requests"] = len(counted)
        status_counts: dict[str, int] = {}
        for row in counted:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        result["status_counts"] = status_counts
        result["settling_request_rows"] = sum(
            1 for row in counted
            if row["status"] not in TERMINAL_USAGE_STATUSES
        )

        def column_sum(name: str, alias: str) -> None:
            if name in columns:
                result[alias] = sum(int(row[name]) for row in counted)
            else:
                result["unreadable"].append(f"model_usage.{name}")

        column_sum("input_tokens", "input_tokens")
        column_sum("output_tokens", "output_tokens")
        column_sum("cache_read_input_tokens", "cache_read_input_tokens")
        column_sum("computed_total_tokens", "computed_total_tokens")
        if "input_tokens" in columns:
            inputs = [int(row["input_tokens"]) for row in counted]
            result["max_request_input_tokens"] = max(inputs) if inputs else 0
            result["mean_request_input_tokens"] = (
                sum(inputs) / len(inputs) if inputs else 0.0
            )
        else:
            result["unreadable"].append("model_usage.input_tokens")
        if ("input_tokens" in columns and "cache_read_input_tokens" in columns
                and result["input_tokens"] is not None
                and result["cache_read_input_tokens"] is not None):
            result["uncached_input_tokens"] = (
                result["input_tokens"] - result["cache_read_input_tokens"]
            )
        if "status" in columns:
            tokenish = ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "computed_total_tokens")
            zero_token_columns = [name for name in tokenish if name in columns]
            result["error_rows_zero_tokens"] = sum(
                1 for row in counted
                if row["status"] == "error"
                and zero_token_columns
                and all(int(row[name]) == 0 for name in zero_token_columns)
            )
        return result
    finally:
        conn.close()


def session_tree_usage(session_id: str, db: str | None = None) -> dict:
    """model_usage aggregate over one session's whole subtree.

    The subtree is the session plus every descendant — the unit a host
    worker's own book covers (its session and anything it spawned). The
    PARENT session is never included: the controller book and the worker
    book stay separate by construction.
    """
    tree = session_tree(session_id, db=db)
    aggregate = model_usage_aggregate(tree["sessions"], db=db)
    aggregate["session_ids"] = tree["sessions"]
    return aggregate
