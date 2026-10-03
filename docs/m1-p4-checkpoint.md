# M1 P4 checkpoint: agent health — 2026-10-03

Phase gate deliverable (revision 7 of Run R001, tasks T001–T004).

## Shipped

- **Schema v2** (backup-replace-restore migration, rows preserved — proven
  by a hand-built v1 database test): resource_status recreated with the
  8-state CHECK (abundant/available/constrained/exhausted/unavailable/
  unknown/cooldown/auth_required) plus last_success_at, last_failure_at,
  failure_streak, last_error_kind, cooldown_until, quota_reset_at,
  last_probe_at, override. Migration fix found by that test: checkpoint +
  sidecar cleanup so no `.migrate-*-wal` lingers.
- **`records.ErrorKind`** + **`adapters.base.classify_failure`** with the
  ordered evidence pattern table (real-run strings: cursor "Cannot use this
  model", codex invalid_json_schema, "Connection lost"; default
  process_failure).
- **`src/orx/health.py` auto-learning** wired into all three CLI launch
  paths (planner/worker/verifier): success→available; AUTH_REQUIRED→
  auth_required; QUOTA_EXHAUSTED→exhausted(+reset); RATE_LIMITED→cooldown
  (60s·2^min(streak,5)+jitter); TEMPORARY needs 3 consecutive; non-gating
  kinds record only. Override rows are never clobbered;
  `orx resource clear` re-enables learning.
- **Routing gates**: auth_required non-routable; cooldown rejected only
  while now < cooldown_until, then re-learns.
- **`orx agent status`** implemented by the cursor-strong worker (grok-4.7,
  first-try pass): PROFILE/STATE/SINCE/REASON with retry times and override
  marks; already showing real auto-learned `available` states for both
  cursor profiles from this run's own audited attempts.

## Live evidence (checklist item 5)

Injected failures against the live project (then restored):

    codex-frontier   cooldown       rate_limited; retry 2026-10-03T11:04:15+00:00
    cursor-strong    auth_required  auth_required
    $ orx replan --profile cursor-strong -> rejected (auth_required), zero completions

Plus the e2e regression: a rate-limiting fake worker drives its profile to
cooldown and the next `run_slice` falls through to the configured fallback.

## Findings fixed in-phase

- resource_learn silently dropped unseeded profiles (UPDATE miss) → upsert.
- Migration sidecar leak (above).

## Test commands and results

- `uv run pytest -q` → **275 passed** (P3: 263; +12 in P4)

## M1 checklist status after P4

Items 2, 3, 4, 5 **passed**. Item 1 partial (275). Items 6–7 pending
(P5–P6); item 8 at P7.
