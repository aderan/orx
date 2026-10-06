# ORX

ORX controls agent scheduling to carry out a Goal: it validates plans, routes
assignments, records execution, and coordinates verification. The host
Controller drives the loop; agents plan, perform the work, and judge results.
SQLite is the state authority; TOML holds only static configuration.

The product boundary is the Goal execution control layer. See
[职责与边界](docs/responsibilities.md) for what ORX executes, delegates, and
excludes, and [CONTEXT.md](CONTEXT.md) for the shared domain vocabulary.

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
uv run orx goal new --objective "..." --acceptance "..."
uv run orx plan --json          # host planner: assignment; cli planner: launches
# write plan.json, then:
uv run orx plan submit --file plan.json
uv run orx run                  # starts <=1 CLI process; parks host/external work
uv run orx task claim T001
uv run orx task complete T001 --evidence evidence.json
uv run orx verify               # deterministic checks + agent verifier dispatch
uv run orx verify submit T001 --result pass
uv run orx status
uv run orx timeline --run R001 --limit 20
uv run orx usage --json        # per-profile tasks, runtime, tokens + accuracy
```

Every command takes `--json` for a machine envelope:
`{"ok": true, ...}` or `{"ok": false, "error": "..."}` (exit 0/1; 2 = usage).

## Profiles (`.orx/profiles.toml`)

- `driver = "host"` — you (or your host agent) do it: `task claim` →
  `task complete` / `task fail`.
- `driver = "cli", harness = "shell"` — any executable; prompt via
  `prompt_transport` (stdin | `{prompt}` argument slot | `{prompt_file}` slot).
  Workers may print `ORX_ACTUAL_EFFORT=<level>`; verifiers must print
  `ORX_VERDICT=pass|fail`.
- `driver = "cli", harness = "codex"` — `codex exec --json -m <model> -C <root>
  -s workspace-write --ephemeral --output-last-message <file> [-c
  model_reasoning_effort=<validated>] <prompt>`; effort validated against the
  local `codex debug models` catalog.
- `driver = "cli", harness = "cursor"` — `agent --print --output-format json
  --workspace <root> --trust --model '<id>[effort=<mapped>]' <prompt>`;
  `force = true` opts into `--force`.
- `driver = "external"` — parked at `waiting_external`; you finish it.

## Layout

- `.orx/config.toml` — role/depth profile ordering, runtime caps
- `.orx/profiles.toml` — profile definitions
- `.orx/state.db` — SQLite: goals, runs, plan revisions, tasks, task_events,
  attempts, evidence, verifications, routing_decisions, resource_status
- `.orx/runs/<run>/` — assignment prompts, exec logs, verification logs
- `ORX_PROJECT` env var or parent search finds the project root

User-layer files live next to each other: `ORX_CONFIG_DIR` if set, otherwise
`$XDG_CONFIG_HOME/orx`, otherwise `~/.config/orx` (`config.toml` and
`profiles.toml`). Data (probe cache and similar) lives in `ORX_DATA_DIR`,
`$XDG_DATA_HOME/orx`, or `~/.local/share/orx`.

```sh
orx config path [--json]
orx config list [--json]          # effective value + winning layer
orx config get <key> [--json]     # same value and origin as list
orx config set <key> <value> [--user] [--json]
```

`config set` writes `.orx/config.toml` unless `--user` is given. Values are
parsed and checked before the file changes; a rejected write leaves it
untouched. `schema_version` is not writable. A missing user config is created
with `schema_version = 1` and only the key being set. Effective precedence,
high to low, is environment (`ORX_RUNTIME_MAX_PARALLEL`,
`ORX_RUNTIME_COMMAND_TIMEOUT_SEC`), project, user, then built-in defaults.

## Agent discovery

```sh
orx agent list [--json]
orx agent info <harness> [--json]
orx agent probe <harness> [--json]
```

`list` shows the adapter harnesses (`codex`, `cursor`, `shell`) and the
host-only `zcode` harness. `info` reads the latest capability snapshot (it
does not probe) and prints that harness's launch contract. `probe` runs the
shared local checks — binary, version, help, auth, models — and writes
`probes/<harness>.json` under the user data directory. A probe never
launches a completion. Only `codex` and `cursor` are probeable; `shell` and
host-only `zcode` exit 1. Unknown harness names exit 1; a missing argument
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
[docs/observability-contract.md](docs/observability-contract.md): the v7
tables, join keys, and the SQL for the six report dimensions. That
contract is read-only. The analysis layer itself is not in this
repository.

## Key invariants

- `task complete` means execution finished, not that the task passed. Only
  verification passing produces `passed`; every active-revision task must be
  `passed` for the Run to be `done`.
- A replan takes effect only through the shared precheck gate; a failed
  precheck leaves the previous revision active (see below). Replan is
  rejected while any task is `running` or `verifying`; a new revision
  cancels the old revision's unfinished tasks and preserves terminal
  results as recorded facts.
- `unknown` resource status stays routable; only `unavailable`/`exhausted` are
  skipped; `constrained` is a last resort. Resource status never rewrites TOML
  ordering.
- An explicitly pinned `--profile` never silently falls back.
- Deep planning refuses classes below `frontier` unless policy allows it.
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

## Docs

- [职责与边界](docs/responsibilities.md) — responsibility baseline, scope
  decisions, and current implementation limits
- [CONTEXT.md](CONTEXT.md) — Goal, Run, Task, agent roles, and related terms
- [Routing strategy](docs/routing-strategy.md) — model tiering convention,
  role × class × effort defaults, V0–V3 validation layers, escalation ladder
- `docs/m0-plan.md` — the frozen implementation baseline + amendment log
- `docs/m0-phase2-checkpoint.md` — core kernel checkpoint + review gate
- `docs/m0-report.md` — M0 acceptance log (commands and outputs)
- [Observability read contract](docs/observability-contract.md) — schema v7
  tables, joins, and read-only SQL for external analysis
- [Replan contract](docs/replan-contract.md) — the declared old<->new
  correspondence, work classifications, artifact provenance, and the
  structural-check vs semantic-review boundary

## M1 layered configuration

User layer (preferred home for profile definitions):

    ~/.config/orx/config.toml      user config (optional)
    ~/.config/orx/profiles.toml    user profiles
    ~/.local/share/orx/            user data dir (probes, snapshots)

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` are respected; `ORX_CONFIG_DIR` /
`ORX_DATA_DIR` override absolutely. Project `.orx/` keeps `config.toml`
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
