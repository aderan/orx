# M1 P2 checkpoint: agent discovery — 2026-10-03

Phase gate deliverable (revision 4 of Run R001, tasks T001–T003).

## Shipped

- **`src/orx/probes.py`** — shared probe module: harness registry
  (codex, cursor, shell-generic, zcode-host-only), `capability_snapshot()`
  with the frozen shape (harness, binary, version, probed_at, features
  {headless, json_output, model_selection, effort_selection, resume}, auth,
  models_discoverable), persistence at `<user data dir>/probes/<harness>.json`,
  `load_snapshot`. Probes are local-only — zero completions, ever. doctor's
  inline probing removed; it consumes `doctor_harness_checks()` with
  byte-compatible output.
- **`orx agent list | info <harness> | probe <harness>`** implemented by the
  cursor-strong CLI worker (grok-4.7, one attempt, all verifications green):
  list marks zcode host-only; info reads the persisted snapshot plus the
  launch contract; probe writes the snapshot; M0 JSON envelope + exit codes.
- Real evidence: `orx agent probe codex --json` against the real binary —
  codex-cli 0.160.0, auth logged_in, models_discoverable true, snapshot at
  ~/.local/share/orx/probes/codex.json. `orx agent probe cursor` equivalent.

## Findings fixed in-phase (dogfood)

5. **Phase intent is invisible to the planner**: the codex replan drafted
   "P1 hardening" because the frozen Goal context says "Current phase: P1".
   Controller corrected via host-authored revision 4. M2 candidate:
   `orx replan --context-file` or a mutable goal-progress note.
6. **Auth misdetection (latent M0 bug)**: "Not logged in" contains the
   positive marker — doctor misreported logged-out harnesses as logged_in.
   Negation now wins in `probes._auth_from`.
7. **effort_selection false negative**: codex exposes effort as a `-c`
   config key absent from --help; the local catalog's
   supported_reasoning_levels is the real signal (both heuristics now feed
   the feature flag; real codex probe reports true).

## Test commands and results

- `uv run pytest -q` → **261 passed** (P1: 246; +15 in P2)
- `uv run orx agent probe codex --json` → ok, snapshot persisted (item 3
  of the M1 checklist: PASS, zero completions)

## M1 checklist status after P2

Items 2 (config) and 3 (agent probe) **passed**. Item 1 partial (suite 261).
Items 4–7 pending (P3–P6); item 8 at P7.
