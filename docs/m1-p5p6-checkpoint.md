# M1 P5+P6 checkpoint: usage observation + inbox — 2026-10-03

Phase gate deliverable (revision 8 of Run R001, tasks T001–T003). P5 ran
the main dogfood chain; P6 was the approved host-fanout worktree (branch
p6-inbox, merged here with its migration renumbered v3->v4).

## P5 shipped

- Schema v3 `usage_observations` (attempt FK, nullable token columns,
  source native_cli|output_estimate, accuracy exact|estimated|unknown —
  unknown is a legal stored outcome).
- Adapters: codex parses `turn.completed.usage` (fixture-backed), cursor
  parses the envelope's camelCase usage; both None when absent. dispatch
  records rows for planner/worker/verifier attempts automatically —
  recording is LIVE (this phase's own calls already carry exact rows).
- `runtime.run_launch` captures 2 MiB so long planner streams keep their
  final turn.completed (the P3 truncation finding).
- `orx usage [--profile] [--json]` implemented by the cursor-strong worker
  (grok-4.7, first-try pass): tasks, runtime (attempt spans), token sums
  with accuracy labels; `-`/null for unobserved.

## P6 shipped (host-fanout worktree)

- Schema v4: `external_events` (UNIQUE(source, external_id) dedupe) +
  `inbox_items` (pending/accepted/rejected/dismissed, goal_id link).
- `src/orx/sources.py`: gh-delegated GitHub source (`gh issue list --json`,
  ORX stores no tokens), `watch_once` (dedupe + policy), `accept_item`
  (Goal via the standard create path; the one-active-Goal error propagates).
- `[inbox]` policy config (github_labels, auto_accept=false default) with
  layered origins.
- CLI: `orx inbox list/show/accept/reject/dismiss`, `orx watch [--once]
  [--interval]`, `orx auth status` (display-only gh delegation, works
  outside projects).

## Live evidence

- `orx auth status` -> "gh auth: ✓ Logged in to github.com account aderan
  (keyring)"; `orx watch --once` -> real fetch (0 issues: this repo has no
  GitHub remote — honest live result; fake-gh e2e tests cover the pipeline).
- Synthetic event -> `orx inbox list` renders the source row;
  `orx inbox accept 1` correctly REFUSED while Goal G001 is active
  (one-active-Goal invariant, loud error); `orx inbox reject 1` decided it.
- usage rows recording live: cursor-strong worker 119764/21002 in/out
  (exact), cursor-economy audits 32656/6224 (exact).
- Host-maintained ledger `.orx/runs/R001/usage-ledger.jsonl` now
  cross-checks the automatic rows.

## Merge record

- Conflicts: state.py migrations (usage kept v3, inbox renumbered v4),
  cli.py (usage + inbox sub-apps both kept; usage command tail
  reconstructed), tests (schema tables/versions 2->3->4). Full regression:
  **298 passed** (P4: 275; +23 across P5/P6).

## M1 checklist status after P5+P6

Items 2–7 **passed**. Item 1 partial (298 of 320+ target). P7 (completion,
help audit, reference-radar, m1-report, final regression) remains.
