"""M1.2 acceptance: hermetic lifecycle coverage, plus the live R002 report.

The hermetic test never opens the dogfood database and never calls a paid
model. The dogfood test reads `.orx/state.db` read-only, replays one stored
host_report through the real CLI (same counts, so the write is idempotent),
and records the contract queries verbatim in docs/m1.2-report.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from orx import dispatch
from orx.adapters import cursor as cursor_adapter
from orx.cli import app
from orx.state import Store

from conftest import ir_for, make_project, task_spec, write_evidence
from test_adapters import SESSION, _cursor_envelope, install_cursor_stdout, make_cursor_project
from test_observability_contract import load_queries, open_readonly, run as run_query
from test_state import _narrow_usage_check, _strip_v7_columns

ROOT = Path(__file__).parents[1]
REPORT = ROOT / "docs" / "m1.2-report.md"
LIVE_DB = ROOT / ".orx" / "state.db"
runner = CliRunner()

SIX = (
    "delivery_quality",
    "consumption",
    "elapsed",
    "first_acceptance",
    "rework",
    "coverage",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_m12_acceptance_integration(tmp_path: Path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    opened = Store.open(fresh)
    try:
        assert opened.schema_version() == 9
        assert {"session_ref", "run_id", "usage_missing_reason"} <= _columns(opened.conn, "attempts")
        assert {"started_at", "completed_at"} <= _columns(opened.conn, "runs")
    finally:
        opened.close()

    legacy = tmp_path / "legacy.db"
    store = Store.open(legacy)
    goal, run = store.goal_create("legacy objective", ["keep this"], [], "")
    revision = store.revision_create(run.id, "light", "legacy-planner", {"tasks": []})
    linked = store.attempt_create(
        revision.id, "worker", "legacy", "cli", "shell", "m", "high", task_id="T001",
    )
    loose = store.attempt_create(None, "planner", "legacy", "host", "zcode", "m", "low")
    store.usage_add(linked.id, "legacy", run.id, "T001", 3, 1, None, "native_cli", "unknown")
    store.conn.execute(
        "UPDATE runs SET status = 'done', updated_at = '2099-01-01T00:00:00+00:00' WHERE id = ?",
        (run.id,),
    )
    _narrow_usage_check(store.conn)
    _strip_v7_columns(store.conn)
    store.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    store.close()

    upgraded = Store.open(legacy)
    try:
        assert upgraded.schema_version() == 9
        migrated = upgraded.run_get(run.id)
        assert migrated.started_at is None and migrated.completed_at is None
        assert migrated.updated_at == "2099-01-01T00:00:00+00:00"
        rows = {row.id: row for row in upgraded.attempts_all()}
        assert rows[linked.id].run_id == run.id
        assert rows[linked.id].session_ref is None
        assert rows[linked.id].usage_missing_reason is None
        assert rows[loose.id].run_id is None
        assert rows[loose.id].session_ref is None
        usage = upgraded.usage_rows()
        assert len(usage) == 1
        assert usage[0]["source"] == "native_cli"
        assert usage[0]["accuracy"] == "unknown"
        assert usage[0]["cached_input_tokens"] is None
        assert usage[0]["input_tokens"] == 3
    finally:
        upgraded.close()
    assert not list(tmp_path.glob("legacy.db.migrate-*"))

    host_root = tmp_path / "host"
    host_root.mkdir()
    monkeypatch.chdir(host_root)
    monkeypatch.setenv("ORX_SESSION_REF", "host-sess-live")
    monkeypatch.setenv("ORX_PROJECT", str(host_root))
    project = make_project(host_root)
    worker_id = None
    started = None
    try:
        goal, run = dispatch.create_goal(
            project,
            objective="Ship the acceptance fixture",
            acceptance=["marker file exists"],
            constraints=[],
            context="",
        )
        created = project.store.run_get(run.id)
        assert created.status == "planning"
        assert created.started_at is None and created.completed_at is None

        dispatch.plan_route(project)
        planner = next(row for row in project.store.attempts_all() if row.role == "planner")
        assert planner.session_ref == "host-sess-live"
        assert planner.started_at is not None and planner.ended_at is None
        assert planner.run_id == run.id

        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["marker file exists"], verification=["test -f ready.txt"]),
        ]))
        closed = project.store.attempt_get(planner.id)
        assert closed.result == "completed"
        assert closed.started_at is not None and closed.ended_at is not None
        running = project.store.run_get(run.id)
        assert running.status == "running"
        assert running.started_at is not None and running.completed_at is None
        started = running.started_at

        dispatch.run_slice(project)
        claimed = dispatch.task_claim(project, "T001", session="from-claim")
        assert claimed["session_ref"] == "from-claim"
        worker = project.store.attempt_get(claimed["attempt"])
        worker_id = worker.id
        assert worker.started_at is not None and worker.ended_at is None
        assert worker.session_ref == "from-claim"
        assert worker.run_id == run.id

        (host_root / "ready.txt").write_text("ok\n")
        finished = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
        assert finished["status"] == "passed"
        worker = project.store.attempt_get(worker_id)
        assert worker.ended_at is not None and worker.result == "completed"
    finally:
        project.close()

    recorded = runner.invoke(app, [
        "usage", "record", "--json",
        "--attempt", str(worker_id),
        "--input", "4",
        "--output", "2",
        "--accuracy", "exact",
    ])
    assert recorded.exit_code == 0, recorded.stdout + recorded.stderr
    body = json.loads(recorded.stdout)
    assert body["ok"] is True
    assert body["source"] == "host_report"
    assert body["accuracy"] == "exact"
    assert body["input_tokens"] == 4
    assert body["output_tokens"] == 2
    assert body["cached_input_tokens"] is None
    assert body["idempotent"] is False
    again = runner.invoke(app, [
        "usage", "record", "--json",
        "--attempt", str(worker_id),
        "--input", "4",
        "--output", "2",
        "--accuracy", "exact",
    ])
    assert again.exit_code == 0, again.stdout
    assert json.loads(again.stdout)["idempotent"] is True

    project = dispatch.open_project()
    try:
        report = dispatch.usage(project)
        observed = next(row for row in report["observations"] if row["attempt_id"] == worker_id)
        assert observed["source"] == "host_report"
        assert observed["accuracy"] == "exact"
        assert observed["session_ref"] == "from-claim"
        assert observed["cached_input_tokens"] is None
        assert "fee" not in observed
        assert project.store.attempt_get(worker_id).usage_missing_reason is None

        done = dispatch.status_data(project)
        assert done["goal"]["status"] == "done"
        assert done["run"]["status"] == "done"
        assert done["run"]["started_at"] == started
        assert done["run"]["completed_at"] is not None
        completed = done["run"]["completed_at"]
        run_id = done["run"]["id"]
    finally:
        project.close()

    queries = load_queries()
    conn = open_readonly(host_root / ".orx" / "state.db")
    try:
        assert run_query(conn, queries["schema_gate"])[0]["decision"] == "ok"
        quality = run_query(conn, queries["delivery_quality"], "m12-host")
        assert [(row["task_id"], row["status"]) for row in quality] == [("T001", "passed")]
        spent = run_query(conn, queries["consumption"], "m12-host")[0]
        assert spent["canonical_observations"] == 1
        assert spent["input_tokens_sum"] == 4
        assert spent["output_tokens_sum"] == 2
        assert spent["cached_missing"] == 1
        assert spent["cached_input_tokens_sum"] is None
        assert spent["accuracy_exact"] == 1
        elapsed = {row["run_id"]: row for row in run_query(conn, queries["elapsed"], "m12-host")}
        assert elapsed[run_id]["spans_incomplete"] == 0
        assert elapsed[run_id]["spans_complete"] >= 1
        assert elapsed[run_id]["waiting_sec"] is not None
        assert elapsed[run_id]["span_sum_sec"] is not None
        first = run_query(conn, queries["first_acceptance"], "m12-host")
        assert [(row["task_id"], row["first_pass"]) for row in first] == [("T001", 1)]
        assert list(run_query(conn, queries["rework"], "m12-host")) == []
        assert run_query(conn, queries["coverage"], "m12-host")
        fields = run_query(conn, queries["coverage_fields"], "m12-host")[0]
        assert fields["observations"] == 1
        assert fields["cached_missing"] == 1
        assert fields["input_present"] == 1
    finally:
        conn.close()

    project = dispatch.open_project()
    try:
        dispatch.plan_route(project)
        reopened = project.store.run_get(run_id)
        # G004 T003: routing a replan does NOT reopen a completed run
        # anymore — the recorded completion survives until a new revision
        # actually lands.
        assert reopened.status == "done"
        assert reopened.completed_at == completed
        assert reopened.started_at == started
    finally:
        project.close()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ORX_PROJECT", str(cli_root))
    cursor_adapter.reset_caches()
    install_cursor_stdout(bindir, _cursor_envelope(usage={
        "inputTokens": 10,
        "outputTokens": 4,
        "cacheReadTokens": 500,
    }))
    cli = make_cursor_project(cli_root, planner=False, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(cli, "goal text", ["a1"])[0]
        dispatch.submit_plan(cli, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        outcome = dispatch.run_slice(cli)
        assert outcome["started"][0]["status"] == "passed"
        attempt = next(row for row in cli.store.attempts_all() if row.role == "worker")
        assert attempt.started_at is not None and attempt.ended_at is not None
        assert attempt.session_ref == SESSION
        assert attempt.session_ref != "host-sess-live"
        assert attempt.run_id is not None
        assert attempt.usage_missing_reason is None
        row = next(item for item in cli.store.usage_rows() if item["attempt_id"] == attempt.id)
        assert row["source"] == "native_cli"
        assert row["accuracy"] == "exact"
        assert row["input_tokens"] == 10
        assert row["cached_input_tokens"] == 500
        assert row["cached_input_tokens"] > row["input_tokens"]
        data = dispatch.status_data(cli)
        assert data["run"]["status"] == "done"
        assert data["run"]["started_at"] is not None
        assert data["run"]["completed_at"] is not None
    finally:
        cli.close()
        cursor_adapter.reset_caches()


def _dump(rows) -> str:
    payload = [dict(row) for row in rows]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _cohort(attempts: list[dict], observations: dict[int, int]) -> dict:
    """Split one attempt list into coverage buckets. Pending is not a miss."""
    buckets = {
        "attempts": len(attempts),
        "observed": 0,
        "completed_miss_recorded": [],
        "legacy_unassessed": [],
        "pending": [],
        "span_incomplete": [],
        "session_present": 0,
        "session_absent": 0,
    }
    by_role: dict[str, dict] = {}
    for attempt in attempts:
        role = attempt["role"]
        slot = by_role.setdefault(role, {
            "attempts": 0, "observed": 0, "missing_recorded": 0,
            "unassessed": 0, "pending": 0,
        })
        slot["attempts"] += 1
        observed = attempt["id"] in observations
        completed = attempt["result"] is not None and attempt["ended_at"] is not None
        if attempt["session_ref"]:
            buckets["session_present"] += 1
        else:
            buckets["session_absent"] += 1
        if observed:
            buckets["observed"] += 1
            slot["observed"] += 1
        elif not completed:
            buckets["pending"].append(attempt)
            slot["pending"] += 1
        elif attempt["usage_missing_reason"]:
            buckets["completed_miss_recorded"].append(attempt)
            slot["missing_recorded"] += 1
        else:
            buckets["legacy_unassessed"].append(attempt)
            slot["unassessed"] += 1
        if completed and (attempt["started_at"] is None or attempt["ended_at"] is None):
            buckets["span_incomplete"].append(attempt)
    buckets["by_role"] = by_role
    return buckets


def _attempt_line(attempt: dict) -> str:
    return (
        f"- attempt {attempt['id']} role={attempt['role']} profile={attempt['profile']} "
        f"harness={attempt['harness']} driver={attempt['driver']} run_id={attempt['run_id']} "
        f"result={attempt['result']} started_at={attempt['started_at']} "
        f"ended_at={attempt['ended_at']} session_ref={attempt['session_ref']} "
        f"usage_missing_reason={attempt['usage_missing_reason']}"
    )


def test_dogfood_readonly_report(tmp_path: Path):
    """Measure the dogfood database without migrating it, and write the report."""
    assert LIVE_DB.is_file(), "dogfood .orx/state.db is the M1.2 fixture"
    snap = tmp_path / "r002-snapshot.db"
    src = open_readonly(LIVE_DB)
    try:
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
            # backup preserves the WAL header; a WAL database without its
            # sidecars cannot be reopened mode=ro. The snapshot is ours, so
            # switching it to a rollback journal makes it self-contained.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    before = LIVE_DB.stat().st_mtime_ns

    conn = open_readonly(snap)
    try:
        queries = load_queries()
        gate_rows = run_query(conn, queries["schema_gate"])
        assert gate_rows[0]["decision"] == "ok"
        gate = _dump(gate_rows)
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        six = {name: _dump(run_query(conn, queries[name], "ORX")) for name in SIX}
        extra = {
            name: _dump(run_query(conn, queries[name], "ORX"))
            for name in ("coverage_fields", "elapsed_spans", "linkage")
        }
        attempts = [dict(row) for row in conn.execute(
            "SELECT id, role, profile, driver, harness, result, started_at, ended_at, "
            "session_ref, run_id, usage_missing_reason, task_id FROM attempts ORDER BY id"
        )]
        observations = {
            row["attempt_id"]: row["n"]
            for row in conn.execute(
                "SELECT attempt_id, COUNT(*) AS n FROM usage_observations GROUP BY attempt_id"
            )
        }
        host_reports = [dict(row) for row in conn.execute(
            "SELECT u.id, u.attempt_id, u.input_tokens, u.output_tokens, "
            "u.cached_input_tokens, u.accuracy, u.source, u.run_id, a.driver, a.role, "
            "a.session_ref, a.profile FROM usage_observations u "
            "JOIN attempts a ON a.id = u.attempt_id "
            "WHERE u.source = 'host_report' ORDER BY u.id"
        )]
        runs = [dict(row) for row in conn.execute(
            "SELECT id, goal_id, status, created_at, updated_at, started_at, completed_at "
            "FROM runs ORDER BY id"
        )]
    finally:
        conn.close()

    first_r002 = min((row["id"] for row in attempts if row["run_id"] == "R002"), default=None)
    cohorts = {}
    for label, chosen in (
        ("cli_run_id_R001", [a for a in attempts if a["driver"] == "cli" and a["run_id"] == "R001"]),
        ("cli_run_id_R002", [a for a in attempts if a["driver"] == "cli" and a["run_id"] == "R002"]),
        ("cli_run_id_null", [a for a in attempts if a["driver"] == "cli" and a["run_id"] is None]),
        ("cli_id_before_first_R002_attempt", [
            a for a in attempts
            if a["driver"] == "cli" and first_r002 is not None and a["id"] < first_r002
        ]),
        ("host_run_id_R001", [a for a in attempts if a["driver"] == "host" and a["run_id"] == "R001"]),
        ("host_run_id_R002", [a for a in attempts if a["driver"] == "host" and a["run_id"] == "R002"]),
        ("host_run_id_null", [a for a in attempts if a["driver"] == "host" and a["run_id"] is None]),
    ):
        cohorts[label] = _cohort(chosen, observations)

    replay = None
    replay_cmd = None
    if host_reports:
        latest = host_reports[-1]
        cmd = [
            "uv", "run", "orx", "usage", "record", "--json",
            "--attempt", str(latest["attempt_id"]),
            "--input", str(latest["input_tokens"]),
            "--output", str(latest["output_tokens"]),
            "--accuracy", str(latest["accuracy"]),
        ]
        if latest["cached_input_tokens"] is not None:
            cmd.extend(["--cached", str(latest["cached_input_tokens"])])
        env = os.environ.copy()
        env.pop("ORX_CONFIG_DIR", None)
        env.pop("ORX_DATA_DIR", None)
        env["ORX_PROJECT"] = str(ROOT)
        replay_cmd = cmd
        replay = subprocess.run(
            cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    after = LIVE_DB.stat().st_mtime_ns
    analytics = Path.home() / "Sources" / "Tools" / "orx-analytics"
    if analytics.is_dir():
        names = sorted(p.name for p in analytics.iterdir() if not p.name.startswith("."))
        analytics_note = (
            f"`{analytics}` is present. Entries: {names or '(empty)'}. "
            "No entry point is named by the M1.2 contract, so this pass did not "
            "guess a command. The six readings below are the contract SQL."
        )
    else:
        analytics_note = (
            f"`{analytics}` is not present. The six readings below are the "
            "contract SQL from docs/observability-contract.md, executed against "
            "a backup-API snapshot of .orx/state.db."
        )

    cutoff = datetime.now(timezone.utc).isoformat()
    r002 = next(row for row in runs if row["id"] == "R002")
    sections = [
        "# M1.2 acceptance report (R002)",
        "",
        "Status: measured during T005. This file does not claim that G002 or R002",
        "is done. `runs.completed_at` for R002 is pending until the Controller",
        "closes the run through the normal verify path. Nothing in this measurement",
        "forced a status, backfilled a legacy row, or invented a token count.",
        "",
        "## Measurement cutoff",
        "",
        f"- Cutoff (UTC, taken after the snapshot and the queries): `{cutoff}`",
        f"- Database: `.orx/state.db` schema_version `{schema_version}`",
        "- Read path: `mode=ro` plus `PRAGMA query_only`, then `Connection.backup`",
        "  into a private snapshot. The queries ran on the snapshot.",
        "  `Store.open` was not used for the queries, so they did not migrate.",
        f"- Main file mtime_ns after the read-only snapshot `{before}`, after the",
        f"  optional usage-record replay `{after}`. The contract queries ran on",
        "  the snapshot, not on this file.",
        "- Bound `:project_id` for the contract examples: `ORX`.",
        "- The pytest process that rewrites this file is still running, so this",
        "  file does not contain that process's exit code. The Controller's",
        "  verification log for `uv run pytest -q` is that exit code.",
        "",
        "## Commands",
        "",
        "```text",
        "snapshot: sqlite3 mode=ro backup of .orx/state.db",
        "exit: 0 (backup and schema_gate == ok; a failure would have failed this test before the write)",
        "```",
        "",
    ]
    if replay is None:
        sections.extend([
            "No `host_report` row was stored, so this pass did not call `orx usage record`.",
            "A missing host measurement stays unknown. It was not replaced with a fixture or zero.",
            "",
        ])
    else:
        shown = " ".join(replay_cmd)
        sections.extend([
            "Idempotent replay of the latest stored `host_report`. The counts are the",
            "row already in the database, not a new measurement and not a fixture.",
            "",
            "```text",
            f"$ {shown}",
            f"[exit {replay.returncode}]",
            replay.stdout.rstrip(),
            replay.stderr.rstrip(),
            "```",
            "",
        ])
    sections.extend([
        "## Schema gate",
        "",
        "```json",
        gate,
        "```",
        "",
        "## Run lifecycle (pending close is honest)",
        "",
        "```json",
        _dump(runs),
        "```",
        "",
        f"R002 status is `{r002['status']}`. `started_at` is `{r002['started_at']}`.",
        f"`completed_at` is `{r002['completed_at']}`. While that value is null, user",
        "waiting for R002 is unknown. `updated_at` is not a completion time.",
        "",
        "## External analytics",
        "",
        analytics_note,
        "",
        "## Six readings (verbatim snapshot output)",
        "",
    ])
    for name in SIX:
        sections.extend([f"### {name}", "", "```json", six[name], "```", ""])
    sections.extend([
        "### coverage_fields",
        "",
        "```json",
        extra["coverage_fields"],
        "```",
        "",
        "## Coverage cohorts",
        "",
        "The R001 baseline in docs/m1.2-plan.md is unchanged: CLI token coverage",
        "**12/37**, verifier **10/27**, worker **1/6**, planner **1/4**. Those",
        "denominators are not rewritten to match a later cohort. Each cohort below",
        "is counted separately. `observed + missing_recorded + unassessed + pending`",
        "is the attempt count. A pending attempt is not a completed miss. A",
        "completed attempt with no observation and a null `usage_missing_reason`",
        "is an early legacy gap (migration does not backfill a reason), not a",
        "current-path success.",
        "",
        f"`first attempt id with attempts.run_id = R002`: `{first_r002}`.",
        "CLI planners often keep `attempts.run_id` NULL because the attempt is",
        "opened before a revision exists and the CLI path has no assignment.",
        "That NULL stays unknown. It is not folded into R001 or R002.",
        "",
    ])
    for label, bucket in cohorts.items():
        sections.append(f"### {label}")
        sections.append("")
        sections.append(
            f"attempts {bucket['attempts']}, observed {bucket['observed']}, "
            f"session_present {bucket['session_present']}, "
            f"session_absent {bucket['session_absent']}"
        )
        sections.append("")
        sections.append("```json")
        sections.append(json.dumps(bucket["by_role"], indent=2, sort_keys=True))
        sections.append("```")
        sections.append("")
        sections.append("Completed misses with a recorded reason:")
        sections.append("")
        misses = bucket["completed_miss_recorded"]
        sections.extend([_attempt_line(row) for row in misses] or ["- none", ""])
        if misses:
            sections.append("")
        sections.append("Early legacy gaps (completed, no observation, reason NULL):")
        sections.append("")
        gaps = bucket["legacy_unassessed"]
        sections.extend([_attempt_line(row) for row in gaps] or ["- none", ""])
        if gaps:
            sections.append("")
        sections.append("Pending (not completed; excluded from miss totals):")
        sections.append("")
        pending = bucket["pending"]
        sections.extend([_attempt_line(row) for row in pending] or ["- none", ""])
        if pending:
            sections.append("")
        sections.append("Completed spans missing started_at or ended_at:")
        sections.append("")
        spans = bucket["span_incomplete"]
        sections.extend([_attempt_line(row) for row in spans] or ["- none", ""])
        sections.append("")
    sections.extend([
        "## Stored host_report rows",
        "",
        "These are the rows the database already held at the cutoff. This pass",
        "did not insert a new observation. Host attempts with no observation and",
        "a null reason are unassessed, not zero.",
        "",
        "```json",
        json.dumps(host_reports, indent=2),
        "```",
        "",
        "## Controller post-close procedure",
        "",
        "T005 verification runs while R002 is still `running` and `completed_at`",
        "is null. The milestone is not accepted from this file alone. After the",
        "command checks and the agent verdict, the Controller:",
        "",
        "1. Submits this task's evidence and the verifier verdict with the normal",
        "   `orx task complete` / `orx verify submit` commands. Do not update",
        "   `runs.status` or `completed_at` by hand.",
        "2. Runs `uv run orx status --json` and `uv run orx usage --json`.",
        "3. Confirms the goal status is `done`, the run id is `R002`, the run",
        "   status is `done`, and `completed_at` is a real timestamp from that",
        "   status command. If `completed_at` is still null, stop. Do not copy",
        "   `updated_at` into the report.",
        "4. Appends that command transcript under the heading below, including",
        "   both exit codes and the JSON bodies. Put the line `POST_CLOSE_CAPTURED`",
        "   in that section so a later pytest rewrite keeps the transcript.",
        "5. Re-reads every item in the M1.2 acceptance checklist in",
        "   `docs/m1.2-plan.md`, including the items this measurement left",
        "   pending (run `completed_at`, and removal of the staging section).",
        "6. Removes only the `## G002 — M1.2 Observability（staging）` section",
        "   from `IMPLEMENTATION_PLAN.md`. Leaves the G001 history and the",
        "   recovery notes in place.",
        "7. Then, and only then, makes the milestone commit.",
        "",
        "## Post-close transcript",
        "",
        "PENDING. Append the real `done` / `completed_at` output here. Do not",
        "fill this section in before the Controller has observed it.",
        "",
    ])
    if REPORT.exists():
        previous = REPORT.read_text()
        marker = "## Post-close transcript"
        if marker in previous:
            tail = previous.split(marker, 1)[1]
            if "POST_CLOSE_CAPTURED" in tail:
                sections = sections[: sections.index("## Post-close transcript") + 1]
                sections.extend(["", tail.strip(), ""])
    text = "\n".join(sections)
    if not text.endswith("\n"):
        text += "\n"
    REPORT.write_text(text)
    assert REPORT.stat().st_size > 0
    assert "12/37" in text
    assert "Post-close transcript" in text
    # Pre-close the report says PENDING; once the Controller has observed
    # the real done/completed_at, the preserved transcript carries the
    # POST_CLOSE_CAPTURED marker instead (procedure step 4).
    assert r002["completed_at"] is None or "PENDING" in text or "POST_CLOSE_CAPTURED" in text
    if replay is not None:
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout)["idempotent"] is True
