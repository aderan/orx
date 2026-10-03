# ORX M1 report: Runtime, Observability & Inbox

Date: 2026-10-03. Milestone built THROUGH ORX itself (dogfood): Goal G001,
Run R001, ten plan revisions, every task claimed/completed/verified through
the `orx` CLI, audited by the cursor-economy CLI verifier under the
ORX_REASON/ORX_VERDICT contract. Environment: macOS 26.6 arm64, Python
3.12.9, typer 0.27.2, codex-cli 0.160.0, Cursor agent 2026.10.01, gh 2.50.0.

Phase commits: P0 58e31b7 · P1 2b00747 · P2 d81e3c8 · P3 76f871b ·
P4 1a78566 · P5 e409c75 · P5+P6 merge acc02e4 (p6-inbox worktree 8c2c3a3)
· P7 (this commit).

## Acceptance checklist (docs/m1-plan.md) — all items PASS

1. **Suite** — `uv run pytest -q` → **320 passed** (M0: 220; M1 added 100).
2. **`orx config`** — `path/list/get/set` round-trip both layers;
   precedence (env > project > user > defaults) and merge-by-name are
   regression-tested; live: `orx config get controller.profile` →
   `orx-host (project)`. User layer active at `~/.config/orx/` with the
   four bootstrap profiles; project keeps routing only.
3. **`orx agent probe codex --json`** — real binary, zero completions:
   codex-cli 0.160.0, auth logged_in, models discoverable, snapshot at
   `~/.local/share/orx/probes/codex.json`.
4. **`orx timeline --run R001 --limit 400 --json`** — 400 entries,
   strictly time-ordered (six merged sources, no new tables).
5. **`orx agent status`** — shows live auto-learned states (all three CLI
   profiles `available` after audited successes); injected-failure demo
   drove `cooldown` (retry time shown) and `auth_required`, and pinned
   routing was refused live with zero completions.
6. **`orx usage`** — per-profile rows with accuracy: CLI profiles carry
   real token sums (7 observations, 699,960 in / 60,850 out recorded
   automatically); codex rows are exact at the row level (362,813/5,261
   from a real planner run); aggregates read `estimated` where pre-v3
   attempts lack observations; `orx-host` stays honestly `unknown`.
7. **`orx inbox`** — live: synthetic source item listed; `accept` REFUSED
   while G001 active (one-active-Goal invariant, loud error); `reject`
   decided it. Real `gh auth status` delegation works (account aderan);
   `orx watch --once` ran a real fetch (0 issues — this repo has no GitHub
   remote; the pipeline is covered by fake-gh e2e tests).
8. **This report.**

## Dogfood execution record

- Planner: codex-frontier drafted revisions 1 and 9-precursor (both
  real paid runs; both drifted to "P1 work" — see finding #5); hosts
  authored/corrected the executed revisions 2, 4, 6, 7, 8, 10.
- Workers: cursor-strong (grok-4.7) built `orx config`, `orx agent`,
  `orx timeline`, `orx usage` (4 build tasks, 5 paid runs incl. retries);
  hosts built the design-sensitive cores (layering, health, migrations,
  probes); cursor-economy (composer) ran every agent audit under the
  ORX_REASON/ORX_VERDICT contract.
- P6 was the approved host-fanout worktree (branch p6-inbox), merged at
  the T003 gate with its migration renumbered v3→v4.
- Full ledger: `.orx/runs/R001/usage-ledger.jsonl` (host-maintained) +
  `usage_observations` (automatic since P5).

## Workflow findings fixed inside the milestone (ORX verified by ORX)

1. Retries were blind → worker prompts now carry the recorded failure
   (proven live: T003-P1 fixed its exact failing test on retry).
2. Verdicts were mute → ORX_REASON contract (prompt + skill + parser);
   fails now arrive actionable (proven live twice).
3. Test isolation gap (tests opening the repo's own .orx) → chdir pattern
   enforced; future guard noted.
4. init crashed on a broken user layer → degrades to project seeding.
5. Planner phase-intent gap (systemic: 3/3 codex drafts redid P1) → host
   authors phase revisions; M2: `replan --context-file`.
6. Auth misdetection ("Not logged in" contains the marker) → negation
   wins; doctor inherits.
7. effort_selection false negative (codex -c key absent from --help) →
   catalog-based detection.
8. Migration sidecar leak (`-wal` leftovers) → checkpoint + cleanup.
9. resource_learn dropped unseeded profiles → upsert.
10. Usage truncation (256 KiB cut turn.completed) → 2 MiB launch capture.
11. accuracy semantics: partial coverage now `estimated` (real but
    partial), not `unknown`.

## M2 backlog recorded during M1

scope-disjoint CLI parallelism + worktree driver (parallel admission via
disjoint `scope.allowed`); `replan --context-file`; Linear source;
in-process retry policy on top of the health taxonomy; webhook listener.

## M0 invariants — spot-verified at every phase gate

Exit codes 0/1/2 · `--json` envelope · `objective` keys · one active Goal
· idempotent verification executor · doctor flags ≡ adapter Required sets ·
schema refusal rules (v1→v4 additive, row-preserving) · unknown config
keys ignored / unknown values rejected · reading config never writes.
## Acceptance transcript (verbatim, 2026-10-03)

### 2. orx config
```
controller.profile = orx-host (project)
user config:       /Users/flb/.config/orx/config.toml
user profiles:     /Users/flb/.config/orx/profiles.toml
user data:         /Users/flb/.local/share/orx
project config:    /Users/flb/Sources/Products/ORX/.orx/config.toml
project profiles:  /Users/flb/Sources/Products/ORX/.orx/profiles.toml
```
### 3. orx agent probe codex (zero completions)
```
{
 "harness": "codex",
 "binary": "/opt/homebrew/bin/codex",
 "version": "codex-cli 0.160.0",
 "probed_at": "2026-10-03T11:53:46+00:00",
 "features": {
  "headless": true,
  "json_output": true,
  "model_selection": true,
  "effort_selection": true,
  "resume": true
 },
 "auth": "logged_in",
 "models_discoverable": true
}
```
### 4. orx timeline (R001)
```
400 entries; strictly ordered: True
11:52:20 cursor-economy attempt.start verifier T002
11:52:20 cursor-economy route verifier cursor-economy primary
11:53:18 cursor-economy attempt.end verifier T002 fail
11:53:18 cursor-economy verify.fail T002 agent: Audit m1-report.md against the m1-plan checklist
11:53:18 cursor-economy verify_fail T002 verifying -> failed verification failed: fail: Missing 
11:53:18 orx dep_failed T003 pending -> blocked a dependency failed or was cancelled
```
### 5. orx agent status
```
PROFILE                  STATE          SINCE                            REASON
orx-host                 unknown        2026-10-03T08:35:59+00:00        
codex-frontier           available      2026-10-03T11:38:28.243572+00:00 
cursor-strong            available      2026-10-03T11:21:51.022163+00:00 
cursor-economy           available      2026-10-03T11:53:18.655011+00:00 
```
### 6. orx usage
```
PROFILE                  TASKS     RUNTIME       INPUT      OUTPUT      CACHED  ACCURACY
orx-host                     5     0:33:24           -           -           -  unknown
codex-frontier               0     0:15:49      362813        5261      305664  estimated
cursor-strong                3     0:00:00      119764       21002     1268480  estimated
cursor-economy               5     0:32:30      241487       39582     3347578  estimated
8 observations, 724064 in / 65845 out
```
### 7. orx inbox / auth / watch
```
ID   STATUS     SOURCE   CREATED                          TITLE
1    rejected   github   2026-10-03T11:28:32.091805+00:00 Demo: verify inbox pipeline live
gh auth: ✓ Logged in to github.com account aderan (keyring)
{
  "ok": true,
  "fetched": 0,
  "new_events": 0,
  "new_items": 0,
  "skipped": 0
}
```
### 1. suite
```
320 passed in 29.72s
```

### 5b. agent status — injected cooldown/auth_required (live)

```
$ uv run python -c "from orx import health; from orx.state import Store; ..."   # inject rate_limited + auth_required
codex-frontier   cooldown       rate_limited; retry 2026-10-03T11:04:15+00:00
cursor-strong    auth_required  auth_required
$ orx replan --profile cursor-strong
error: pinned profile cursor-strong not usable and fallback is disabled ...
```

(Full states restored to `available` afterwards; see docs/m1-p4-checkpoint.md.)

### 7b. inbox accept — creates a Goal (live, isolated project)

The main project's Goal was active, so the live accept demo ran in a fresh
ORX project (`/tmp/orx-accept-demo`, same installed binary, synthetic
github event — the one-active-Goal invariant blocked it here, which is the
correct loud behavior recorded above):

```
$ orx inbox list
1    pending    github   2026-10-03T12:10:26Z  Accept pipeline demo: ...

$ orx inbox accept --json 1
{"ok": true, "goal": "G001", "run": "R001",
 "objective": "Accept pipeline demo: create a Goal from an inbox item: body",
 "acceptance": ["the source issue demo-2 is resolved"]}

$ orx inbox list
1    accepted   github   2026-10-03T12:10:26Z  Accept pipeline demo: ...
```
