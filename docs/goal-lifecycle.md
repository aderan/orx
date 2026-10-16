# Goal lifecycle: pause, resume, cancel (G010)

Status: operating contract for `orx goal pause` / `orx goal resume`
(2026-10-16, schema v13) and `orx goal cancel` (2026-10-16, schema v14).
This document is the authoritative table of what each lifecycle operation
does, which entry points the startup gate covers, and what a pause or a
cancel never does. The task-level state machine (`src/orx/machine.py`)
remains the single status authority; this document explains the Goal-level
extension layered on top of it.

## The Goal state machine

```
active ──pause──> paused ──resume──> active
   │                │                   (idempotent repeats are legal
   │                └──cancel──> cancelling     no-ops with changed: false)
   └──cancel──> cancelling ──converge──> cancelled
   └── (completion invariant) ──> done        (fires only while ACTIVE)
```

| Transition | Legality | Idempotent repeat |
|---|---|---|
| `active -> paused` (`orx goal pause`) | legal | pausing a paused Goal is a no-op: `changed: false`, exit 0 |
| `paused -> active` (`orx goal resume`) | legal | resuming an active Goal is a no-op: `changed: false`, exit 0 |
| `paused -> done` | illegal — completion converges only through `resume` + the existing invariant | — |
| `active\|paused -> cancelling` (`orx goal cancel`) | legal from every unfinished state (planning, executing, blocked, paused) | cancelling a cancelling/cancelled Goal is a no-op: `changed: false`, exit 0; the wrap-up is never re-run |
| `cancelling -> cancelled` (convergence) | legal only when no unconfirmed execution remains; compare-and-set | the CAS fires once; a repeated call reports the converged state |
| `cancelling -> active` | illegal — a cancel cannot be un-asked; it converges, it does not resume | — |
| `cancelling -> done` | illegal — the cancelling row blocks the completion CAS; a racing completion keeps its facts and never aggregates the Goal as done | — |
| `done -> anything`, `cancelled -> anything` | illegal (terminal; a domain error, exit 1) | — |

`paused` and `cancelling` are Goal states, not run states. `runs.status`
keeps its task-derived meaning (`planning`/`running`/`blocked`/`done`) and
task statuses are unchanged by a pause: a paused Goal with mid-flight tasks
still shows a `running` run, because those tasks ARE still running. A
cancel DOES move tasks — the unstarted ones (below) — through legal
task-machine transitions with recorded events.

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

## What a cancel does (the two-phase wrap-up)

Cancel is a REQUEST followed by a CONVERGENCE — never a jump:

1. **The request** (`orx goal cancel`): `active|paused -> cancelling`,
   persisted (schema v14), idempotent on repeat. From that commit:
   - every new executor start is refused (the startup gate below);
   - the completion CAS (`active -> done`) can never fire — a completion
     racing the cancel records its real delivery and results but the Goal
     can only end `cancelled`. Exactly one legal wrap-up result exists:
     if the Goal completed first, a late cancel is a domain error
     (`cannot cancel from done`, exit 1).
2. **The sweep** (idempotent, re-run by every closing fact): unstarted
   work is cancelled EXPLICITLY, each through a legal task-machine
   transition with a recorded `goal_cancel` event:
   - `pending` / `runnable` / `blocked` tasks;
   - parked `waiting_host` / `waiting_external` assignments (their
     placeholder attempts close with `result='cancelled'`; `started_at`
     is never synthesized — unclaimed stays NULL);
   - `verifying` tasks with NO dispatched verifier (there is no execution
     to wait for and no follow-up verification may start);
   - a waiting planning assignment and its open planner attempt (no claim
     protocol exists — the assignment IS the unstarted artifact; a late
     `orx plan submit` is refused by the gate with a clear error).
3. **The convergence** (`cancelling -> cancelled`, compare-and-set): fires
   only when NO unconfirmed execution remains — no RUNNING task and no
   dispatched, still-open verifier attempt. The convergence re-checks
   after every closing fact, so the wrap-up finishes even when the cancel
   call returned with work outstanding.

### What the wrap-up waits for (and never guesses)

Running worker, planner, and verifier executions are NEVER concluded from
silence. Lost contact, overdue heartbeats, a dead-looking session — all
observations, none a verdict (the G009 discipline, unchanged). They close
only on REAL facts, each of which converges the Goal when it is the last:

- a late `orx task complete` (the real delivery, evidence, and results are
  preserved verbatim; the task keeps `passed` — only the Goal ends
  cancelled);
- a verifier verdict (`orx verify submit`) closing exactly the dispatched
  attempt;
- an explicit failure (`orx task fail`) — recorded as the real fact it is;
- `orx task recover --termination-fact` — the explicit fact entry: during
  a cancel it closes the confirmed-terminated execution WITHOUT re-dispatch
  (the task lands `cancelled`, not runnable; the operator's fact and the
  machine-read facts are recorded next to each other).

Until then the Goal stays `cancelling`, the one unfinished-Goal slot stays
TAKEN (a cancelling Goal blocks `orx goal new` exactly like a paused one),
and the cancel output plus `orx status` list every unconfirmed execution
with its executable follow-ups (observe / deliver / verdict / recover).

### The manageable CLI stop

A CLI executor launched by THIS project's own `orx run` / `orx plan` /
`orx verify dispatch` process is the one execution ORX can stop itself:

- the launch carries a read-only `stop_check` (the persisted goal status),
  polled while the process waits;
- when a cancel is requested — from ANY process; a separate `orx goal
  cancel` during the execution wait is the designed path — the runtime
  puts the child in its own session at spawn, then sends SIGTERM to
  exactly that process group (the launched process and its controlled
  children; never any other process), escalating to SIGKILL after a grace
  window;
- it ALWAYS waits for the actual exit, and the recorded facts are the
  stop request, the signal, and the observed exit code on the attempt
  (`result='cancelled'`) plus the task's `goal_cancel_stop` transition;
- usage, session, and the execution log are recorded in every case — the
  history stays complete.

## The startup gate (one rule, every entry point)

While the Goal is paused OR cancelling/cancelled, ORX refuses to create or
start any new executor. The gate is read inside the same write transaction
that creates the executor placeholder (attempt row, assignment row, or
task transition), so a `goal pause` or `goal cancel` committing between
the check and the write is still refused — there is no window. Refusals
raise `GoalPausedError` ("run `orx goal resume` before starting new work")
or `GoalCancellingError` ("a cancel was requested and no new work starts
for this Goal") and exit 1.

| Entry point | Gated where | While paused / cancelling |
|---|---|---|
| planner / replan routing (`orx plan`, `orx replan`) | clean read + inside the assignment/attempt transaction | no planning assignment is created or refreshed; no planner attempt opens |
| plan activation (`orx plan submit`, CLI planner auto-submit) | clean read + inside the activation transaction | no revision lands; the previous revision, tasks, and the waiting assignment are untouched; a completed Run keeps its completion |
| worker dispatch (`orx run`: host/external parks, CLI launches) | clean read at the slice top + inside each park/launch placeholder transaction | no task is parked or launched; runnable tasks stay runnable |
| parked assignment claim (`orx task claim`) | inside the claim transaction | the task stays `waiting_host` with its prompt file and nonce |
| verifier dispatch (`orx verify dispatch`) | clean read + inside the verifier-attempt transaction | no verifier attempt opens; tasks in `verifying` keep their dispatched verifiers |
| downgrade startup (resource-failure reroute to the next rung) | through the same park/launch transactions the reroute lands in | the failed task is left runnable with its recorded history; the next dispatch happens after resume |
| suppression re-verification (`orx task reverify`) | clean read before any record | no lift is recorded: the re-verification itself never starts a worker, and its lift changes dispatch eligibility — a paused or cancelling/cancelled Goal is not the moment to re-open a suppressed (task, profile); the recorded blocker facts are untouched |
| `orx goal new` / inbox acceptance | unfinished-Goal check | refused while any paused OR cancelling Goal exists |

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

## What a cancel never does

- **Never concludes a running execution from silence.** No heartbeat
  timeout, session gap, or wall clock moves a task to `cancelled`; only
  the real facts above close an unconfirmed execution.
- **Never destroys history.** Evidence rows, verification rows, usage
  observations, events, progress reports, and attempt results survive the
  cancel verbatim; late writes are refused (stale attempt / task-not-
  running), never silently recorded over the terminal state; queries by
  Goal/Run (`orx status`, `orx timeline --run`, `orx usage`, and every
  store read) keep answering for a cancelled Goal, and a new Goal does
  not disturb the old Goal's history.
- **Never re-dispatches.** No follow-up verification is dispatched for a
  cancelling Goal, no retry loop starts, and `orx task recover` during a
  cancel closes without routing anything.
- **Never aggregates to success.** A Goal that was cancelling when its
  last task completed ends `cancelled`; `done` is reachable only through
  the completion invariant while still `active`.

## Interfaces

`pause`, `resume`, and `cancel` all have text and `--json` forms. Repeats
are explicit idempotent results (exit 0, `changed: false`), never errors.

```
$ orx goal pause --json
{"ok": true, "goal": "G001", "status": "paused", "changed": true,
 "run": {...}, "in_flight": {"planning": [...], "parked": [...],
 "running": [...], "verifying": [...]}}

$ orx goal resume --json
{"ok": true, "goal": "G001", "status": "active", "changed": true,
 "run": {...}, "plan": {...}, "in_flight": {...}, "next": "orx run"}

$ orx goal cancel --json
{"ok": true, "goal": "G001", "status": "cancelling|cancelled",
 "changed": true, "run": {...},
 "cancelled": {"assignments": [...], "tasks": [...], "attempts": [...]},
 "unconfirmed": [{"task": "T001", "kind": "worker|verifier", "attempt": 7,
                  "session_ref": "...", "next": ["orx task observe T001", ...]}],
 "converged": false, "next": "..."}
```

`in_flight.running[].contract` is the recovery guidance for that
executor; `in_flight.parked[].resume_with` / `in_flight.verifying[].resume_with`
name the exact next command for that item. If the Goal converged to
`done` at resume (everything completed during the pause), `status`
reports `done` honestly. `unconfirmed[].next` lists the executable
follow-ups for each still-running execution; while any exist, `orx status`
carries the same list under `cancellation`.

## Concurrency contract

- **Pause/cancel vs startup placeholder:** the gate read and the
  placeholder write share one `BEGIN IMMEDIATE` transaction. Either the
  placeholder fully commits before the pause/cancel (it then stays
  parked/owned — a pause keeps it for resume; a cancel's sweep cancels it
  explicitly) or the start is refused with nothing written — never half
  of each.
- **Completion vs cancel:** both wrap-up outcomes are compare-and-set on
  the goal row. `done` fires only from `active`; `cancelling` fires only
  from `active|paused`. Whichever commits first is the one legal outcome;
  the loser records its facts (a late completion keeps its delivery,
  evidence, and `passed` task) and can never overwrite the winner.
- **Claim vs cancel:** a claim whose transaction runs after the cancel
  commit is refused inside the gate (the task stays parked); a claim that
  committed first survives as the unconfirmed execution the wrap-up waits
  for.
- **Sweep vs fact submission:** each task cancellation is one legal
  machine transition whose in-transaction status re-read arbitrates every
  race — a task moved by a real fact first is skipped by the sweep, and a
  task cancelled first refuses the late fact. Repeated sweeps and
  repeated cancels write nothing new (idempotent by construction).
- **Concurrent claims:** unchanged compare-and-set; exactly one winner,
  the loser gets a conflict, one claim event.
- **Repeated resume:** two simultaneous resumes serialize on the write
  lock; the Goal flips `paused -> active` exactly once and the second
  call reports the idempotent no-op.
- **No write transaction waits across a process or a barrier.** File
  writes (prompt files) happen after their transaction; every gated
  transaction is short. The manageable-CLI stop polls the goal status
  read-only from the WAITING process — the cancel itself never holds a
  write lock while a launch waits. Tests demonstrate this with
  independent sqlite connections, real subprocesses, and deterministic
  barriers (`tests/test_lifecycle.py`).

## Known follow-ups (out of this round)

- `orx sources fetch --auto-accept` while a Goal is paused leaves the
  fetched items pending and exits loudly (the auto-accept calls
  `create_goal`, which refuses); accepting them after `orx goal resume`
  is the supported path.
- Analytics stays read-only over these states (observability contract,
  v13/v14 amendments); it never gains scheduling authority.
