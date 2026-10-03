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
`retry <cooldown_until>`. `reset <quota_reset_at>` appears when a quota
reset time is known. A manual `orx resource set` override is marked
`override`; `orx resource clear` drops that mark. `--json` is the usual
`{"ok": true, ...}` envelope; `profiles` carries the same fields. A missing
project exits 1.

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

## Key invariants

- `task complete` means execution finished, not that the task passed. Only
  verification passing produces `passed`; every active-revision task must be
  `passed` for the Run to be `done`.
- Replan is rejected while any task is `running` or `verifying`; a new revision
  cancels the old revision's unfinished tasks.
- `unknown` resource status stays routable; only `unavailable`/`exhausted` are
  skipped; `constrained` is a last resort. Resource status never rewrites TOML
  ordering.
- An explicitly pinned `--profile` never silently falls back.
- Deep planning refuses classes below `frontier` unless policy allows it.
- M0 write execution is serial: effective CLI parallelism is 1 (one process
  per `orx run`; more are reported as `deferred`).

## Skills / update

```sh
orx skill install   # ~/.agents/skills + symlinks into ~/.zcode|~/.cursor|~/.codex
orx skill update
orx update --check  # install source + upgrade path (uv tool installs only)
```

## Docs

- [职责与边界](docs/responsibilities.md) — responsibility baseline, scope
  decisions, and current implementation limits
- [CONTEXT.md](CONTEXT.md) — Goal, Run, Task, agent roles, and related terms
- [Routing strategy](docs/routing-strategy.md) — model tiering convention,
  role × class × effort defaults, V0–V3 validation layers, escalation ladder
- `docs/m0-plan.md` — the frozen implementation baseline + amendment log
- `docs/m0-phase2-checkpoint.md` — core kernel checkpoint + review gate
- `docs/m0-report.md` — M0 acceptance log (commands and outputs)

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
