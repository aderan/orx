# Routing strategy — model tiers, roles, and validation layers

Status: active policy (adopted 2026-10-03). This document records *why* the
routing tables look the way they do; `.orx/config.toml` and
`~/.config/orx/profiles.toml` are the executed form. Terminology lives in
[CONTEXT.md](../CONTEXT.md); the frozen M0 mechanics are in
`docs/m0-plan.md`.

## Model tiering convention

Three classes, mapped to vendors by product tier (not by benchmark):

| ORX class | Convention | Current members |
|---|---|---|
| `frontier` | latest flagships of codex / claude code | `gpt-6.1-sol` (codex 6.1), `claude-opus-5-5` (claude code 5.5, via cursor) |
| `strong` | other vendors' pro models | glm 5.3 (the zcode host), `grok-4.7`, deepseek |
| `economy` | flash / compose tiers | glm 5.3 flash, `composer-2.5` |

deepseek and glm 5.3 flash have no harness adapter yet — they are part of the
convention but get no profile until a harness exists.

Class ≠ effort. Effort (`low | medium | high | xhigh | max`) is the reasoning
depth a profile requests; it makes a model use its existing ability more
fully but never raises the model's ceiling. A `strong + max` profile does not
become a frontier model.

**House rule:** GLM models default to `max` effort — the strong tier
compensates with maximum reasoning depth (the host profile runs this way).

### Effort vocabulary (renamed 2026-10-03)

`quick | standard | deep | max` → `low | medium | high | xhigh | max`
(`quick→low`, `standard→medium`, `deep→high`, `max` unchanged, `xhigh` new).
Historical rows in `state.db` and pre-M2 docs keep the old words; nothing is
back-written. Provider-facing levels map 1:1 today (`EFFORT_MAP` in
`adapters/base.py` stays as the seam), and adapters still validate against
each provider's catalog before passing a level.

Plan depth (`light | standard | deep`) is a different axis — how much planning
a Goal needs, not how hard a model thinks. It keeps its vocabulary.

## Model declaration and resolution (mechanism, not policy)

Since G008 (2026-10-07) a profile's `model` may be declared semantically
(`model = { family = "grok", version = "4.7" }`) instead of a bare CLI slug.
Both forms are equivalent at this policy layer: class, effort, and ladder
order are properties of the profile, never of the spelling. Turning either
form into the harness `--model` argument — baked slugs, bracket parameters,
the refreshable catalog, evolution diagnostics — is mechanism and lives in
[model selection](model-selection.md). The tier table and the role defaults
below are unchanged by that layer.

## Role × class × effort defaults

| Stage | Default | Escalation |
|---|---|---|
| Controller (`[controller]`) | strong + max (`orx-host`) | escalate by moving work to a stronger class, not by deliberating more |
| Plan, light depth | strong + medium (`cursor-strong`) | — |
| Plan, standard/deep | frontier + high (`codex-frontier`, `cursor-frontier`) | deep keeps the frontier-only gate (`allow_class_downgrade = false`) |
| Build (worker) | strong + medium (`cursor-strong`) | ladder in list order, see below |
| Verify, structural | economy + medium (`cursor-economy`) | strong fallback (`cursor-strong`, `orx-host`) |
| Verify, semantic | strong + medium (fallback rungs of `[verify]`) | pin a stronger profile for the retry |
| Verify, visual | frontier + vision (`cursor-frontier`, `agent[vision]:`) | — |
| Replan | frontier + high (planner pool) | — |

The intelligence budget is deliberately asymmetric: **planning is
intelligence-heavy, building is execution-heavy, verification is
evidence-heavy, the controller is state-heavy.** M1 paid-run data backs this —
see `docs/m1-report.md`: the frontier planner cost ~363k input tokens per
draft, strong workers carried the builds, and the economy verifier ran every
audit at high frequency for ~1/7 the input. The expensive mistake of M1 was
not tiering but *planner drafts that were thrown away* (finding #5) — which
is why planner runs are gated behind `replan` discipline, not made cheaper.

### Worker escalation ladder

`[worker] profiles` order *is* the ladder.

**2026-10-04 amendment (zcode preset).** The executed default is now
ZCode-first: `orx preset install zcode` + user-layer `orx config set` made
the defaults

    controller = zcode-controller (the ZCode host session, GLM-5.3)
    worker   = zcode-worker (GLM-5.3 subagent) → cursor-strong (grok-4.7)
    verify   = zcode-verifier-flash (Flash subagent) → cursor-economy (composer-2.5)

A project that should burn Cursor first flips the two entries in its own
`.orx/config.toml`. The historical ladder below remains valid for projects
that keep the pre-preset order. Every preset host profile also declares the
`host_context` capability (next section), so a task that declares it parks
for host even in a Cursor-first ladder — a CLI rung cannot satisfy a
host-exclusive capability.

    cursor-strong (strong+medium) → cursor-strong-high (strong+high)
    → orx-host (strong, host) → cursor-frontier / codex-frontier (frontier+high)

Automatic routing picks the first usable entry (availability/cooldown
filtered); a human/controller-driven escalation pins a stronger profile for a
retry. Triggers, from the controller contract:

- fails once → retry with feedback, same rung;
- same acceptance criterion fails twice → pin the next rung up;
- plan/verification disagreement, scope drift, wrong dependency graph →
  `orx replan` (frontier pool), never another blind retry.

## Recovery failure classes and bounded blocker suppression (G009, 2026-10-07)

The 2026-10-07 incident (a131/a132/a133, ~10M tokens): three consecutive CLI
attempts on the SAME profile hit the SAME permission blocker (Cursor's
internal shell approval denial); every retry re-launched the profile and the
retry prompts misdiagnosed the blocker as code failure ("fix the red
tests"). Recovery decisions never keyed on the blocker's identity. The fix
is a recovery vocabulary (`health.failure_class`) read from recorded facts
only, plus a bounded re-launch gate keyed on the blocker's condition:

| class | meaning | re-launch? |
|---|---|---|
| `permission` | approval/permission/sandbox denial | suppressed after the bound |
| `environment` | environment cannot serve the task as configured (tool missing, auth, model unavailable) | suppressed after the bound |
| `code` | worker-reported code failure or red controller gate | never suppressed — the repair path stays open |
| `resource` | quota/rate facts (health + the G005 reroute; T005's domain) | handled by resource gates, not here |
| `other` | unproven (protocol errors, unpatterned crashes) | never suppressed |

**Fingerprint.** A blocker condition fingerprints deterministically over
(normalized reason core + profile + harness + class) — the delivery-result
envelope and the `exit N:` transport prefix are stripped, so the same
condition recorded through different envelopes fingerprints equal while a
genuinely different condition does not.

**Suppression.** `worker.max_same_blocker_attempts` (config, default 3 — the
Goal's "at most three consecutive attempts on the same problem") bounds the
trailing same-fingerprint blocker streak per (task, profile). At the bound,
`orx run` excludes that profile for THAT task (task-scoped — the profile
stays routable for other tasks): the ladder falls through to the next rung
exactly like a resource gate, or dispatching stops with a recorded reason —
in the run output, the routing decision row, and a `dispatch_suppressed`
task event — naming the blocker, the count, the fingerprint, and the remedy
(config change / routing change / human). `orx task retry` stays available
(operator intent) but reports the suppression in its payload. A different
failure signature, a success on the profile, or a replan resets the streak.

**Explicit re-verification after a permission fix (G010 T004).** The
suppression above had a recorded gap (recovery-hardening-report T008): the
excluded profile could never record the different-fingerprint attempt that
would break its own streak — the exclusion is what kept it from launching.
The config-change branch of the remedy now has a machine: when a
suppression engages, `orx run` records the profile's effective
permission-config fingerprint (the G009 T007 preflight, read-only) as a
`suppression_baseline` task event bound to that blocker episode; after the
operator fixes the real config, `orx task reverify <task> --profile <name>
--reason <what changed>` re-reads the current effective fingerprint and
compares. A genuinely different fingerprint lifts the exclusion for exactly
that (task, profile) — a `suppression_reopen` event records both
fingerprints, their sources, the operator's restart reason, and the attempt
watermark. The lift is bounded and honest by construction:

- the re-verification starts no worker (no attempt, no nonce, no state
  change); the restart is the next `orx run`'s normal dispatch, which
  creates the new attempt and nonce — and a passed preflight is never
  stated as execution success (in-agent shell execution stays unprovable);
- the lift is one-shot: the profile's next recorded attempt on the task
  consumes it. A returning same-condition blocker re-engages the bounded
  suppression from the recorded streak; a success or a different failure
  resets it honestly;
- an unchanged effective fingerprint (an mtime-only touch included), a
  config that already got its relaunch, or a repeated reverify never lifts
  — each refusal is a recorded `suppression_reverify_refused` event with
  its machine reason and the next honest step;
- a suppression with no recorded baseline (it engaged before this seam, or
  nothing readable existed then) never lifts on a claim: the current config
  is recorded as the supplementary baseline and the steps say what must
  change; host/shell harnesses (no permission config to consume) get the
  routing-shaped steps instead;
- the Goal lifecycle startup gate applies — paused/cancelling/cancelled
  refuse before anything is recorded; and the scope is exactly
  (task, profile): other tasks' streaks, other profiles, resource/quota
  gates, and the recorded failure history keep their own semantics.

**Budget gates (G009 T011).** The `[budget]` config keys are the cost
backstop, the same explainable-stop shape as suppression:
`budget.hard_stop_task_processing_tokens` (default 50M) and
`budget.hard_stop_task_attempts` (default 8) gate NEW launches for one
task when its RECORDED usage crosses the bound;
`budget.hard_stop_run_processing_tokens` (default 150M) defers every new
launch of the run. Thresholds are calibrated from recorded samples (the
2026-10-07 incident's usage rows and the T009 microtask baseline — see
config.BUILTIN_DEFAULTS citations); "processing tokens" use the per-harness
normalized volume from the T010 vocabulary (zcode/codex input already
includes cache; cursor sums three buckets) and are never a billing claim.
Unknown usage is never treated as free: attempts without a usage
observation count as `attempts_unassessed` and toward the attempt bound;
observations whose harness declares no cache semantics count as
`attempts_unnormalized` and make no claim in the sum. A hard stop records
`dispatch_budget_stopped` and leaves the task runnable with every recorded
fact queryable — the restore point is consciously raising the limit
(`orx config set budget.…`). `budget.soft_warn_task_processing_tokens`
(default 8M) only annotates: dispatched entries and `orx task list` carry
the budget observation; nothing is stopped below the hard bounds.

**Same-session continuation (verified facts).** Resume support was verified
2026-10-07 from each CLI's own help output, read-only (no paid calls):
cursor 3.23.12 ships `--resume [chatId]` / `--continue`; codex-cli 0.160.0
ships `codex exec resume [SESSION_ID] [PROMPT]` (works non-interactively);
a shell-harness profile is a plain executable — no session exists, nothing
to resume; `zcode` is a host harness where continuation IS the host session
model. Retry guidance is capability-honest (`adapters.base.
harness_resume_supported`): a resume-capable harness is told the repair can
continue the SAME session once conditions changed; a sessionless harness is
told plainly that every attempt is a new process and the repair package is
its full context. The bounded fallback for a harness without resume is a
NEW attempt carrying the compact repair package (the prior-failure block:
reason, failed checks with logs, classified guidance) — never a claim of
same-session continuation.

## Host-exclusive capability routing (`host_context`) — 2026-10-04

R002 lesson: the report/global-acceptance task (T005) burned two CLI worker
rounds (~21 min) on the same failing check before a host attempt passed in
18 min. That task class never belonged on a CLI worker — the decision belongs
at **plan time**, not at retry time.

**Mechanism.** `routing.required_capabilities` may name `host_context`
(`HOST_CONTEXT_CAPABILITY` in `src/orx/routing.py`). Host-driver profiles
declare the capability in profiles.toml — the zcode preset registers it on
all four host profiles (`zcode-controller`, `zcode-worker`,
`zcode-verifier-flash`, `zcode-verifier-strong`). When `orx run` routes a
task that declares it:

- only host-driver profiles that declare the capability are kept; the task is
  parked as a host assignment (`waiting_host`, `orx task claim …`) — **a CLI
  worker is never started for it**;
- the exclusivity is structural, not a convention: a non-host profile that
  claims `host_context` is still rejected (`driver_not_host`), so a
  misconfigured profiles.toml cannot silently land the task on a CLI worker;
- if no profile in the role's ladder can serve it, routing fails loudly
  (`missing_capability`) and the task stays runnable — it is never silently
  delegated to a CLI fallback.

A task that does **not** declare the capability routes exactly as before:
same ladder, same order, same filters. The declaration is the only switch.

**When to declare it at plan stage** (planner/controller judgment criteria —
also in `skills/orx-controller`):

- **Global acceptance** — checks that judge the whole run against the Goal
  (`orx status --json` arbitration, cross-cutting acceptance criteria), not
  one task's diff.
- **Report generation from cross-task read-only snapshots** — deliverables
  assembled from run state, verification history, and transcripts across many
  tasks (read-only, no scope of their own).
- **Tasks needing main-session context** — work whose real input is the
  Controller's session context (handovers, consulting syntheses, decisions
  that depend on conversation history rather than repo files).

Ordinary build/verify tasks never declare it; a task that a CLI worker can
do from its prompt alone stays on the ladder.

**Why capability matching and not a new `prefer_driver` flag:** the existing
`required_capabilities` filter already expresses "must run in the host" once
host profiles declare the capability — the driver gate in `route()` closes
the only gap (a non-host profile claiming it). A separate driver hint would
be a second way to say the same thing; the additive allowance for
`prefer_driver` was not needed and was not added.

## Validation layers (V0–V3)

Plans express verification through the existing syntax; the layering is a
policy about *what proves what*, not new machinery:

| Layer | Mechanism | Cost | Trust |
|---|---|---|---|
| V0 deterministic | shell command entries | free, no LLM | highest — compile/tests/lint/fixtures |
| V1 structural | `agent:` on economy | cheap | tasks complete, files changed, evidence present, no TODO/stub residue |
| V2 semantic | `agent:` on strong | medium | logic meets the Goal, edge/error paths, hidden regressions, happy-path-only tests |
| V3 visual | `agent[vision]:` on a vision profile | medium | layout/spacing/overflow/state vs acceptance criteria |

**V0-first rule** (baked into the planner prompt): every verification list
leads with the shell checks that can prove the criterion; agent checks cover
only what shell cannot prove; `agent[vision]:` only for rendered-UI
acceptance. A verifier never relaxes acceptance criteria to produce a pass —
it reports, it does not repair.

## Final acceptance

A run lands `done` only when all three hold (the controller contract repeats
this): **acceptance criteria met, evidence files complete, verifications
passed** — arbitrated by `orx status --json`, never by output "looking
finished".

## Project-level overrides

Layering already provides the override points; no per-project dialect exists:

- **Profile override:** a same-name profile in the project's
  `.orx/profiles.toml` completely replaces the user-layer one — redeclare
  with a different `effort`/`class` to re-tier a model for one project.
- **Routing override:** a project `[worker]` / `[verify]` / `[plan.*]`
  section replaces the whole user-layer section — reorder or swap profiles
  per project.
- **Effort variation without new models:** effort is baked into profiles, so
  "same model, deeper" is a second profile (`cursor-strong` /
  `cursor-strong-high` is the pattern).

## Future work (explicitly not done here)

- `[worker.low|medium|high]` per-complexity routing — Plan IR already carries
  `routing.complexity` (validated, persisted) but dispatch does not consume
  it yet.
- Automatic retry escalation (climb the ladder on retry *count* instead of
  controller-pinned profiles). G005 landed the resource-shaped subset: a
  quota/rate-limit failure re-routes one rung down inside the same
  `orx run` slice, and an exhaustion with a known reset self-releases when
  it passes.
- Field-level profile merge (project overrides one field instead of the whole
  profile).
- Harness adapters for deepseek / glm-flash CLI surfaces.
