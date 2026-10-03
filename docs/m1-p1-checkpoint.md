# M1 P1 checkpoint: user layer configuration — 2026-10-03

Phase gate deliverable (revision 2 of Run R001, tasks T001–T005).

## Shipped

- **Layered configuration** (`src/orx/config.py`): user layer at
  `~/.config/orx/{config,profiles}.toml` (XDG respected, `ORX_CONFIG_DIR` /
  `ORX_DATA_DIR` absolute overrides, data dir `~/.local/share/orx`);
  precedence env > project > user > built-in defaults; env allowlist
  `ORX_RUNTIME_MAX_PARALLEL` / `ORX_RUNTIME_COMMAND_TIMEOUT_SEC`; section
  lists taken whole from the highest layer that sets them; profiles merged
  by name with complete same-name replacement; `EffectiveConfig.origins` /
  `profile_origins` track provenance; reading never writes files; a broken
  user layer degrades `init` (project-layer seeding) and FAILS doctor.
- **Integration**: `open_project` builds every Project from the effective
  merge; doctor gained `user_layer` + `effective_config` checks (M0 check
  names unchanged), shell-executable checks cover user-layer profiles;
  `orx profiles` annotates each profile's layer; init seeds the effective
  profile set.
- **`orx config path | list | get | set [--user]`** implemented by the
  cursor-strong CLI worker (grok-4.7, two attempts — see workflow findings).
- **Migration**: `config.migrate_profiles_to_user()` — the collision-safe,
  idempotent host procedure; the four bootstrap profiles now live at the
  user layer, the project keeps only routing; resource rows and Goal state
  untouched (profile identity is the name).
- README section "M1 layered configuration".

## Test commands and results

- `uv run pytest -q` → **246 passed** (M0: 220; +26 in P1)
- `orx doctor` → 0 failures (user_layer + effective_config ok)
- `orx config path | list | get controller.profile | profiles` all exercised
  live; `orx config set` round-trips both layers (worker's test suite)

## Dogfood execution record (per Controller directive)

| task | executor | attempts | paid calls |
|---|---|---|---|
| T001 layering core | host | 1 | composer audit ×1 |
| T002 integration | host | 1 | composer audit ×1 |
| T003 `orx config` | cursor-strong (grok-4.7-medium) | 2 (fail → retry pass) | grok ×2, composer audit ×1 |
| T004 migration | host | 3 (audit fail → remediate → pass) | composer audit ×3 |
| T005 regression+gate | host | 1 | composer audit ×1 |

Token/run ledger: `.orx/runs/R001/usage-ledger.jsonl` (per-call model,
duration, input/output/cached tokens; source native_cli, accuracy exact).

## Workflow findings fixed in-phase (ORX verified by its own development)

1. **Retries were blind**: `task retry` cleared failure_reason and re-sent
   the identical worker prompt. Fixed: `worker_prompt` now embeds the most
   recent recorded failure (from `task_events`) as "A previous attempt
   FAILED with ... Fix that specific problem". Proven live: T003's second
   attempt fixed the exact failing test.
2. **Fails were mute**: the verifier contract required a verdict but not a
   reason, so `ORX_VERDICT=fail` arrived with nothing actionable. Fixed:
   the contract (prompt + orx-agent skill) now requires `ORX_REASON=` before
   the verdict; dispatch records `fail: <reason>`. Proven live: T004's
   audit fail named the missing tests; remediation passed re-audit.
3. **Test isolation gap**: a new test opened the ORX repo's own `.orx/`
   (cwd resolution) instead of its temp project; it had been silently
   passing against the dogfood project. Fixed by the conftest chdir pattern;
   noted as a future ORX guard (refuse .orx/ above the project root in
   tests, or `--project` flag).
4. `init` initially crashed on a broken user layer (unimported name in the
   fallback); now degrades to project-layer seeding, doctor reports the
   layered failure (regression-tested).

## M1 checklist status after P1

Item 2 (`orx config` round-trip + precedence proven by tests): **passed**.
Items 1, 8 (suite size, m1-report): partial (246 tests; report lands at P7).
Items 3–7: pending their phases (P2–P6). The Goal stays active; P2 planning
is the next Controller step.
