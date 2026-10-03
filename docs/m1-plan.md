# ORX M1 Plan: Runtime, Observability & Inbox

Status: frozen baseline (2026-10-03). Amendments append at the bottom with
dates; the baseline text above them never rewrites silently. This document
is the authority for M1 scope and contracts, in the same spirit as
`docs/m0-plan.md` for M0.

M1 is developed **through ORX itself** (dogfood): one Goal carries the whole
milestone; each phase ends with a replan (new revision supersedes the old),
a checkpoint report, user approval, and a git commit. Phase boundaries are
hard stops.

## Goals

From the M1 directive, in priority order — each phase lands one capability:

1. **User layer config** (`~/.config/orx/` + `~/.local/share/orx/`, layered
   precedence, `orx config` commands).
2. **Agent discovery** (`orx agent list/info/probe`, capability snapshots,
   zero completions).
3. **Timeline** (`orx timeline` over the existing event tables).
4. **Agent health** (error taxonomy, auto-learned states, cooldown gating,
   `orx agent status`).
5. **Usage observation** (`orx usage`, accuracy-labeled, unknown is legal).
6. **Inbox + GitHub source** (`orx inbox`, `orx watch`, `gh` delegation,
   `orx auth status`).

## Non-goals (deferred to M2+)

Linear, webhook/listener server, in-process automatic retry loops, adapter
installer manifests (`adapters/*.toml`), worktree parallelism, per-CLI token
management for agent harnesses (ORX never touches Codex/Cursor credentials —
probe only).

## M0 invariants that M1 must not break

- Exit codes 0 success / 1 domain error / 2 usage. `--json` envelope on every
  command (`{"ok": true, ...}` / `{"ok": false, "error": ..., "errors": [...]}`).
- Status payloads use `objective`, never `title`.
- One active Goal per project; run `done` only when every active-revision
  task is `passed`; every Goal acceptance string appears verbatim in some
  task acceptance list.
- One idempotent verification executor; CLI verifier verdict contract is one
  final `ORX_VERDICT=pass|fail` line (read from the extracted agent message
  first, raw streams as fallback).
- Doctor's flag sets stay exactly the adapter Required sets; probes never
  launch completions.
- Profiles define in TOML, runtime state lives in SQLite; loading config
  never rewrites runtime state.
- SQLite: refuse missing/newer `schema_version`; migrations are additive
  functions keyed by integer version with backup-replace-restore.
- Unknown config keys are ignored (forward compat); unknown values are load
  errors.

## Dogfood protocol

- ORX project root = this repo (`.orx/`; `state.db` and `runs/` are git-
  ignored, config/profiles TOML are committed).
- The M1 Goal has two stable acceptance criteria, so **every** revision can
  carry them verbatim (per-revision coverage rule):
  1. `uv run pytest -q exits 0`
  2. `docs/m1-plan.md M1 acceptance checklist passes end to end`
  Each revision = one phase's tasks + a final regression task (host) whose
  acceptance list carries both strings and whose verification runs pytest.
- Routing: planner `codex-frontier`; workers `codex-frontier` primary /
  `cursor-strong` fallback; agent verifications `cursor-economy`; design-
  sensitive work (config layering semantics, health state machine, routing
  gates, schema migrations, per-phase review/integration) routes to
  `orx-host` and is done by the host agent.
- Budget: ~30–50 paid CLI calls across M1; each phase capped; host may
  absorb tasks on request.

## Phase breakdown

### P0 — plan freeze + bootstrap + git baseline

- This file; `.gitignore` (`.orx/state.db*`, `.orx/runs/`, caches); baseline
  commit of the M0 + real-run-fix state; `orx init`; project-layer profiles
  (moved to the user layer in P1); M1 Goal; revision 1 (P0–P1 tasks).

### P1 — user layer config + `orx config`

- Paths: `~/.config/orx/config.toml`, `~/.config/orx/profiles.toml`,
  data dir `~/.local/share/orx/`. `XDG_CONFIG_HOME` / `XDG_DATA_HOME`
  respected; `ORX_CONFIG_DIR` / `ORX_DATA_DIR` override absolutely.
- Precedence (high → low): CLI argument > environment > project `.orx/` >
  user layer > built-in defaults.
- Merge semantics: scalars — project wins when present; profiles — merged by
  name, same-name project profile replaces the user one; role routing lists
  (controller/plan.*/worker/verify) come from the effective config — the
  project list when the project config sets that section, else the user's.
- Commands: `orx config path [--json]`, `orx config list [--json]` (effective
  values with layer origin), `orx config get <key> [--json]`,
  `orx config set <key> <value> [--user] [--json]` (default writes the
  project layer; `--user` writes the user layer; values validated before
  write; never rewrites `schema_version`). `orx profiles` annotates each
  profile with its layer.
- P0's project-layer profiles move to `~/.config/orx/profiles.toml`; the
  project layer keeps only routing.

### P2 — agent discovery

- Shared probe module extracted from doctor's harness checks (doctor output
  unchanged, flags ≡ Required sets).
- `orx agent list` — harnesses with adapters (codex/cursor/shell) plus the
  host-only `zcode`.
- `orx agent probe <harness> [--json]` — runs binary/version/help/auth/models
  probes and writes a capability snapshot:
  `{harness, binary, version, probed_at, features{headless, json_output,
  model_selection, effort_selection, resume}, auth, models_discoverable}`.
  Never launches a completion. Snapshot persisted at
  `~/.local/share/orx/probes/<harness>.json`.
- `orx agent info <harness> [--json]` — latest snapshot + the adapter's
  launch contract summary.

### P3 — timeline

- Read-only model over existing tables: `task_events` + `attempts` +
  `verifications` + `planning_assignments` + `routing_decisions` + goal/run
  rows. No new tables in v1.
- `orx timeline [--run R###] [--task T###] [--profile NAME] [--limit N]
  [--json]` — strictly ordered by time; human rendering is
  `HH:MM:SS  actor  event  detail`.

### P4 — agent health

- `records.ErrorKind`: AUTH_REQUIRED, RATE_LIMITED, QUOTA_EXHAUSTED,
  TEMPORARY_FAILURE, MODEL_UNAVAILABLE, CONTEXT_EXCEEDED, INVALID_REQUEST,
  PROCESS_FAILURE, CANCELLED.
- `ResourceStatus` gains `cooldown` and `auth_required`. Schema v2 migration
  (backup-replace-restore) extends `resource_status` with: `last_success_at`,
  `last_failure_at`, `failure_streak`, `last_error_kind`, `cooldown_until`,
  `quota_reset_at`, `last_probe_at`, `override INTEGER DEFAULT 0`.
- Adapters grow `classify_failure(run_result) -> ErrorKind | None` (stderr
  pattern tables; evidence from real runs: cursor "Cannot use this model" →
  MODEL_UNAVAILABLE, codex 400 `invalid_json_schema` → INVALID_REQUEST,
  "rate limit" → RATE_LIMITED, auth errors → AUTH_REQUIRED). Default
  PROCESS_FAILURE for unknown non-zero exits.
- Health auto-learning in dispatch after every CLI attempt: success →
  available, streak 0; AUTH_REQUIRED → auth_required; QUOTA_EXHAUSTED →
  exhausted (+ `quota_reset_at` when parseable); RATE_LIMITED → cooldown
  until now + exponential backoff by streak (+ jitter); TEMPORARY_FAILURE →
  streak++, cooldown only after 3 consecutive. Auto-learning never
  overwrites rows with `override = 1` (set by manual `orx resource set`;
  `orx resource clear <profile>` re-enables learning).
- Routing: `auth_required` always non-routable; `cooldown` non-routable while
  `now < cooldown_until` (after that it reads as its pre-cooldown state).
- `orx agent status [--json]` — per-profile health view
  (PROFILE / STATE / SINCE / REASON). No in-process retry loops in M1.

### P5 — usage observation

- Table `usage_observations`: `id, attempt_id REFERENCES attempts(id),
  profile, run_id, task_id, input_tokens NULL, output_tokens NULL,
  cached_input_tokens NULL, source (native_cli | output_estimate),
  accuracy (exact | estimated | unknown), created_at`.
- Adapters grow `usage_observation(launch, run_result)`; codex parses
  `turn.completed.usage` from the JSONL (real fixture captured 2026-10-03),
  cursor parses the result envelope's `usage`; shell reports nothing (no
  row). Rows attach to the attempt that ran.
- `orx usage [--profile] [--json]` — per-profile aggregates (tasks, runtime,
  tokens with accuracy); runtime and task counts outrank token precision.

### P6 — inbox + GitHub source + watch

- Tables: `external_events (id, source, external_id, kind, payload_json,
  fetched_at, UNIQUE(source, external_id))`; `inbox_items (id, event_id
  REFERENCES external_events(id), title, body, url, status
  (pending|accepted|rejected|dismissed), goal_id NULL, created_at,
  decided_at NULL)`.
- GithubSource lists issues via `gh issue list --json` (needs `gh` present
  and authed; ORX stores no GitHub token — `gh` owns credentials). Phase
  precheck: if `gh` is missing, tables + CLI land first and the GitHub
  source is deferred without blocking.
- Policy in config `[inbox]`: `github_labels = ["orx"]`, `auto_accept =
  false` (manual by default).
- `orx inbox list/show/accept/reject [--json]`; accept creates a Goal (loud
  error when a Goal is active — one-active-Goal invariant) and links
  `goal_id`.
- `orx watch [--once] [--interval SEC] [--json]` — poll → normalize →
  dedupe on (source, external_id) → inbox. `--once` = single pass.
- `orx auth status [--json]` — delegates to `gh auth status` (display only;
  no login/logout in M1).

### P7 — CLI polish + acceptance

- `orx completion (zsh|bash)` — emits a completion script; root app flips
  `add_completion` accordingly; exit codes unchanged.
- Help audit: every command's `--help` is self-sufficient for an agent
  caller (machine-doc principle).
- `docs/references/references.toml` + `docs/references/reference-radar.md`
  — repo engineering process (watched repos + areas), never runtime.
- `docs/m1-report.md` — acceptance transcript from real runs; full
  regression; the dogfood Goal reaches `done`.

## M1 acceptance checklist (milestone gate)

1. `uv run pytest -q` exits 0 (suite grows from 220 to 320+).
2. `orx config get/set/list/path --json` round-trips; layer precedence is
   proven by tests.
3. `orx agent probe codex --json` emits a capability snapshot and launches
   zero completions.
4. `orx timeline --json` is non-empty and strictly time-ordered.
5. `orx agent status` shows cooldown/auth_required transitions driven by
   injected failures.
6. `orx usage --json` reports per-profile rows with an `accuracy` field
   (codex rows are `exact`, parsed from real output).
7. `orx inbox list --json` shows a source item; accepting it creates a Goal.
8. `docs/m1-report.md` exists and contains the acceptance transcript.

## Risks

- Codex/Cursor CLI drift → probe snapshots + tolerant parsing (the pattern
  proven during the post-M0 real runs).
- `gh` missing → P6 splits gracefully.
- Paid-call overrun → per-phase caps; host absorbs tasks on request.
- Schema v2 migration risk → existing backup-replace-restore framework;
  migration has its own tests.

## M1 amendments (parallel orchestration, 2026-10-03)

Applied mid-milestone after P4 landed (P1–P4 complete on the main dogfood
chain; schema v2 already in main, so the original "front-load v2" idea is
moot). Protocol, user-approved:

- **P5 (usage)** stays on the main ORX dogfood chain (planner/worker/verify
  through `orx`), landing schema v3 (`usage_observations`).
- **P6 (inbox)** runs as a **host-fanout worktree**: a branch off main,
  disjoint scope, built in parallel; inside ORX its tasks route `host-work`
  (host claims them, completes with merge evidence). P6 owns schema v4
  (`external_events`, `inbox_items`) — renumbered from 3 at merge, after
  P5's v3 lands. Migrations stay single-writer per number.
- Conflict surfaces: `cli.py` sub-app registrations are append-only;
  `state.py` MIGRATIONS dict is the only ordered merge point.
- P7 stays last: full regression + acceptance transcript on main.
- Recorded as M2 candidates: "scope-disjoint CLI parallelism + worktree
  driver" (parallel admission via disjoint `scope.allowed`), and
  `replan --context-file` (finding #5).
