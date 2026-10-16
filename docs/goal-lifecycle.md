# Goal lifecycle: pause, resume, and the startup gate (G010)

Status: operating contract for `orx goal pause` / `orx goal resume`
(2026-10-16, schema v13). This document is the authoritative table of what
each lifecycle operation does, which entry points the startup gate covers,
and what a pause never does. The task-level state machine
(`src/orx/machine.py`) remains the single status authority; this document
explains the Goal-level extension layered on top of it.

## The Goal state machine

```
active ──pause──> paused ──resume──> active
   │                                     (idempotent repeats are legal
   └── (completion invariant) ──> done      no-ops with changed: false)
```

| Transition | Legality | Idempotent repeat |
|---|---|---|
| `active -> paused` (`orx goal pause`) | legal | pausing a paused Goal is a no-op: `changed: false`, exit 0 |
| `paused -> active` (`orx goal resume`) | legal | resuming an active Goal is a no-op: `changed: false`, exit 0 |
| `paused -> done` | illegal — completion converges only through `resume` + the existing invariant | — |
| `done -> anything`, `cancelled -> anything` | illegal (terminal; a domain error, exit 1) | — |

`paused` is Goal state, not run state. `runs.status` keeps its
task-derived meaning (`planning`/`running`/`blocked`/`done`) and task
statuses are unchanged by a pause: a paused Goal with mid-flight tasks
still shows a `running` run, because those tasks ARE still running.

## What a pause does

1. **Persists in the database** (`goals.status = 'paused'`, schema v13).
   A new process, a restarted Controller, and a crashed session all see
   the same paused Goal. `orx goal new` and inbox acceptance refuse while
   a paused Goal exists — a paused Goal is unfinished, and ORX keeps one
   unfinished Goal per project (no second unfinished Goal can be created).
2. **Gates every new executor start** (the startup gate, below).
3. **Leaves in-flight work alone** (see "What a pause never does").
4. **Is never lifted implicitly.** No `refresh` flips a paused Goal: the
   completion write is a compare-and-set on `active -> done`, decided by
   the database row (a stale in-memory Goal object cannot lift the pause).
   If every task completes during the pause, the Run records `done` and
   the Goal stays paused; `orx goal resume` replays the same invariant and
   the Goal converges to `done` right there.

## The startup gate (one rule, every entry point)

While the Goal is paused, ORX refuses to create or start any new
executor. The gate is read inside the same write transaction that creates
the executor placeholder (attempt row, assignment row, or task
transition), so a `goal pause` committing between the check and the write
is still refused — there is no window. All refusals raise
`GoalPausedError` ("goal ... is paused; run `orx goal resume` before
starting new work") and exit 1.

| Entry point | Gated where | While paused |
|---|---|---|
| planner / replan routing (`orx plan`, `orx replan`) | clean read + inside the assignment/attempt transaction | no planning assignment is created or refreshed; no planner attempt opens |
| plan activation (`orx plan submit`, CLI planner auto-submit) | clean read + inside the activation transaction | no revision lands; the previous revision, tasks, and the waiting assignment are untouched; a completed Run keeps its completion |
| worker dispatch (`orx run`: host/external parks, CLI launches) | clean read at the slice top + inside each park/launch placeholder transaction | no task is parked or launched; runnable tasks stay runnable |
| parked assignment claim (`orx task claim`) | inside the claim transaction | the task stays `waiting_host` with its prompt file and nonce; claimable again after resume |
| verifier dispatch (`orx verify dispatch`) | clean read + inside the verifier-attempt transaction | no verifier attempt opens; tasks in `verifying` keep their dispatched verifiers |
| downgrade startup (resource-failure reroute to the next rung) | through the same park/launch transactions the reroute lands in | the failed task is left runnable with its recorded history; the next dispatch happens after resume |
| `orx goal new` / inbox acceptance | unfinished-Goal check | refused while any paused Goal exists |

## What a pause never does

- **No preemption, kill, or rebind.** Attempts, spans, nonces, session
  bindings, and evidence stay exactly as recorded. A pause writes one
  row: `goals.status`.
- **No starvation of running executors.** Fact submissions from an
  already-running executor stay legal while paused: `orx task heartbeat`,
  `orx task complete`, `orx task fail`, `orx task recover`, `orx task
  note`, `orx task observe`, `orx task check`, and `orx verify submit`
  (a verdict closes exactly the attempt it was dispatched for). The
  delivery gate runs as usual inside those completions.
- **No second executor on resume.** `orx goal resume` starts NOTHING. It
  transitions the Goal, replays the completion refresh (converging a
  fully-completed run to `done`), and returns the in-flight ownership
  snapshot: run, revision, the open planning assignment, parked tasks,
  running host workers, and dispatched verifier attempts — each with the
  exact attempt id and nonce that own it, plus recovery guidance for the
  running ones (the same no-second-writer contract `orx run`'s recovery
  surface carries). The next dispatch is the operator's explicit
  `orx run`.

## Interfaces

Both commands have text and `--json` forms. Repeats are explicit
idempotent results (exit 0, `changed: false`), never errors.

```
$ orx goal pause --json
{"ok": true, "goal": "G001", "status": "paused", "changed": true,
 "run": {...}, "in_flight": {"planning": [...], "parked": [...],
 "running": [...], "verifying": [...]}}

$ orx goal resume --json
{"ok": true, "goal": "G001", "status": "active", "changed": true,
 "run": {...}, "plan": {...}, "in_flight": {...}, "next": "orx run"}
```

`in_flight.running[].contract` is the recovery guidance for that
executor; `in_flight.parked[].resume_with` / `in_flight.verifying[].resume_with`
name the exact next command for that item. If the Goal converged to
`done` at resume (everything completed during the pause), `status`
reports `done` honestly.

## Concurrency contract

- **Pause vs startup placeholder:** the gate read and the placeholder
  write share one `BEGIN IMMEDIATE` transaction. Either the placeholder
  fully commits before the pause (it then stays parked/owned, simply not
  claimable until resume) or the start is refused with nothing written —
  never half of each.
- **Concurrent claims:** unchanged compare-and-set; exactly one winner,
  the loser gets a conflict, one claim event.
- **Repeated resume:** two simultaneous resumes serialize on the write
  lock; the Goal flips `paused -> active` exactly once and the second
  call reports the idempotent no-op.
- **No write transaction waits across a process or a barrier.** File
  writes (prompt files) happen after their transaction; every gated
  transaction is short. Tests demonstrate this with independent sqlite
  connections and deterministic barriers (`tests/test_lifecycle.py`).

## Known follow-ups (out of this round)

- Cancel and safe wrap-up (cancel with in-flight executor disposition) is
  the other half of G010 and is not implemented here; `cancelled` remains
  reachable only through the pre-existing paths.
- `orx sources fetch --auto-accept` while a Goal is paused leaves the
  fetched items pending and exits loudly (the auto-accept calls
  `create_goal`, which refuses); accepting them after `orx goal resume`
  is the supported path.
- Analytics stays read-only over these states (observability contract,
  v13 amendment); it never gains scheduling authority.
