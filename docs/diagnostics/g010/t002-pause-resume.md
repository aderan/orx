# G010 T002 diagnostics: persistent Goal pause/resume + startup gate

Date: 2026-10-16. Scope of record: the design facts, race reasoning, and
the dogfood-isolation change this task made. The user-file baseline
(`user-file-baseline.json`, T001) kept protecting `docs/m1.2-report.md`
throughout; this round's changes never touch it.

## Why schema v13 (and why the goals rebuild differs from v2/v7/v12)

`goals.status` needed to admit `paused`. The CHECK cannot be altered in
place, so the table is rebuilt with the v2/v7/v12 backup-replace pattern
inside the copy-then-replace migration — with one structural difference:
`goals` is a PARENT table (`runs.goal_id` references it). Dropping a
parent with foreign keys ON fails on every existing database
(`FOREIGN KEY constraint failed` from the implicit DELETE before DROP),
verified experimentally before writing the migration. The migration
therefore toggles `PRAGMA foreign_keys=OFF` around the swap: on the
upgrade path (autocommit) the pragma takes effect; on the fresh path the
surrounding transaction makes it a no-op, which is safe there because the
copied-out table is empty. By the end of the script every original goal
row is back and `PRAGMA foreign_key_check` is clean (asserted in
`tests/test_state.py::test_v12_to_v13_migration_admits_paused_without_backfill`).

## Why the completion write is a CAS

The pre-existing refresh wrote `done` when the caller's Goal object said
`active`. That object can be a stale snapshot: a `goal pause` committing
between the caller's read and the refresh would be silently overwritten
by a completion — exactly the "pause lifted implicitly" failure G010
exists to prevent. `Store.goal_complete_if_active` makes the completion a
compare-and-set on `active -> done` decided by the row: a paused row is
never touched, and a fully-completed run converges the Goal at `goal
resume` through the same invariant (`machine.refresh_run` +
`machine.goal_resume`).

## Where the gate lives (race reasoning)

A clean read at each entry point gives the operator a fast, clear error;
the race-safety comes from re-reading the gate INSIDE the same
`BEGIN IMMEDIATE` transaction that writes the executor placeholder:

- plan_route host branch: assignment row + prompt refresh + planner
  attempt + routing audit, one transaction; the prompt FILE is written
  after it (a refused park leaves no stale file).
- `_park_task`: attempt + routing audit + waiting_host/external
  transition, one transaction; file after.
- `_execute_cli_task`: attempt + routing audit + route_cli transition,
  one transaction, then the launch.
- `machine.claim`: the gate read and the waiting_host -> running
  compare-and-set share the claim transaction.
- `submit_plan`: the gate re-read is the first statement of the
  activation transaction; a pause after the precheck rolls the whole
  activation back.
- verifier dispatch: open-attempt reuse and creation share one
  transaction; `_run_cli_verifier` re-reads before its launch.
- the downgrade reroute lands in `_park_task`/`_execute_cli_task`, so it
  inherits their gates.

No gated transaction performs file I/O or waits on anything external
while holding the write lock; that is the "write transactions never wait
across subprocesses" property, demonstrated (not just asserted) by
`tests/test_lifecycle.py::test_pause_racing_a_park_is_race_safe` — the
park thread parks at a barrier BEFORE its transaction (minted nonce in
hand), an independent connection pauses, and the park's in-transaction
gate then refuses with nothing written.

## What the pause deliberately does not do

No kill/preempt/rebind of running executors; fact submissions (heartbeat,
complete, fail, recover, note, observe, check, verify verdicts) stay
legal while paused; parked work stays parked and owned. `goal resume`
starts nothing — it returns the in-flight ownership (run, revision,
assignment, attempts, nonces) with recovery guidance, and the next
dispatch is the operator's `orx run`. Cancel/safe-wrap-up is the OTHER
half of G010 and is not implemented in this round.

Known consumer edge, disclosed rather than hidden: `orx sources fetch
--auto-accept` with a paused Goal leaves the fetched items pending and
exits 1 (its auto-accept calls `create_goal`, which now refuses on a
paused Goal). `src/orx/sources.py` is outside this task's write scope;
the loud refusal is the safe behavior (no second unfinished Goal), and
`docs/goal-lifecycle.md` documents the supported path (resume, then
accept). The same reasoning covers the one-line consumer migration in
`tests/test_package_acceptance.py` (`upgraded_schema_version` 12 -> 13):
the scope list omitted that mechanical schema assertion, and leaving the
full suite red was worse than the disclosed minimal fix.

## Dogfood isolation (why the m12 test no longer writes the workspace)

T001's full-suite run had regenerated `docs/m1.2-report.md` from inside
pytest (recorded in the T001 baseline postscript) and its usage replay
ran `orx usage record` against the REAL `.orx/state.db` (idempotent, but
a write to the execution library nonetheless). G010 T002 removes both:

- the report is written to pytest's `tmp_path`; the user's
  `docs/m1.2-report.md` is never read or rewritten (the post-close
  transcript preservation that read/rewrote it is gone with the write);
- the idempotent replay runs `python -m orx usage record` against a
  private backup-API snapshot of the database inside a throwaway project
  under `tmp_path` (with its own minimal config and isolated user-layer
  env), so the real database is never opened for writing or migrated by
  the test;
- `tests/test_m12_acceptance.py::test_dogfood_writes_only_temporary_artifacts`
  re-runs the dogfood test in a real subprocess (its own pytest tmp,
  deterministic process completion as the sync point) and proves
  byte-for-byte that `docs/m1.2-report.md`, `.orx/state.db`, and its
  WAL/SHM sidecars are unchanged, while the report and the replay
  snapshot really landed under the temporary tree.

## Verification

- `tests/test_machine.py`: goal transition table, idempotent repeats,
  terminal refusal, refresh-never-lifts (incl. the stale-object case).
- `tests/test_goal.py`: pause persistence across reopen, one-unfinished-
  Goal invariant, CLI text/JSON interfaces with idempotent results,
  in-flight payload with verifier nonce.
- `tests/test_lifecycle.py`: the gate matrix (planner/replan, activation,
  worker dispatch, parked claim, verifier dispatch, downgrade fallback)
  with running-executor facts still landing and identity untouched;
  parked-work survival + same-attempt resume; completion-during-pause
  convergence at resume; new-process identity preservation with recovery
  guidance; real cross-process pause/resume; the barrier race; both
  deterministic orderings; concurrent claims; concurrent resumes.
- `tests/test_state.py`: v12 -> v13 migration (identity/status/history/
  usage preserved, foreign key check clean, paused insertable, nothing
  backfilled), migration failure leaves the original usable, backup copy
  still migrates.
- `tests/test_observability_contract.py` + this repo's contract doc:
  schema gate whitelist now `8`..`13` (v14 refused).
- Full suite green (1092 passed) including the packaged-upgrade consumer.
