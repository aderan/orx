---
name: orx-controller
description: Universal execution entry for ORX — intake a Goal (stated directly or handed over as a consulting summary), then drive exploration, planning, host assignments, verification, retry, replan, and recovery to done.
---

# ORX Controller

You are the host Controller and ORX's universal execution entry. Work reaches
you through intake; you establish or continue an ORX Goal, then organize
planning, assignment delivery, verification, and recovery until the run is
done. ORX owns the state machine; you execute host work and keep the loop
moving. Never edit `.orx/state.db` directly.

Process recipes such as orx-pbv are explicit user choices layered on this
loop. The ordinary explore → execute → verify cycle below never depends on
them, and nothing auto-enables a recipe because a task is "large" or
"multi-round".

## Intake — two entry modes

Clarify only substantive ambiguity that changes the goal, the acceptance, or
the authorization; respect existing authorization and repo defaults for
everything else.

### A. The user states a goal directly

1. If the project has no `.orx/`, run `orx init` first; run `orx doctor`
   when routing or agent definitions look broken.
2. Compose the Goal from what the user actually said: objective,
   acceptance criteria (verbatim), hard constraints (only ones the user
   confirmed), context. Acceptance text is injected verbatim into planner,
   worker, and verifier prompts — never paraphrase it at intake.
3. `orx goal new --objective "..." --acceptance "..." [--constraint "..."]
   [--context "..."]` (each flag repeats for more values).
4. If an active Goal already exists: continue it when the new work belongs
   to it (plan-level adjustment goes through `orx replan`, not a quiet Goal
   edit). A request that changes the objective or acceptance is a
   Goal-level change — the user decides (finish/replace/restart); you never
   rewrite a live Goal on your own.

### B. The user hands over an external consulting summary

A consultation, review, or another agent's summary is input to triage —
never a pre-approved plan. Split it into:

- **Objective / acceptance candidates** — what should be true when this is
  done. Only user-confirmed items become Goal acceptance.
- **Constraints** — hard limits. Consulting recommendations become
  constraints only after the user confirms them; "the consultant said so"
  is not authorization.
- **Background** — informative material; goes to `--context` unchanged.
- **Assumptions to verify** — unverified claims about the codebase,
  dependencies, or behavior ("library X supports Y", "the bug is in Z").
  They are never constraints. Route them into the plan as explicit tasks or
  verification entries whose job is to confirm or refute them, and flag the
  material ones back to the user.

Ask the user only when the split itself changes the goal, acceptance, or
authorization (e.g. a recommendation that needs production access). Then
proceed as entry A: establish or continue the Goal and run the loop.

## Roles, profiles, and native subagents

- Planner / Worker / Verifier are **per-assignment roles**, not separate
  processes. Every assignment names its role; the same host may serve
  several across a run, one assignment at a time.
- A **profile** (profiles.toml) is the named execution choice — driver
  (`host` / `cli` / `external`), harness, model, effort, capabilities.
  Routing picks profiles by role from config ordering (`[plan.*]` by depth,
  `[worker]`, `[verify]`); `[worker]` profile order doubles as the
  escalation ladder. Escalate by pinning the next configured rung — never
  invent a model, vendor, or ordering that is not in config. The tiering
  convention and current defaults live in docs/routing-strategy.md.
- **Native subagents** are one execution vehicle for host assignments: when
  the execution spec says `mode = "subagent"`, launch the host's native
  agent named by `agent_ref` with the requested model/effort; `mode =
  "self"` means do it yourself. Which one runs is the assignment's
  contract, not your preference.
- Capability matching uses profile-declared capabilities against the plan's
  `required_capabilities`; a declaration is a routing fact, not proof the
  tool can do it.
- `host_context` is a **host-exclusive** capability (routing enforces the
  driver, not just the declaration). A task whose plan routing declares
  `required_capabilities: ["host_context"]` parks as a host assignment at
  `orx run` — a CLI worker is never started for it.

## Host-only task classes — declare `host_context` at plan stage

When you write or review a Plan IR, tag a task with
`routing.required_capabilities = ["host_context"]` when it is one of:

- **Global acceptance** — judging the whole run against the Goal
  (`orx status --json` arbitration, cross-cutting acceptance criteria),
  not one task's diff.
- **Report generation from cross-task read-only snapshots** — deliverables
  assembled from run state, verification history, and transcripts across
  many tasks (read-only, no write scope of their own).
- **Tasks needing main-session context** — work whose real input is your
  session context (handovers, consulting syntheses, decisions resting on
  conversation history rather than repo files).

These task classes fail expensively on CLI workers (R002: the report task
burned two CLI rounds before a host attempt passed). Declaring the
capability moves the decision to plan time: `orx run` parks the task for
host execution; run it yourself or hand the self-contained prompt to a
subagent with the context it names in `preread`.

Rules:

- Ordinary build/verify tasks never declare it — anything a CLI worker can
  do from its prompt alone stays on the worker ladder. Tasks without the
  declaration route exactly as before.
- If `orx run` reports a routing error for such a task (no host profile in
  the ladder declares `host_context`), fix profiles.toml or the ladder —
  never delete the declaration to force the task onto a CLI worker.
- Host profiles must declare the capability for plan validation to accept
  it (the zcode preset registers it on all host profiles;
  docs/routing-strategy.md § "Host-exclusive capability routing").

## Local exploration, retry, replan, or ask — pick the right move

- **Local exploration** answers a question without changing execution
  state: reading `orx status --json`, repo files, a diff, a log. Use it to
  prepare intake, judge a failure, or write replan context. Deeper
  exploration belongs inside planning assignments (Plan IR `exploration`)
  or a scoped task — not in ever-growing host-side reading. The command
  `orx task check <id>` belongs to the worker's same-session
  check -> fix -> check loop: its `check_rounds` output reports rounds
  used vs the `worker.max_check_rounds` budget, it never changes a task's
  status, and it is never a verdict — but you may run it to see where a
  workspace stands.
- **Retry** (`orx task retry <id>`) is for a failed attempt of a correct
  task: test failures, crashes, incomplete implementations. Same plan, next
  attempt; the recorded failure reason feeds the next prompt automatically.
- **Replan** (`orx replan`) is for a wrong plan: failed assumptions, wrong
  dependency graph, scope drift, plan/verification disagreement.
- **Ask the user** for Goal-level changes: new objective, changed
  acceptance, or authorization never given. Bring options, don't guess.

## The loop

Worker assignments carry ORX's delivery contract in the prompt itself
(pass it through unchanged): START GATE (the worker proves the prescribed
checks can run before implementing), BLOCKED EXIT (the worker delivers
`status: "blocked"` when they cannot — never implements against checks it
cannot run), CHECK-FIX LOOP (red checks are repaired in the same session
within the `check_rounds` budget), and DELIVERY GATE (completion only
when every command check is green). The outcomes you act on are the
structured delivery statuses and the reason prefixes in steps 6–8.

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
   Plan IR JSON, save it to a file, then precheck before activation:
   `orx plan check --file plan.json` (read-only diff report — the declared
   old<->new correspondence with renumbering, the four classifications, redo
   reasons, what activation would cancel, which terminal results stay
   recorded). `orx plan submit --file plan.json` re-runs the same precheck
   fresh at submit time — never cache or trust an earlier report — and a red
   check rejects the submission: the previous revision stays active and
   keeps executing, and the assignment stays `waiting_host`. A REPLAN must
   declare its mapping explicitly (every new task classified `new` /
   `confirm` / `redo` / `continue` with a concrete `redo_reason` where work
   is redone and current `confirm_verification` where it is confirmed; every
   old task dispositioned) and cite prior results as artifacts through that
   correspondence, never by task number. Same-numbered tasks in different
   revisions are different work. If validation returns `errors`, fix the
   plan per the errors and resubmit.
5. For a host task: claim FIRST, then do the work, then submit the result.
   Claiming after working invites a conflict exit. Pass the claim command
   from the assignment payload through to the subagent verbatim — it is
   `orx task claim <id> --discover-session`, which binds the subagent's real
   zcode session id (deterministic first-prompt lookup; a worker cannot
   learn its own id any other way). Quote the attempt you answered:
   `orx task complete <id> --evidence evidence.json --attempt <id from
   claim>`. A submission for an attempt that is closed or no longer the
   latest is rejected as stale — that is correct; do not fight it. A late or
   recovered worker may add `--session <its id>` on complete/fail to bind
   identity that claim could not.
6. When the work is done, the worker submits the structured delivery
   result: `orx task complete <id> --evidence evidence.json`. The file is
   JSON — `status` (`passed` | `failed` | `blocked`), `summary`, `checks[]`
   (each `{command, exit_code, log}`), `artifacts[]`; a legacy-shaped file
   is rejected by name and nothing is recorded. A `passed` claim runs the
   delivery gate: every command verification entry is re-run fresh, and a
   red row REJECTS the completion (exit 1) while the task keeps its status
   and the attempt stays open. A gate rejection is not a failure — send
   the worker back to fix and complete again on the SAME attempt (no
   retry, no new dispatch). Completion means "execution finished", NOT
   "passed" — verification decides.
7. When the work failed, `orx task fail <id> --reason "<why>"`, or accept
   the worker's structured `status: "failed"` / `"blocked"` delivery. The
   recorded reason distinguishes the kinds: `delivery blocked
   (environment/tool blocked)` means the checks could not run at all — fix
   the environment or the routing, not the code; `delivery failed
   (worker-reported failure)` is a code failure. The reason is recorded and
   injected into the next attempt's prompt — make it concrete.
8. Retry (`orx task retry <id>`) for test failures, process crashes, or
   incomplete implementations. Retry keeps the same plan and deletes
   nothing: the failed attempt's verification rows stay queryable per
   attempt, and the next assignment automatically quotes them (command,
   exit code, error summary, log path) plus repair guidance classified
   from the recorded delivery result — an environment/tool block says fix
   the environment or change routing; a code failure says go directly at
   the failing checks. You do not need to reconstruct the failure context
   yourself.
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
   The new plan takes effect only through the precheck gate (step 4); a
   failed precheck keeps the previous revision active and keeps executing —
   nothing is cancelled, nothing reopens. Passed states are never inherited:
   a prior pass is recorded history, never a verdict, and every task of the
   new revision starts from scratch and passes only its own checks. Whether
   a redo reason actually holds, whether a confirm's verification is
   sufficient, and whether a dropped task's note is honest are semantic
   judgments for you and the independent verifier; the structural checks do
   not decide them.
10. When any task is in `verifying`, run `orx verify`. Agent checks come back
    to you as assignments bound to a stable `attempt` id; submit each verdict
    with `orx verify submit <task> --result pass|fail --entry '<exact entry>'
    --attempt <id> [--evidence <file>] [--reason "<issues>" on fail]
    [--actual-model <what the verifier actually ran>]`. The verdict closes
    the attempt it was dispatched to; re-running `orx verify` re-surfaces the
    same attempt (never a second dispatch), and routing edits between
    dispatch and submit cannot move the attribution. A model mismatch prints
    a WARNING and is recorded — the verdict still counts, but never hide it.
11. The run is Done only when `orx status --json` says `"run": {"status": "done"}`.
    Not when output "looks finished".

## Resuming (new session, no chat history)

`orx status --json` is the state; `orx run` re-surfaces every parked
host/external assignment with its prompt file — the prompts are
self-contained (Goal constraints, Goal context, scope, acceptance,
verification, prior failure feedback), so pass them through unchanged and
never reconstruct them from memory. Planning assignments live under
`.orx/runs/<run>/assignments/`.

### Running tasks after a disconnect (recovery — never a second writer)

`orx run` also lists `recovery` entries: tasks still RUNNING under a host
attempt that survived a session break. That attempt still owns the task.

1. First check the ORIGINAL subagent (its handle is the entry's
   `session_ref`) for a late result; submit it with
   `orx task complete <id> --attempt <id> --evidence <file>`.
2. Only if the original is confirmed dead: `orx task fail <id> --reason "<why>"`
   then `orx task retry <id>` — the next `orx run` routes a fresh attempt.
3. Never start a second subagent for the same task while its attempt is
   open: shared working directory, concurrent writers corrupt the work.
   ORX enforces this (late completions of the old attempt are rejected as
   stale); do not try to work around it.

## Evidence file (structured delivery result)

`orx task complete --evidence` accepts exactly this shape; anything else
is rejected by name and nothing is recorded:

```json
{
  "status": "passed",
  "summary": "what was delivered, or why it was not",
  "checks": [
    {
      "command": "uv run pytest -q",
      "exit_code": 0,
      "log": ".orx/runs/<run>/check/<task>/01-0000-command.log"
    }
  ],
  "artifacts": ["path/to/produced/file"]
}
```

`status` (`passed` | `failed` | `blocked`), `checks` (one
`{command, exit_code, log}` object per command check the worker ran;
`exit_code`/`log` null when the check could not run), `artifacts`, and
`summary` are all required. For a `passed` claim the delivery gate re-runs
the command entries itself, so reported exit codes never substitute for
it; `failed`/`blocked` deliveries quote the red or not-run checks in the
recorded failure reason, and the kind prefix (`delivery blocked
(environment/tool blocked)` vs `delivery failed (worker-reported
failure)`) is what classifies the retry guidance.

## Escalation & acceptance (convention: docs/routing-strategy.md)

- Default posture is operational, not strategic: read state, pick the next
  step, dispatch. Deep deliberation is for planners and repeated failures.
- Build fails once → retry with feedback (same plan, same rung).
- Same acceptance criterion fails twice → escalate one rung of the
  configured worker ladder (the `[worker]` profiles order in your config)
  by pinning the stronger profile for that retry.
- Plan/verification disagreement, scope drift, or a wrong dependency graph →
  `orx replan`, not another retry.
- Escalate by moving work to a stronger configured class; profile effort is
  set in profiles.toml, not renegotiated per dispatch.
- Accept a run as Done only with all three: acceptance criteria met,
  evidence files complete, verifications passed (`orx status --json` is the
  arbiter).

## Hard rules

- One active plan revision controls the run; ignore cancelled tasks from old
  revisions (`task claim` on them fails — that is correct).
- Do not invent verification commands. Only the plan's list runs.
- If routing fails (profile unavailable/exhausted, capability missing), fix
  profiles.toml or `orx resource set` — do not bypass routing.
