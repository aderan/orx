# ORX

ORX controls agent scheduling to carry out a Goal: it validates plans, routes
assignments, records execution, and coordinates verification. The host
Controller drives the loop; agents plan, perform the work, and judge results.
SQLite is the state authority; TOML holds only static configuration.

The ORX repository also maintains [ORX Analytics](tools/orx-analytics/README.md)
and the [ORX macOS app](apps/macos/README.md). The two Python commands install
independently. Install from the current development source with uv:

```sh
uv tool install --python 3.12 'git+https://github.com/aderan/orx.git@main'
uv tool install --python 3.12 'git+https://github.com/aderan/orx.git@main#subdirectory=tools/orx-analytics'
orx version --json
orx-analytics --version
```

Use a full commit SHA in place of `main` for a reproducible install. See
[Analytics installation and source migration](tools/orx-analytics/docs/installation.md)
and the [macOS build and rollback guide](tools/orx-analytics/docs/app-installation.md).
Release downloads are listed on [ORX Releases](https://github.com/aderan/orx/releases).
Historical Analytics downloads are available from the
[previous repository's releases](https://github.com/aderan/orx-analytics/releases).

The product boundary is the Goal execution control layer. See
[职责与边界](docs/responsibilities.md) for what ORX executes, delegates, and
excludes, and [CONTEXT.md](CONTEXT.md) for the shared domain vocabulary.

See [Planning and model selection](CONTEXT.md#planning-and-model-selection)
for the meaning of Plan Depth, Task Difficulty, Model Tier, Specialized Model
(`S`), Effort, and Capability. The [0.5 migration plan](IMPLEMENTATION_PLAN.md)
tracks rollout of the tier and effort conventions. See
[model selection](docs/model-selection.md) for currently supported configuration.

**M0 is complete.** The CLI drives the full lifecycle — Goal → Plan →
Tasks (host, CLI, or external) → verification → Done — and the adapters
implement the probed Codex / Cursor / shell launch surfaces. Automated tests
(201) and the acceptance scenario run entirely on local fakes: no paid model
is ever called. See `docs/m0-report.md` for the acceptance log.

## Quick start

```sh
uv sync
uv run orx init
uv run orx doctor
uv run orx config models        # preview project candidates before dispatch
uv run orx goal new --objective "..." --acceptance "..."
uv run orx plan --json          # host planner: assignment; cli planner: launches
# write plan.json, then:
uv run orx plan submit --file plan.json
uv run orx run                  # starts <=1 CLI process; parks host/external work
uv run orx task claim T001
uv run orx task complete T001 --evidence evidence.json
uv run orx verify               # deterministic checks + agent verifier dispatch
uv run orx verify submit T001 --result pass
uv run orx goal pause             # stop NEW dispatch; running executors finish
uv run orx goal resume            # keep run/revision/attempt/nonce; then:
uv run orx status
uv run orx timeline --run R001 --limit 20
uv run orx usage --json        # per-profile tasks, runtime, tokens + accuracy
```

Every command takes `--json` for a machine envelope:
`{"ok": true, ...}` or `{"ok": false, "error": "..."}` (exit 0/1; 2 = usage).

## Current Controller and clean setup

Declare the current conversation with root options before the command:

```sh
orx --host codex skill install       # creates this host's skill links if needed
orx --host codex init
orx --host codex config models --json
orx --host codex plan --json
# In a ZCode conversation, use --host zcode for the same project.
```

`--host` identifies the Controller for this invocation. Repeat
`--host-agent <reference>` for native agents the conversation can actually
launch; omission declares none. `--host-session <actual-id>` records the
Controller reference when known. Explicit options replace the legacy
`ORX_CONTROLLER_HARNESS` / `ORX_CONTROLLER_AGENTS` declaration and are
retained in executable `next_actions` commands. They do not edit project
routing or start a Controller process.

Without an installed user preset, `init` creates a host self profile with
`harness = "current"`. It follows the declared host; a missing declaration
produces a setup hint. This value is valid only for host self execution.
Its `model = "unconfigured"` is a placeholder, not a model selection or
an observation about the current conversation.

For optional CLI defaults on a clean user layer, run `orx preset install cli`
**before** `init`. The project then inherits editable profiles for Claude
Code Sonnet planning, ZCode/Cursor implementation, and Codex verification,
plus current-host self work for tasks requiring host context. Each CLI needs
its own installation and login; model names are configuration examples.
Review `config models` before dispatch. Existing project overrides and user
settings remain effective; installing a preset does not replace them.

`orx --host zcode skill install` also works when that host's skill directory
does not exist. Codex, ZCode, Cursor and Claude Code are supported targets.
For isolated installation checks, `skill install/update --home <directory>`
chooses a separate installation home. PBV remains explicitly opt-in.

See the [clean-install acceptance](docs/diagnostics/host-install-2026-11-19/README.md)
for real Codex and ZCode Controller Runs using the built wheel.

## Model tiers and effort

New profiles declare `tier = "T0" | "T1" | "T2" | "T3" | "S"` and
`effort = "low" | "medium" | "high" | "max"`. Tier and effort are independent.
S profiles require a named specialized capability, such as `ocr`, and
participate only when the task explicitly requests it.

```toml
[profiles.project-planner]
driver = "cli"
harness = "claude"
model = "sonnet"
tier = "T1"
effort = "high"
capabilities = ["coding"]
```

The Controller selects `orx plan --depth light|standard|deep`. ORX uses the
project's ordered candidates for that depth and filters unavailable or
incompatible profiles. Standard is the default, including for legacy
`plan.depth = "auto"`. The old `allow_class_downgrade` setting is accepted
for compatibility.

Existing `class` profiles and `xhigh` keep their old execution behavior.
New profiles can explicitly set `native_effort` for a model-specific
parameter. See [tier and effort mapping](docs/routing-strategy.md) for the
harness mappings and [model configuration](docs/model-selection.md) for
migration examples.

This checkout's [project routing](.orx/config.toml) uses Claude Code Sonnet
for planning, ZCode then Cursor for implementation, and Codex CLI for
semantic verification. CLI profiles are explicit in
[profiles.toml](.orx/profiles.toml); compatible native fallbacks remain
inherited from user configuration. See the [Stage 4 acceptance](docs/diagnostics/stage4-2026-11-19/README.md)
for the coding Run, injected recovery checks, and measurement limits.

## Profiles (`.orx/profiles.toml`)

- `driver = "host"` — you (or your host agent) do it: `task claim` →
  optional `task heartbeat` progress reports while running →
  `task complete` / `task fail`.
- `driver = "cli", harness = "shell"` — any executable; prompt via
  `prompt_transport` (stdin | `{prompt}` argument slot | `{prompt_file}` slot).
  Workers may print `ORX_ACTUAL_EFFORT=<level>`; verifiers must print
  `ORX_VERDICT=pass|fail`.
- `driver = "cli", harness = "codex"` — `codex exec --json -m <model> -C <root>
  --dangerously-bypass-approvals-and-sandbox --ephemeral --output-last-message <file> [-c
  model_reasoning_effort=<validated>] <prompt>`; the model declaration is the
  same dual form, resolved strictly — effort validated against the local
  `codex debug models` catalog (see
  [model selection](docs/model-selection.md)).
- `driver = "cli", harness = "cursor"` — `agent --print --output-format json
  --workspace <root> --trust --force --model '<resolved>' <prompt>`. The model comes
  from the profile's declaration — a legacy bare slug or a semantic `model`
  table (`family` / `version` / `effort` / `speed` / `context`) — and is
  resolved at launch against the refreshable local catalog into an
  enumerated variant slug, a bracket parameter string for unlisted
  families, or a pre-launch diagnostic that never substitutes another
  model ([model selection](docs/model-selection.md)). Existing profiles can
  still contain `force`, which no longer controls agent launches.
- `driver = "cli", harness = "claude"` — Claude Code print mode with explicit
  `--dangerously-skip-permissions`, `--model` and `--effort`. Set `model = "sonnet"`
  to let Claude Code resolve its Sonnet alias, or use an explicit model ID when the project
  needs to pin it. Planner and Verifier use native JSON schemas. ORX keeps
  the requested alias in the profile and records the reported model when
  the terminal result includes it.
- `driver = "cli", harness = "zcode"` — ZCode headless `--mode yolo`.
  For a built-in account provider, specify its qualified model ID (for
  example `account:bigmodel-individual-coding-plan/GLM-5.3`) and effort.
  The adapter writes a provider selection file for each launch and uses the
  existing CLI login. The result stream provides the session ID, model and
  usage when reported. Effective reasoning remains unknown if the CLI
  reports only the requested level.

All three ORX-launched agent roles (Planner, Worker and Verifier) use these
permission settings. Run them in trusted projects or an externally isolated
environment. Cursor `--force` honors explicit denies and leaves sandboxing to
Cursor configuration. See the [task authorization backlog](docs/backlog.md).
- `driver = "external"` — parked at `waiting_external`; you finish it.

Failed CLI launches record process timeouts as `timeout` in health classification.
Structured output contributes provider errors through its error envelopes.
Ordinary JSONL timestamps, event IDs, token counts, generated reasoning, and
successful tool output do not trigger rate-limit cooldowns. Plain diagnostics
require an explicit HTTP/status 429 or a rate-limit message.

When a CLI verifier disconnects, times out, or returns an invalid result,
`orx verify --json` reports a verifier attempt error with a null verdict.
Run `orx verify` again to retry pending entries. Previously accepted checks
on the same unchanged delivery and inputs remain usable. Artifact, preread,
or dependency changes invalidate affected checks while retaining their history.
`status` reports stale inputs read-only; `verify` or `run` applies the invalidation.
See [verification validity](docs/verification-validity.md) for input versions,
dependency propagation, and stale in-flight verdicts. A valid `fail` verdict puts
the task into the worker repair flow.

`orx run --json` returns each worker's `inputs`, including its scope, checks,
prereads, and `dependency_artifacts`. For new structured deliveries, ORX
records the project-relative artifacts declared by the worker after the
command gate passes. Once a direct dependency passes all its checks, its
paths and producing attempt appear in the next task's assignment. The paths
refer to the project workspace, and unavailable paths are marked explicitly.

`orx verify --json` returns `evidence` for each issued verifier assignment,
including the current worker attempt, command-check results and log paths,
and the current and dependency artifacts. `run`, `verify`, and `status`
also return `next_actions` for the Controller, such as dispatching a ready
task, continuing verification, handling a host assignment, or inspecting a
blocker. The Controller decides when to retry, replan, or request input.

## Layout

- `.orx/config.toml` — role/depth profile ordering, runtime caps
- `.orx/profiles.toml` — profile definitions
- `.orx/state.db` — SQLite: goals, runs, plan revisions, tasks, task_events,
  attempts, evidence, verifications, routing_decisions, resource_status
- `.orx/runs/<run>/` — assignment prompts, exec logs, verification logs
- `ORX_PROJECT` env var or parent search finds the project root

The user configuration directory contains both `config.toml` and
`profiles.toml`. ORX checks `ORX_CONFIG_DIR`, then `ORX_HOME`, then
`$XDG_CONFIG_HOME/orx` when explicitly set. The default is `~/.orx`.
Use an absolute path for overrides so the terminal and App can resolve it
from different working directories. Data follows `ORX_DATA_DIR`,
`$XDG_DATA_HOME/orx`, or `~/.local/share/orx`.

```sh
orx config path [--json]
orx config migrate-home [--json]  # migrate the old default user configuration
orx config list [--json]          # effective value + winning layer
orx config models [--json]        # per-role models, effort, order, and sources
orx config get <key> [--json]     # same value and origin as list
orx config set <key> <value> [--user] [--json]
```

On upgrade from the old default `~/.config/orx`, finish config edits and
run `orx config migrate-home` once. The command copies both TOMLs into the
resolved new directory together, requires a destination that does not yet
exist, and keeps the originals. Check the result with `orx config path`.
The macOS routing page uses the CLI selected in its ORX executable field
and shows the config path it resolved. An override exported only in a
terminal applies to that terminal, so use the default for Finder launches
or provide the override in the GUI session environment as well.

`config set` writes `.orx/config.toml` unless `--user` is given. Values are
parsed and checked before the file changes; a rejected write leaves it
untouched. `schema_version` is not writable. A missing user config is created
with `schema_version = 1` and only the key being set. Effective precedence,
high to low, is environment (`ORX_RUNTIME_MAX_PARALLEL`,
`ORX_RUNTIME_COMMAND_TIMEOUT_SEC`), project, user, then built-in defaults.

`orx goal new` and `orx goal resume` display the same model selection.
Candidate order includes fallbacks. Task capabilities and resource status
determine the route at dispatch. Pass `--host` and any `--host-agent`
declarations to flag incompatible host candidates. To change project order, use `orx config set` with
`plan.<depth>.profiles`, `worker.profiles`, or `verify.profiles`.

## Agent discovery

```sh
orx agent list [--json]
orx agent info <harness> [--json]
orx agent probe <harness> [--json]
```

`list` shows `codex`, `cursor`, `zcode`, `claude`, and `shell`. `info`
reads the latest capability snapshot (it
does not probe) and prints that harness's launch contract. `probe` runs the
shared local checks — binary, version, help, auth, models — and writes
`probes/<harness>.json` under the user data directory. A probe never
launches a completion. Model catalogs are probed on Codex and Cursor.
ZCode and Claude Code expose binary and interface probes. Shell and unknown
harness names exit 1; a missing argument
exits 2. `--json` uses the same envelope as the rest of the CLI.

## Agent health

```sh
orx agent status [--json]
```

Per-profile health from `resource_status` in `.orx/state.db` (never TOML).
One row for every configured profile, then any status row whose profile is
no longer defined. A profile with no row is `unknown`. Human columns are
`PROFILE`, `STATE`, `SINCE`, `REASON`. `SINCE` is `updated_at`. `REASON`
joins `last_error_kind` and `note`. A `cooldown` row includes
`retry <cooldown_until>`. `reset <quota_reset_at>` appears when a quota reset time is known; a known
reset that has already passed shows `reset passed (routes again)`. A manual
`orx resource set` override is marked `override`; `orx resource clear` drops
that mark. `--json` is the usual `{"ok": true, ...}` envelope; `profiles`
carries the same fields. A missing project exits 1.

## Live quota preflight

```sh
orx quota [--force] [--json]
```

Best-effort live usage for the three harnesses with a quota source —
`codex` (ChatGPT backend), `cursor` (dashboard usage-summary via the
login-keychain session), `zcode` (GLM coding-plan monitor API) — plus which
of the project's CLI profiles each harness backs. Never launches an agent
and never raises on an unreachable provider (that reports `unknown`).
Snapshots are cached for 60s; `--force` refetches. Outside a project it
prints the harness snapshots only. `orx run` / `orx plan` / `orx verify`
run the same preflight before routing: a provider that reports a reached
limit gates its profiles as `exhausted` with the reset time, an expired
exhaustion is released automatically, and operator `orx resource set`
overrides always win. `ORX_QUOTA_PREFLIGHT=0` disables the preflight
entirely.

## Timeline

```sh
orx timeline [--run R###] [--task T###] [--profile NAME] [--limit N] [--json]
```

Read-only history over the tables already in `.orx/state.db` (no new tables):
goal and run creation, planning assignments, routing decisions, attempts
(planner, worker, and verifier), task events, and verifications. Entries are
strictly time-ordered. Each one is `{ts, actor, event, detail}`. Human lines
are `HH:MM:SS  actor  event  detail`. `--limit` keeps the newest N, still
oldest-first. An unknown run or task exits 1; a bad `--limit` exits 2.

## Usage

```sh
orx usage [--profile NAME] [--json]
```

Per-profile aggregates from attempts and `usage_observations` in
`.orx/state.db`. `tasks` is the number of distinct task ids. `runtime_sec`
sums each attempt's `started_at`..`ended_at` span. Token fields
(`input_tokens`, `output_tokens`, `cached_input_tokens`) sum observations.
A null field means that number was not observed, not that it was zero.
An attempt with no observation leaves the sums of the rows that exist and
sets `accuracy` to `unknown`. `accuracy` is `exact`, `estimated`, or
`unknown`. Unknown is a successful row: shell runs and streams without
usage still show tasks and runtime.
Human columns are `PROFILE`, `TASKS`, `RUNTIME`, `INPUT`, `OUTPUT`,
`CACHED`, `ACCURACY`. An unknown `--profile` exits 1; a bad flag exits 2.

Out-of-repo analysis reads `.orx/state.db` through
[docs/observability-contract.md](docs/observability-contract.md): the
v8/v9/v10/v11/v12 tables — v8 established the observability schema; the
v9, v10, v11, and v12 amendments were additive — plus join keys and the
SQL for the six report dimensions. The gate's current allowlist is
exactly the supported versions 8 through 15: the 0.3.1-era README
statement — the gate accepts exactly the supported versions 8, 9, 10,
11, and 12 — was widened additively by the v12 `zcode_db` usage source,
the v13/v14 `paused` and `cancelling` goal states, and the v15
`completion_receipts` and goal-lifecycle reads;
docs/observability-contract.md is authoritative. The external analysis
layer is the separate `orx-analytics` tool: the pairing verified with
this release is ORX v0.4.0 + Analytics v0.4.2 (Analytics store v3),
which accepts ORX schema 8–15 read-only, refuses unknown future
versions, and mirrors completion-receipt facts without the replay
payload. That contract is read-only. The analysis layer itself is not
in this repository.

## Key invariants

- `task complete` means execution finished, not that the task passed. Only
  verification passing produces `passed`; every active-revision task must be
  `passed` for the Run to be `done`.
- A replan takes effect only through the shared precheck gate; a failed
  precheck leaves the previous revision active (see below). Replan is
  rejected while any task is `running` or `verifying`; a new revision
  cancels the old revision's unfinished tasks and preserves terminal
  results as recorded facts.
- A paused Goal starts nothing: `orx goal pause` (idempotent, persisted in
  the database, one unfinished Goal per project) gates every new executor
  start — planner/replan routing, plan activation, worker dispatch,
  parked-assignment claims, verifier dispatch, and downgrade fallbacks —
  while already-running executors keep their attempt, span, nonce, and
  session, and their fact submissions still land. `orx goal resume`
  (idempotent) starts nothing either: it returns the in-flight ownership
  and the next dispatch is your explicit `orx run`. `orx goal cancel`
  (idempotent) requests the two-phase wrap-up: unstarted work is
  cancelled explicitly, a running execution is NEVER concluded from
  silence — it closes only on real facts (a late delivery, a verdict, or
  `orx task recover --termination-fact`), the Goal converges to
  `cancelled`, and the full history stays queryable. See
  [Goal lifecycle](docs/goal-lifecycle.md).
- `unknown` resource status stays routable; only `unavailable`/`exhausted` are
  skipped; `constrained` is a last resort. Resource status never rewrites TOML
  ordering.
- An explicitly pinned `--profile` never silently falls back.
- Planning depth selects a project-configured candidate list. The Controller
  chooses `--depth`; all depths accept their configured general model tiers.
- M0 write execution is serial: effective CLI parallelism is 1 (one process
  per `orx run`; more are reported as `deferred`).

## Replanning (precheck before activation)

A plan revision takes effect only through the precheck gate:

```sh
orx plan check --file plan.json    # read-only diff report before activation
orx plan submit --file plan.json   # re-runs the same precheck fresh, then activates
```

The new plan declares the old<->new correspondence (`replan` mapping; the
full contract is [docs/replan-contract.md](docs/replan-contract.md)): every
new task is classified `new` / `confirm` / `redo` / `continue`, every old
task gets a disposition (`confirmed` / `continued` / `redone` / `split` /
`merged` / `dropped`), a `redo` carries a concrete `redo_reason` (what
changed, what was wrong, or what the new plan needs the old result could not
provide — not boilerplate), and a `confirm` lists the current verification
that must still pass (`confirm_verification`, verbatim in the task's
`verification`). Prior results are cited as artifacts through the declared
correspondence — never by task number. The check report shows the renumbered
correspondence, the classifications, the redo reasons, what activation would
cancel, and which terminal results are preserved. When the check fails, the
previous revision stays active and keeps executing: nothing is cancelled, no
revision is written, and the waiting planning assignment is kept.

Boundaries:

- Task numbers are not identity: the same number in different revisions is
  different work. Only the declared mapping relates old and new tasks.
- No passed state is ever inherited: every task of a new revision starts
  pending/runnable with an empty verification window and passes only through
  its own checks. A prior pass is supporting material, never a verdict.
- Records that were never written read as unknown; ORX does not guess them.
- Whether a redo reason actually holds, whether a confirm's verification is
  sufficient, and whether a dropped task's note is honest are semantic
  judgments for the independent verifier and the Controller; the structural
  checks do not decide them.
- The R003 retrospective's token-waste figure was withdrawn (same-numbered
  tasks in different revisions had been misjudged as the same work); ORX
  claims no verified token savings from replanning.

## Host worker progress reports

Host workers report progress **explicitly**; ORX stores each report and
surfaces the latest one of the *current* attempt:

```sh
orx task heartbeat T001 --attempt 42 --phase checking --message "round 2/3 red, fixing"
```

- `--attempt` (the id `task claim` returned) is the only identity entry.
  Each report is one append-only row bound to that attempt, with a
  per-attempt sequence and an ORX-clock `received_at`; history is never
  overwritten and never backfilled — an attempt without reports reads
  `unknown`, not a guess.
- `orx status`, `orx task list`, `orx timeline`, and the `orx run` recovery
  face show the current attempt's latest report: `state` is `unknown`
  (no report yet), `reported` (age below the threshold), or `overdue`
  (age at or past the threshold — the boundary is closed).
- `overdue` prints a CHECK HINT: check the original worker session (its
  `session_ref` handle) before any fail/retry. The hint acts on nothing:
  no fail, no retry, no second worker — and the attempt's legal late
  delivery (`orx task complete --attempt <id>`) stays acceptable however
  stale the report is.
- Threshold: `worker.progress_timeout_min` (the `[worker]` table) —
  positive integer minutes, default 60 (`ORX_WORKER_PROGRESS_TIMEOUT_MIN`
  overrides; env > project > user layer > builtin).
- A report is an observation, not a liveness proof: receiving one proves
  only that a caller holding that attempt id submitted that text at that
  time. Workers make the call themselves at key phases — ORX runs no timer
  and sends no automatic heartbeat; there is no lease and no keepalive.
- After an explicit `orx task fail` + `orx task retry`, the fresh attempt
  starts a new window at `unknown`; the old attempt's further reports are
  rejected (`attempt_closed` / `attempt_superseded`) and its recorded
  reports remain only as timeline history.

The frozen contract — gates, response fields, error reasons, storage, and
recovery boundaries — is
[docs/host-progress-contract.md](docs/host-progress-contract.md).

## Skills / update

```sh
orx skill install   # ~/.agents/skills + symlinks into ~/.zcode|~/.cursor|~/.codex
orx skill update
orx update --check  # install source + upgrade path (uv tool installs only)
```

The default set installs `orx-controller` (the universal execution entry:
goal intake — stated directly or handed over as a consulting summary —
planning, assignment delivery, verification, retry/replan, recovery) and
`orx-agent` (single-assignment discipline for plan/task/verify work).
`orx-pbv` is an optional, explicitly-invoked multi-round development recipe
on top of the controller protocol: `orx skill install orx-pbv`.

Upgrading from 0.3.x? Follow
[docs/upgrade-0.4.0.md](docs/upgrade-0.4.0.md): pause writers, take the
SQLite-consistent backup (database + project config + run materials)
before the new version first opens the old database (schema v8/v11/v12
migrates automatically to the current schema version — v15 in the
released 0.4.0; rollback restores that backup — never a hand-edited
`schema_version`), then refresh skills with `orx skill update` and
explicitly compare/replace the three role definitions; upgrading the
optional analytics tool of the verified pairing (ORX v0.4.0 +
Analytics v0.4.2, store v3) is covered there too. Editable installs
update by checking out the release — no `uv tool upgrade`. Upgrading
0.3.0 to 0.3.1 instead? The historical guide stays at
[docs/upgrade-0.3.1.md](docs/upgrade-0.3.1.md).

## Docs

- [职责与边界](docs/responsibilities.md) — responsibility baseline, scope
  decisions, and current implementation limits
- [CONTEXT.md](CONTEXT.md) — Goal, Run, Task, agent roles, and related terms
- [Routing strategy](docs/routing-strategy.md) — model tiering convention,
  role × class × effort defaults, V0–V3 validation layers, escalation ladder
- [Model selection](docs/model-selection.md) — the semantic `model`
  declaration form, the refreshable model catalog (`orx agent probe` /
  `orx agent info`), per-adapter resolution rules, evidence provenance, and
  diagnostics
- [Goal lifecycle](docs/goal-lifecycle.md) — pause/resume/cancel semantics,
  the unified startup gate matrix, in-flight ownership, the concurrency
  contract, and the operation cookbook (including confirm-termination,
  re-verification after a permission fix, and delivery retransmission)
- `docs/m0-plan.md` — the frozen implementation baseline + amendment log
- `docs/m0-phase2-checkpoint.md` — core kernel checkpoint + review gate
- `docs/m0-report.md` — M0 acceptance log (commands and outputs)
- [Observability read contract](docs/observability-contract.md) — schema
  v8 through v15 tables, joins, and read-only SQL for external analysis
- [Upgrade to 0.4.0](docs/upgrade-0.4.0.md) — per-channel update steps
  (editable vs wheel), the pre-upgrade SQLite-consistent backup with the
  automatic schema migration to v15 and restore-only rollback, the
  verified pairing ORX v0.4.0 + Analytics v0.4.2 (Analytics store v3),
  and the `replan --context-file` input contract
- [Release notes 0.4.0](docs/release-0.4.0.md) — what shipped in the
  ORX v0.4.0 + Analytics v0.4.2 pairing, the schema v8→v15 chain, and
  the version/channel rules
- [Upgrade to 0.3.1](docs/upgrade-0.3.1.md) — the historical 0.3.0→0.3.1
  guide, kept verbatim with a banner pointing to the 0.4.0 guide
- [Replan contract](docs/replan-contract.md) — the declared old<->new
  correspondence, work classifications, artifact provenance, and the
  structural-check vs semantic-review boundary
- [Host progress contract](docs/host-progress-contract.md) — explicit host
  worker progress reports, the current window, overdue check hints, and the
  safe-recovery boundaries

## M1 layered configuration

User layer (preferred home for profile definitions):

    ~/.orx/config.toml             user config (optional)
    ~/.orx/profiles.toml           user profiles
    ~/.local/share/orx/            user data dir (probes, snapshots)

`ORX_CONFIG_DIR` and `ORX_HOME` select the user config directory.
`XDG_CONFIG_HOME` applies when neither is set. `ORX_DATA_DIR` still
takes precedence over `XDG_DATA_HOME` for user data. Project `.orx/` keeps `config.toml`
(routing) and optionally `profiles.toml` (same-name project profiles
completely replace user ones).

Precedence, high to low: CLI arguments (`--depth`, `--profile`) >
environment (`ORX_RUNTIME_MAX_PARALLEL`, `ORX_RUNTIME_COMMAND_TIMEOUT_SEC`;
plus `ORX_PROJECT` for discovery) > project `.orx/` > user layer >
built-in defaults. Inspect with `orx config path | list | get <key>`, write
with `orx config set <key> <value> [--user]` (project layer by default;
`schema_version` is never writable). Reading never creates files.

Adopting the user layer from a project-first setup:
`orx.config.migrate_profiles_to_user()` implements the collision-safe,
idempotent host procedure (same-name definitions must match; unrelated
user entries preserved; resource rows survive because profile identity is
the name).
