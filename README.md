# ORX

ORX orchestrates host and CLI coding agents against a single authoritative
plan. SQLite is the state authority; TOML holds only static configuration.

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

- `docs/m0-plan.md` — the frozen implementation baseline + amendment log
- `docs/m0-phase2-checkpoint.md` — core kernel checkpoint + review gate
- `docs/m0-report.md` — M0 acceptance log (commands and outputs)
