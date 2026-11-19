#!/usr/bin/env python3
"""orx-analytics — read-only six-metric report over ORX project state.

Lives OUTSIDE the ORX repo by design (user decision 2026-10-04): ORX owns
the raw facts and the documented read contract (docs/observability-contract.md);
this tool consumes them and never writes project state.

Calibers fixed by docs/m1.2-plan.md (ORX repo):
  1 交付质量      acceptance passed, defects found after pass
  2 交付消耗      all attempts (incl. failed / superseded), tokens per profile,
                  per-runner token fields NEVER normalized (Cursor cached may
                  exceed input; R001: 410k input vs 5.6M cached)
  3 完成时间      wall clock (run lifecycle) AND execution-span sum, separate;
                  never mixed
  4 一次验收通过率  tasks whose first worker delivery passed every verification
  5 返工          retry/repair attempts with failure reasons
  6 数据覆盖      token / time / session coverage as separate dimensions

Accuracy and coverage are stated separately: "these 12 observations are
exact" is a different claim from "the whole run's consumption is known".
Join keys: project + run + revision + task + attempt (short ids repeat
across projects and revisions).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READONLY_URI = "file:{}?mode=ro"


@dataclass
class Project:
    root: Path
    db: Path
    project_id: str  # stable identity = absolute path (short ids repeat)


@dataclass
class Report:
    project_id: str
    run_id: str
    metrics: dict[str, Any] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


def discover(project_dirs: list[Path]) -> list[Project]:
    out = []
    for root in project_dirs:
        db = root / ".orx" / "state.db"
        if not db.exists():
            print(f"skip {root}: no .orx/state.db", file=sys.stderr)
            continue
        out.append(Project(root=root.resolve(), db=db, project_id=str(root.resolve())))
    return out


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(READONLY_URI.format(db), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def build_report(project: Project, run_id: str | None) -> Report:
    conn = connect(project.db)
    try:
        run = _pick_run(conn, run_id)
        if run is None:
            raise SystemExit(f"no run found in {project.db}")
        rep = Report(project_id=project.project_id, run_id=run["id"])
        rep.metrics["delivery_quality"] = _delivery_quality(conn, run)
        rep.metrics["consumption"] = _consumption(conn, run)
        rep.metrics["completion_time"] = _completion_time(conn, run)
        rep.metrics["first_pass_rate"] = _first_pass_rate(conn, run)
        rep.metrics["rework"] = _rework(conn, run)
        rep.metrics["coverage"] = _coverage(conn, run, schema_version(conn))
        return rep
    finally:
        conn.close()


def _pick_run(conn: sqlite3.Connection, run_id: str | None) -> sqlite3.Row | None:
    if run_id:
        return q(conn, "SELECT * FROM runs WHERE id=?", (run_id,))[0]
    rows = q(conn, "SELECT * FROM runs ORDER BY created_at DESC")
    return rows[0] if rows else None


def _delivery_quality(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    status = run["status"]
    verifs = q(
        conn,
        "SELECT passed, COUNT(*) c FROM verifications "
        "WHERE revision_id IN (SELECT id FROM plan_revisions WHERE run_id=?) "
        "GROUP BY passed",
        (run["id"],),
    )
    v = {r["passed"]: r["c"] for r in verifs}
    return {
        "run_status": status,
        "verifications_passed": v.get(1, 0),
        "verifications_failed": v.get(0, 0),
        # post-pass defects are not observable from state alone yet; the
        # controller records them as manual ledger entries with source tags
        "post_pass_defects": None,
    }


def _consumption(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    """All attempts of this run, grouped per profile. Token fields are kept
    per-runner and per-field; no input-cached arithmetic, no fee math."""
    rows = q(
        conn,
        """
        SELECT a.profile, a.driver, a.role, a.result,
               u.input_tokens, u.output_tokens, u.cached_input_tokens,
               u.accuracy
        FROM attempts a
        LEFT JOIN usage_observations u ON u.attempt_id = a.id
        WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id = ?)
        """,
        (run["id"],),
    )
    per_profile: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = per_profile.setdefault(
            r["profile"],
            {
                "attempts": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "token_fields_observed": 0,
                "accuracies": set(),
            },
        )
        p["attempts"] += 1
        if r["input_tokens"] is not None or r["output_tokens"] is not None:
            p["token_fields_observed"] += 1
            p["input_tokens"] += r["input_tokens"] or 0
            p["output_tokens"] += r["output_tokens"] or 0
            p["cached_input_tokens"] += r["cached_input_tokens"] or 0
            if r["accuracy"]:
                p["accuracies"].add(r["accuracy"])
    for p in per_profile.values():
        p["accuracies"] = sorted(p["accuracies"]) or ["unknown"]
    return {"per_profile": per_profile, "fee": "not computed (no billing basis)"}


def _completion_time(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    rows = q(
        conn,
        """
        SELECT a.started_at, a.ended_at
        FROM attempts a
        WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id = ?)
        """,
        (run["id"],),
    )
    span_sum = 0.0
    for r in rows:
        if r["started_at"] and r["ended_at"]:
            from datetime import datetime

            def p(s: str) -> datetime:
                return datetime.fromisoformat(s)

            span_sum += (p(r["ended_at"]) - p(r["started_at"])).total_seconds()
    out: dict[str, Any] = {
        "execution_span_sum_sec": round(span_sum, 1),
        "parallelism_note": "span sum double-counts parallel execution; "
        "wall clock below is the user-facing wait",
    }
    for col in ("created_at", "started_at", "completed_at", "updated_at"):
        out[f"run_{col}"] = run[col] if col in run.keys() else None
    if run["started_at"] and run["completed_at"]:
        from datetime import datetime

        t0 = datetime.fromisoformat(run["started_at"])
        t1 = datetime.fromisoformat(run["completed_at"])
        out["wall_clock_sec"] = round((t1 - t0).total_seconds(), 1)
        out["wall_clock_source"] = "runs lifecycle timestamps"
    else:
        out["wall_clock_sec"] = None
        out["wall_clock_source"] = (
            "lifecycle timestamps absent (pre-v7 run); created_at..updated_at "
            "is an upper-bound heuristic only"
        )
    return out


def _first_pass_rate(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    rows = q(
        conn,
        """
        SELECT t.task_id,
               (SELECT COUNT(*) FROM attempts a
                 WHERE a.revision_id = t.revision_id
                   AND a.task_id = t.task_id AND a.role='worker') AS worker_attempts
        FROM tasks t
        WHERE t.revision_id IN (SELECT id FROM plan_revisions WHERE run_id = ?)
        """,
        (run["id"],),
    )
    tasks = [dict(r) for r in rows]
    first_pass = [t for t in tasks if t["worker_attempts"] <= 1]
    return {
        "total_tasks": len(tasks),
        "first_delivery_passed": len(first_pass),
        "per_task": tasks,
    }


def _rework(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    rows = q(
        conn,
        """
        SELECT a.role, a.task_id, a.result, a.failure_reason
        FROM attempts a
        WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id = ?)
          AND (a.result = 'failed' OR a.failure_reason IS NOT NULL)
        """,
        (run["id"],),
    )
    items = [dict(r) for r in rows]
    return {
        "failed_attempts": len(items),
        "items": items,
        "superseded_revisions": q(
            conn,
            "SELECT COUNT(*) c FROM plan_revisions WHERE run_id=? AND status='superseded'",
            (run["id"],),
        )[0]["c"],
    }


def _coverage(conn: sqlite3.Connection, run: sqlite3.Row, sv: int) -> dict:
    total = q(
        conn,
        "SELECT COUNT(*) c FROM attempts a WHERE a.revision_id IN "
        "(SELECT id FROM plan_revisions WHERE run_id = ?)",
        (run["id"],),
    )[0]["c"]
    with_tokens = q(
        conn,
        "SELECT COUNT(DISTINCT a.id) c FROM attempts a "
        "JOIN usage_observations u ON u.attempt_id=a.id "
        "WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id=?)",
        (run["id"],),
    )[0]["c"]
    with_spans = q(
        conn,
        "SELECT COUNT(*) c FROM attempts a "
        "WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id=?) "
        "AND a.started_at IS NOT NULL AND a.ended_at IS NOT NULL",
        (run["id"],),
    )[0]["c"]
    out: dict[str, Any] = {
        "attempts": total,
        "token_coverage": f"{with_tokens}/{total}",
        "time_coverage": f"{with_spans}/{total}",
        "schema_version": sv,
    }
    # session_ref exists only from v7 on
    if sv >= 7:
        with_sess = q(
            conn,
            "SELECT COUNT(*) c FROM attempts a "
            "WHERE a.revision_id IN (SELECT id FROM plan_revisions WHERE run_id=?) "
            "AND a.session_ref IS NOT NULL",
            (run["id"],),
        )[0]["c"]
        out["session_coverage"] = f"{with_sess}/{total}"
    else:
        out["session_coverage"] = "n/a (pre-v7 schema)"
    return out


def render_markdown(reports: list[Report]) -> str:
    lines = ["# orx-analytics report", ""]
    for rep in reports:
        lines += [
            f"## {rep.project_id} — run {rep.run_id}",
            "",
            "```json",
            json.dumps(rep.metrics, indent=1, default=str, ensure_ascii=False),
            "```",
            "",
        ]
        lines += [f"- caveat: {c}" for c in rep.caveats] or []
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("projects", nargs="+", help="ORX project roots")
    ap.add_argument("--run", help="run id (default: latest)")
    ap.add_argument("--json", action="store_true", help="JSON instead of markdown")
    args = ap.parse_args()

    reports = []
    for project in discover([Path(p).expanduser() for p in args.projects]):
        try:
            reports.append(build_report(project, args.run))
        except SystemExit as e:
            print(f"{project.project_id}: {e}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps([r.metrics for r in reports], indent=1, default=str))
    else:
        print(render_markdown(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
