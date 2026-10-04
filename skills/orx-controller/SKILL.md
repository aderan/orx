---
name: orx-controller
description: Drive ORX runs as the host Controller — read status, launch subagents for host assignments, claim tasks, submit results, and land the run at done.
---

# ORX Controller

You orchestrate an ORX run. ORX owns the state machine; you execute host work
and keep the loop moving. Never edit `.orx/state.db` directly.

## The loop

1. Read `orx status --json` before acting. It is the only source of truth.
2. Treat `mode = "host_required"` from `orx plan` (or an entry in
   `host_required` from `orx run`) as your cue to launch a subagent — it is
   never a failure. The entry's `execution` spec is the full launch contract:
   `mode` (`self` = do it yourself; `subagent` = launch the native agent named
   by `agent_ref`, e.g. the ZCode subagent `orx-worker`/`orx-verifier`),
   requested `model`/`effort` (profile facts — report what actually ran via
   `--actual-model`, never assume), `workdir`, and the stable `attempt` id.
3. Pass the assignment `prompt` and `schema` through to the subagent
   **unchanged**. Do not paraphrase the prompt or trim the schema.
4. For a planning assignment: run the prompt in a subagent, have it produce
   Plan IR JSON, save it to a file, then `orx plan submit --file plan.json`.
   If validation returns `errors`, fix the plan per the errors and resubmit;
   the assignment stays `waiting_host`.
5. For a host task: `orx task claim <id>` FIRST, then do the work, then submit
   the result. Claiming after working invites a conflict exit. Quote the
   attempt you answered: `orx task complete <id> --evidence evidence.json
   --attempt <id from claim>`. A submission for an attempt that is closed or
   no longer the latest is rejected as stale — that is correct; do not fight it.
6. When the work is done, write an evidence file and
   `orx task complete <id> --evidence evidence.json`. Completion means
   "execution finished", NOT "passed" — verification decides.
7. When the work failed, `orx task fail <id> --reason "<why>"`.
8. Retry (`orx task retry <id>`) for test failures, process crashes, or
   incomplete implementations. Retry keeps the same plan.
9. Replan (`orx replan`) only when an assumption or the dependency graph is
   wrong. Replan is rejected while tasks are running or verifying. When you
   replan mid-Goal, write a context file first and pass
   `orx replan --context-file <file>`: the file carries this round's reason,
   the intent (what should change, what must not), and supporting material.
   ORX adds the Goal verbatim plus a deterministic execution-fact snapshot
   (revisions, task statuses, passed work, failure reasons, evidence and
   verification references) to the same prompt — the planner sees all three,
   and the Goal is never rewritten. Re-running `orx replan` refreshes a
   still-waiting planning assignment with the latest facts and intent.
10. When any task is in `verifying`, run `orx verify`. Agent checks come back
    to you as assignments bound to a stable `attempt` id; submit each verdict
    with `orx verify submit <task> --result pass|fail --entry '<exact entry>'
    --attempt <id> [--evidence <file>] [--actual-model <what the verifier
    actually ran>]`. The verdict closes the attempt it was dispatched to;
    re-running `orx verify` re-surfaces the same attempt (never a second
    dispatch), and routing edits between dispatch and submit cannot move the
    attribution. A model mismatch prints a WARNING and is recorded — the
    verdict still counts, but never hide it.
11. The run is Done only when `orx status --json` says `"run": {"status": "done"}`.
    Not when output "looks finished".

## Resuming (new session, no chat history)

`orx status --json` is the state; `orx run` re-surfaces every parked
host/external assignment with its prompt file — the prompts are
self-contained (Goal constraints, Goal context, scope, acceptance,
verification, prior failure feedback), so pass them through unchanged and
never reconstruct them from memory. Planning assignments live under
`.orx/runs/<run>/assignments/`.

## Evidence file

```json
{ "summary": "", "commands": [], "artifacts": [] }
```

## Escalation & acceptance (routing strategy: docs/routing-strategy.md)

- Default posture is operational, not strategic: read state, pick the next
  step, dispatch. Deep deliberation is for planners and repeated failures.
- Build fails once → retry with feedback (same plan, same rung).
- Same acceptance criterion fails twice → escalate one rung of the worker
  ladder (`cursor-strong` → `cursor-strong-high` → frontier profiles) by
  pinning the stronger profile for that retry.
- Plan/verification disagreement, scope drift, or a wrong dependency graph →
  `orx replan`, not another retry.
- Effort ladder on the host (GLM) already runs at `max`; escalate by moving
  work to a stronger class, not by thinking harder about state transitions.
- Accept a run as Done only with all three: acceptance criteria met,
  evidence files complete, verifications passed (`orx status --json` is the
  arbiter).

## Hard rules

- One active plan revision controls the run; ignore cancelled tasks from old
  revisions (`task claim` on them fails — that is correct).
- Do not invent verification commands. Only the plan's list runs.
- If routing fails (profile unavailable/exhausted, capability missing), fix
  profiles.toml or `orx resource set` — do not bypass routing.
