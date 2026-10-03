# M1 P3 checkpoint: timeline — 2026-10-03

Phase gate deliverable (revision 6 of Run R001, tasks T001–T002).

## Shipped

- **`orx timeline`** implemented by the cursor-strong CLI worker (grok-4.7,
  one attempt, ~15 min, first-try pass): `dispatch.timeline()` merges the
  six existing sources (task_events, attempts — including planner/verifier
  rows, verifications, planning_assignments, routing_decisions, goal/run
  rows) into strictly time-ordered `{ts, actor, event, detail}` entries.
  Filters `--run` (default active context) / `--task` / `--profile` /
  `--limit` (default 50); human `HH:MM:SS  actor  event  detail` + the M0
  JSON envelope. Pure read model — no new tables.

## Real evidence (this project's own history)

- `orx timeline --run R001 --limit 200 --json` → 200 entries,
  `strictly ordered: True` (checklist item 4: PASS)
- human excerpt (see transcript): route → attempt.start → complete →
  verify.pass → deps_satisfied → claim, with cursor-economy/orx-host actors.

## Findings recorded in-phase

- Dogfood finding #5 confirmed systemic: the second codex-frontier replan
  again drafted "P1 hardening" (the frozen Goal context says
  "Current phase: P1"; repo checkpoints did not outweigh it). Phase
  revisions are therefore host-authored from P3 on; codex planning returns
  when `replan` grows a context override (M2 backlog).
- The codex replan exec log's JSONL was stream-truncated before
  turn.completed (long exploration output), so its usage line is missing
  from the ledger — noted as P5 motivation (parse usage before truncation
  / raise stream limit for launches).

## Test commands and results

- `uv run pytest -q` → **263 passed** (P2: 261; +2 timeline tests)

## M1 checklist status after P3

Items 2, 3, 4 **passed**. Item 1 partial (263 ≥ 320 target at P7).
Items 5–7 pending (P4–P6); item 8 at P7.
