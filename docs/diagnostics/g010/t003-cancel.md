# G010 T003 diagnostics: goal cancel, real stop confirmation, safe wrap-up

Date: 2026-10-16. Scope of record: the design facts and race reasoning
behind `orx goal cancel`. The user-file baseline kept protecting
`docs/m1.2-report.md` throughout; this round's changes never touch it.

## Why two phases (cancelling -> cancelled), not one jump

A single `active -> cancelled` jump has no place to record "a cancel was
requested but executions are still running". The wrap-up needs a persisted
INTERMEDIATE state because two independent things must become true before
the Goal may end:

1. no new work may start (the startup gate) AND the completion CAS must be
   disarmed — a completion racing the cancel must not aggregate the Goal
   to `done`;
2. every running execution must be closed on a real fact — silence of any
   kind (lost contact, overdue heartbeat, dead-looking session) never
   closes anything (the G009 discipline, kept verbatim).

`cancelling` is exactly state 1 without pretending state 2. It occupies
the one unfinished-Goal slot (`goal_unfinished` admits it), refuses
`goal resume` (a cancel cannot be un-asked), and converges to `cancelled`
by compare-and-set only when `_outstanding_executions` is empty (no
RUNNING task, no dispatched still-open verifier attempt). Schema v14
widens the goals CHECK again (parent-table rebuild with FK deferred, the
v13 pattern; verified by `tests/test_state.py` migration tests).

## Why the completion race needs the CAS, not ordering

`Store.goal_complete_if_active` fires only on an `active` row. The moment
`goal cancel`'s transaction commits `cancelling`, every later completion
refresh records its real facts (delivery, evidence, verification rows,
`passed` task) and the goal write simply does not fire — the Goal can only
end cancelled. The reverse order needs no new machinery at all: a Goal
that reached `done` before the cancel refuses it (`cannot cancel from
done`), so exactly one wrap-up result exists in both interleavings. The
tests pin both orders plus the deterministic claim/cancel race (independent
connections, gate re-read inside `machine.claim`'s transaction).

## What the sweep cancels, and why the planning assignment is included

Unstarted work is cancelled explicitly through LEGAL task-machine
transitions (pending/runnable/blocked/waiting_host/waiting_external ->
cancelled; verifying-with-no-dispatched-verifier -> cancelled), each with a
`goal_cancel` event; still-open placeholder attempts close
`result='cancelled'` via the existing `attempts_close_for_tasks`
(`started_at` never synthesized — the R006 zombie rule). The waiting
planning ASSIGNMENT is unstarted work too: it has no claim protocol, so
the assignment row and its open planner attempt are cancelled and a late
`orx plan submit` is refused by the startup gate — the operator sees a
clear refusal, not a silently dropped plan.

The sweep is idempotent by construction: already-cancelled tasks are
skipped, and each attempted transition's in-transaction status re-read
arbitrates races with fact submissions (a task moved by a real fact first
is left alone — that fact is the one legal outcome for it). Repeated
`goal cancel` calls re-run the sweep and write nothing new (pinned: the
event count does not move).

## The manageable CLI stop — what ORX may and may not kill

ORX may stop exactly the processes IT started: a CLI worker/planner/
verifier launched by this project's own `orx run` / `orx plan` / `orx
verify dispatch` process. The mechanics:

- `adapters.base.Launch` carries an optional `stop_check` (a cheap
  read-only predicate; dispatch closes over the persisted goal status).
  `runtime.run_launch` keeps its one-argument signature — the stop
  capability rides the Launch record, so nothing else about the adapter
  contract changes.
- the manageable runner spawns the child with `start_new_session=True`,
  so the child leads its OWN process group; the stop signals that group
  (SIGTERM, escalating to SIGKILL after a 10 s grace) and nothing else —
  no pid scanning, no other user process is ever addressable;
- the runner ALWAYS waits for the actual exit (repeated `communicate()`
  with a poll timeout is the documented no-output-lost pattern), so the
  recorded exit code is a fact; `CommandResult.stop_requested /
  stop_signal / stop_escalated` carry the request;
- the caller records the machine-produced termination fact on the attempt
  (`result='cancelled'`, reason naming the stop request, the signal, and
  the actual exit), transitions the task `running -> cancelled`
  (`goal_cancel_stop`), and converges the Goal. Usage, session identity,
  and the execution log are recorded before interpretation, exactly as on
  any other CLI outcome — the history stays complete.

Another process cancelling during the execution wait is the designed path:
the stop predicate polls the goal status read-only from the WAITING
process; the cancel itself never holds a write lock while a launch waits.
The end-to-end test drives this with a real `orx goal cancel` subprocess
against a sleeping CLI worker owned by a second connection's `orx run`.

## Why fact submissions converge the Goal

The convergence re-check (`_converge_cancelled`: sweep + outstanding +
CAS) runs not only inside `goal cancel` but after every fact that can
close an executor: `task complete`, a failed/blocked delivery, `task
fail`, `task recover`, `verify submit`, and the manageable-CLI stop. The
wrap-up therefore finishes even when the cancel call returned with work
outstanding — the closing fact itself flips the Goal. During a cancel,
`task recover` closes WITHOUT re-dispatch (the task lands `cancelled`,
not runnable): the explicit fact entry is how a confirmed-terminated
host/external execution exits, and nothing is routed again for a
cancelling Goal. `orx status` and the cancel output list every
unconfirmed execution with executable follow-ups (observe / deliver /
verdict / recover), so a cross-session operator inherits a runnable
wrap-up script, not a guess.

## Scope disclosure (mechanical, out-of-list consumers)

The schema bump to v14 changes what `schema_version()` reads on every
database. Five assertions in four test files that hardcode the version
literal were updated mechanically (the number only; the T002 round set
the precedent and disclosed the same class of consumer):

- tests/test_inbox.py (1 site)
- tests/test_m12_acceptance.py (2 sites)
- tests/test_subagent_contract.py (2 sites, one raw SQL literal)
- tests/test_package_acceptance.py (1 site + the adjacent comment)

Nothing behavioral in those files changed. The scope-listed files carried
all behavioral work.
