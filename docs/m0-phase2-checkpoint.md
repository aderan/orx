# ORX Phase 2 checkpoint — Core Kernel

Date: 2026-10-03. Baseline: `docs/m0-plan.md`. No real Codex/Cursor execution
was integrated; no paid model call was made by any command or test.

## Result

**Green.** `uv run pytest` → **167 passed**. The CLI drives a full fake
lifecycle (Goal → submitted Plan → claimed Tasks → completed Tasks →
verification → Done) with no AI provider, across separate processes.

## Files / modules added

```
pyproject.toml            orx-agent 0.1.0; deps typer + pydantic>=2; script `orx`
README.md
src/orx/
  records.py    184   enums (Role/Driver/Harness/ModelClass/Effort/PlanDepth/
                      TaskStatus/ResourceStatus/...) + domain exceptions
                      (renamed from model.py at the review gate)
  config.py     325   config.toml + profiles.toml load/validate; profile
                      references; max_parallel warning (M0 forces effective 1)
  state.py     1069   SQLite v1 schema, WAL, foreign_keys, migrations keyed by
                      version (copy-migrate-replace), repository layer,
                      nested-tx via savepoints
  machine.py    101   the ONLY place task statuses change; transition table;
                      task_events audit log; atomic host claim
  plan.py       323   Plan IR (pydantic, extra=ignore), validation (dup ids,
                      missing deps, cycles, scope paths, complexity,
                      verification syntax, capability refs, verbatim Goal
                      acceptance coverage), depth policy, planner prompt+schema
  routing.py    187   deterministic routing; candidates/reasons persisted
  runtime.py    139   the only subprocess wrapper; redaction, 256 KiB
                      truncation, timeout, verification denylist
  verify.py     134   deterministic command checks + agent verdict evaluation
  dispatch.py   984   project discovery/init, goal/plan/run/task/verify
                      orchestration, readiness + run-status recompute
  doctor.py     179   environment/project checks; probes never run completions
  cli.py        503   Typer surface; every command `--json` envelope
  __main__.py
tests/          1767  conftest + 9 test modules (167 tests)
```

## Schema summary (SQLite v1, `meta.schema_version = 1`)

`goals`, `runs`, `plan_revisions` (UNIQUE(run_id, revision); status
active|superseded), `planning_assignments`, `tasks` (UNIQUE(revision_id,
task_id)), `task_dependencies`, `task_events` (from/to/event/reason —
**added** beyond the plan's table list to satisfy "persist transitions"),
`attempts` (**added `role`**; revision_id nullable for pre-plan planner
attempts), `evidence`, `verifications` (**added `kind`** command|agent),
`routing_decisions` (**added `requested_json`**; candidates_json, selected,
reason, downgrade_blocked), `resource_status`.

Version handling: newer-than-code version → error, file untouched; tables
without meta → error; missing row in resource_status reads as `unknown`.

## State-machine summary

States: pending, runnable, running, waiting_host, waiting_external, verifying,
passed, failed, blocked, cancelled. All changes flow through
`machine.transition` / `machine.claim` (single BEGIN IMMEDIATE transaction;
second claimant gets ConflictError). Legal edges are a table in machine.py
(e.g. failed→runnable only via retry; passed/cancelled terminal). Every
transition writes a task_events row (from, to, event, reason), so the path to
any current state is reconstructable. After each mutation, dispatch recomputes
dependency readiness (pending→runnable|blocked) and Run status (done only when
every active-revision task is passed; running while anything live; else
blocked), and marks the Goal done with the Run.

## CLI implemented (all with `--json` envelopes, non-zero exit on error)

init, doctor, version, goal new/show, plan (routes; host→assignment
P00n with prompt file + IR schema + submit hint), plan submit --file, replan,
run (parks host/external work; never blocks on the Controller), status
(human `✓ ● ○` layout + JSON), task list/claim/complete/fail/retry, verify
(dispatches checks, reports agent assignments), verify submit (--result
pass|fail, optional --entry/--evidence), profiles, resource list/set.

## Test command and result

```
$ uv run pytest -q
167 passed in ~3.5s
```

Coverage maps 1:1 to the required list: init, config/profile validation,
schema init + reopen/recovery, newer-version refusal, Goal persistence,
Plan IR validation (dup/missing-dep/cycle/path-escape/complexity/verification
syntax), verbatim Goal-acceptance coverage, plan revisions + replan rejection
while running/verifying + supersession cancellation, readiness, 20 legal +
15 illegal transition pairs, complete≠passed, deterministic pass/fail, host
agent-verification submission, vision-capability requirement, retry (incl.
retry-after-verification-failure), run completion, unknown-routable /
unavailable / exhausted / constrained / pinned / frontier-downgrade routing,
restart/recovery (in-process reopen and separate `python -m orx` process).
No network, no paid model.

## Demonstrated fake end-to-end lifecycle (real CLI processes, /tmp/orx-demo)

```
orx init                          → .orx/{config,profiles,state.db} seeded, resources unknown
orx doctor                        → 0 failures (harness probes: flags+auth only, "operational probe not_run")
orx goal new --objective ... --acceptance "hello.txt contains the greeting" --acceptance "summary.md exists"
orx plan --json                   → host_required, assignment P001 (orx-host, standard), prompt file written
orx plan submit --file plan.json  → revision 1, 2 tasks (T001→T002 dependency)
orx run --json                    → T001 parked waiting_host
orx task claim T001
orx task complete T001 --evidence evidence.json   → FAILED (verification `test "$(cat hello.txt)" = "hello, ORX"` exits 1)
echo "hello, ORX" > hello.txt     (operator fixes the work)
orx task retry T001; orx run; orx task claim T001
orx task complete T001 --evidence evidence.json   → passed
orx run → claim T002; write summary.md; orx task complete T002 → verifying (agent entry outstanding)
orx verify --json                 → agent_required: T002 via orx-host
orx verify submit T002 --result pass --evidence verdict.json → passed
orx status                        → Run R001: DONE; Goal done; ✓ T001 ✓ T002; 3 passed / 0 failed
orx status --json (new process)   → same state (SQLite authoritative)
orx replan --json                 → reopens: new assignment P002, run planning
```

## Deviations from docs/m0-plan.md

1. **Added `doctor.py` module and additive columns/tables** (`task_events`,
   attempts.role, verifications.kind, routing_decisions.requested_json). The
   phase goal requires persisted transitions; the plan's table list read as a
   minimum.
2. **CLI-driver planning/execution returns `mode: "incomplete"` /
   `not_implemented` entries** instead of the plan's `completed` mode — agent
   adapters are Phase 4; faking success was forbidden. No assignment or state
   change is made on that path.
3. **`orx init` writes one neutral `orx-host` profile** (driver=host,
   model="unconfigured", class=frontier) — `src/orx/` must not name
   GLM/Codex/Cursor models; users edit profiles.toml for real harnesses.
4. **Verification rows are cleared on `task retry`**: they are per-attempt
   state, and a stale failed row would make a retried task permanently failed
   (found during the CLI demo). History remains in task_events/attempts.
5. Plan validation additionally requires non-empty `scope.allowed` and checks
   `required_capabilities` against capabilities declared in profiles.toml
   ("where applicable" in the plan).
6. `orx update`, `orx skill install/update` are not implemented (Phase 6/7);
   doctor reports skills as not installed rather than failing.
7. `orx run` parks *all* runnable host tasks at waiting_host. Parking is not
   execution; the parallel cap of 1 governs process execution (Phase 4). No
   worktree isolation, per the M0 decision.
8. Minor: `_active_context` falls back to the most recent Goal once the active
   Goal is done so `orx status`/`task list` keep working after completion.

## Issues to resolve before Agent adapter integration (Phase 4)

1. **No `adapters/` yet.** Need `base.py` (Launch record), `shell.py`
   (`prompt_transport` stdin|argument|file, `ORX_ACTUAL_EFFORT=` parsing),
   then codex/cursor argv builders per the plan's probes.
2. **Host claim has no lease/heartbeat.** A host that claims and dies leaves
   the task `running`; today recovery is manual (`task fail` + `retry`, or
   replan). Decide on lease/timeout policy before unattended runs.
3. **`orx run` executes nothing yet** — the started[] slice is empty until the
   shell adapter lands; CLI-driver profiles are reported `not_implemented`.
4. **Codex/Cursor capability probes are doctor-level only.** The adapter needs
   the plan's effort maps (quick→low … max→max), `codex debug models` catalog
   validation, JSONL effort extraction (Phase 4, against captured fixtures).
5. **runtime.py lacks env-scrubbing and cancellation propagation** (timeout
   kill only). Add before launching real agents; redaction of captured output
   is in place.
6. **Planner attempts before the first revision have `revision_id = NULL`.**
   Adapters/reports must tolerate pre-plan attempts.
7. **Superseded tasks are not visible in the CLI** (active revision only);
   diagnosis currently requires sqlite. Consider `orx task list --revision`.
8. `orx plan` reuses an existing waiting_host assignment regardless of changed
   `--depth`/`--profile`; acceptable for M0, revisit when the host driver
   becomes concurrent.

## Core Review Gate addendum (2026-10-03)

External STANDARDS review of `docs/m0-plan.md` produced 23 findings (2×P0,
12×P1, 9×P2/judgement). Disposition:

- **Already resolved by the Phase 2 implementation, plan text amended to
  match:** FK id targets (explicit surrogate PKs everywhere); single
  deterministic-verification executor (idempotent, invoked by both
  `task complete` and `orx verify`); status payload `objective` (not `title`);
  second `orx goal new` refused; pinned-unusable error names profile+reason;
  recompute handles the no-revision (`planning`) case and cancelled tasks
  (superseded revisions only).
- **Fixed in code this gate:** `attempts.effort_source` and
  `verifications.required_capabilities_json` columns (v1 amended pre-release);
  profile `force` field (cursor-only); `driver=cli + harness=zcode` load
  error; doctor flag sets now equal the adapter Required sets (verified green
  against the real `codex` 0.160 / `agent` 2026.10.01 on this machine);
  run-status recompute moved into `machine.py` (single owner);
  `model.py` → `records.py` rename.
- **Plan-text only:** exit-code table (0/1/2); cursor prompt delivery
  (positional, Phase 4 verifies); shared effort map + `EffortOutcome` note for
  `adapters/base.py`; pydantic scoped to Plan IR validation;
  `planning_assignments.failed` reserved; `controller` validated-but-never-
  routed; stale "CLI/external slice" wording; review-list renumbering.
- **Kept as frozen semantics, documented (review item: replan guard ignores
  parked work):** `waiting_host` is safely cancellable because claim is the
  participation gate; `waiting_external` tasks are cancelled on supersession
  and a late `task complete` fails loudly against the superseded revision.
  Extending the guard to `waiting_external` was considered and rejected for M0
  (it would let an abandoned external task block replan indefinitely).

Tests: 170 passed after the gate fixes. No adapter-layer P0/P1 remains open;
the gate is green to proceed to Step 3 (agent adapters).
