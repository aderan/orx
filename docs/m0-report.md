# ORX M0 report

Date: 2026-10-03. Environment: macOS 26.6.2 arm64, Python 3.12.9, uv 0.8.2,
codex 0.160.0 (logged in, never invoked for a completion), Cursor `agent`
2026.10.01, `orx` 0.1.0 installed with `uv tool install -e .` from this
checkout (`/Users/flb/.local/bin/orx`).

Every step below ran against the real CLI in a fresh temporary git repo
(`/tmp/orx-accept`). No paid model was called at any point: the "Codex-shaped"
planner is a fake `codex` shell script on PATH that answers the local probes
(`exec --help`, `debug models`) and writes a Plan IR file; the CLI worker is a
fake shell harness script. This is exactly the plan's acceptance design.

Test suite at report time: `uv run pytest -q` → **201 passed** (includes
adapter fake-process tests, skills, update).

## Step-by-step log

### 1. `orx version`, `orx doctor`

```
$ orx version
orx 0.1.0

$ orx doctor            # outside a project: honest failure
✓ python: 3.12.9
✓ uv: on PATH
✓ git: on PATH
✓ git_repo: inside a git repository
✓ codex: found at /tmp/orx-accept/bin/codex; flags present; auth not_logged_in; operational probe not_run
✓ agent: found at /Users/flb/.local/bin/agent; flags present; auth logged_in; operational probe not_run
✗ project: no .orx/ found; run `orx init` here
1 failure(s), 0 warning(s)
```

Doctor found the fake `codex` first on PATH and reported its (lack of) auth
without launching anything — "binary-found is never printed as works".

### 2. `orx init` produces the three `.orx` files; doctor accepts them

```
$ orx init --json
{"ok": true, "root": "/private/tmp/orx-accept",
 "created": [".orx/config.toml", ".orx/profiles.toml", ".orx/state.db"]}

doctor ok: True | failures: 0 | warnings: 0
```

Custom acceptance config then written: `[plan.standard] = ["host-planner",
"fake-codex-planner"]`, `[worker] = ["host-worker", "fake-shell-worker"]`
(fake-shell-worker: harness=shell, executable=/tmp/orx-accept/fake-worker.sh,
prompt_transport=stdin; fake-codex-planner: harness=codex, driver=cli, model
fake-gpt).

### 3. Goal: small feature with a failing test already in the repo

```
$ python3 -m pytest -q test_feature.py
E   ModuleNotFoundError: No module named 'feature'
1 error in 0.04s

$ orx goal new --json --objective "Implement the greet feature so the failing test passes" \
    --acceptance "pytest exits 0 on test_feature.py" \
    --acceptance "artifact.txt contains a line from the fake worker" \
    --context "tiny demo repo"
{"ok": true, "goal": {"id": "G001", ..., "status": "active"}, "run": {"id": "R001", "status": "planning"}}
```

### 4. Standard plan → `host_required`; valid Plan IR submitted

```
mode: host_required | profile: host-planner | depth: standard | assignment: P001

$ orx plan submit --json --file plan.json
{"ok": true, "revision": 1, "depth": "standard", "planner_profile": "host-planner",
 "tasks": 2, "superseded_revision": null, "cancelled_tasks": [], "assignment": "P001"}
```

State contains revision 1 with tasks T001 (host) → T002 (fake CLI, depends on
T001).

### 5. Host task claimed/completed; shell task started by `orx run`

```
orx run -> host_required: ['T001']        # T002 stays pending (dependency)
$ orx task claim --json T001
{"ok": true, "task": "T001", "status": "running", "attempt": 2, "driver": "host"}
```

### 6. Verification fails → task `failed` not `passed`; fix; retry; passes

```
T001 premature complete -> status=failed verdict=failed failed_checks=1
   # (host completed without implementing; `python3 -m pytest -q` exits non-zero)

$ printf 'def greet(name: str) -> str:\n    return f"hello, {name}"\n' > feature.py
$ orx task retry --json T001
{"ok": true, "task": "T001", "status": "runnable"}
$ orx run && orx task claim T001
T001 after fix -> status=passed verdict=passed

orx run started T002: via fake-shell-worker -> passed (actual_effort=high source=reported)
$ cat artifact.txt
artifact line from fake-worker
```

The shell adapter parsed the worker's `ORX_ACTUAL_EFFORT=high` line and the
attempt records `actual_effort=high, effort_source=reported`.

### 7. New process shows the same states

```
$ orx status
Goal G001 [done]: Implement the greet feature so the failing test passes
Run R001: DONE
Plan: revision 1 (standard, planner host-planner) [active]
  ✓ T001 passed           Implement feature.greet so the test passes  (host-worker)
  ✓ T002 passed           Produce the artifact via the fake CLI worker  (fake-shell-worker)
Verification: 2 passed / 0 failed / 0 awaiting agent
Result: DONE
```

Every `orx` invocation above is a separate process; SQLite is the authority.

### 8. Replan through a fake Codex-shaped adapter

```
$ orx replan --json --profile fake-codex-planner
replan: mode=completed planner=fake-codex-planner revision=2 tasks=2 superseded=1 cancelled=[]

sqlite> SELECT id, role, profile, driver, harness, actual_effort, effort_source, result
        FROM attempts WHERE role='planner' AND driver='cli' ORDER BY id DESC LIMIT 1;
5|planner|fake-codex-planner|cli|codex|high|reported|completed

sqlite> SELECT revision, depth, planner_profile, status FROM plan_revisions ORDER BY revision;
1|standard|host-planner|superseded
2|standard|fake-codex-planner|active
```

The adapter built the real Codex argv (`codex exec --json -m fake-gpt -C <root>
-s workspace-write --ephemeral --output-last-message <f>
-c model_reasoning_effort=high <prompt>`), validated `deep→high` against the
fake `codex debug models` catalog, read the Plan IR from the last-message
file, and submitted it. Revision 1 superseded; its tasks were all passed so
nothing was cancelled. Revision 2 was then driven to done the same way
(T101 host → passed, T102 fake shell → passed; `run=done goal=done`).

### 9. Primary set `unavailable` → configured fallback; routing row says why

```
$ orx resource set --json host-planner unavailable --note "demo quota"
{"ok": true, "profile": "host-planner", "status": "unavailable", "note": "demo quota"}

plan after primary unavailable: mode=completed selected=fake-codex-planner reason=fallback revision=3

sqlite> SELECT role, selected, reason, downgrade_blocked, candidates_json
        FROM routing_decisions ORDER BY id DESC LIMIT 1;
planner|fake-codex-planner|fallback|0|
[{"profile": "host-planner", "resource_status": "unavailable", "class": "strong",
  "kept": false, "reject_reason": "unavailable"},
 {"profile": "fake-codex-planner", "resource_status": "unknown", "class": "frontier",
  "kept": true, "reject_reason": null}]
```

### 10. Run reaches `done` only after every active task is `passed`

```
rev3 before work: run=running tasks=[('T101', 'runnable'), ('T102', 'pending')]
T101 -> passed (run still not done: T102 pending)
mid-rev3: run=running tasks=[('T101', 'passed'), ('T102', 'runnable')]
T102 started via fake-shell-worker -> passed

$ orx status
Goal G001 [done]: Implement the greet feature so the failing test passes
Run R001: DONE
Plan: revision 3 (standard, planner fake-codex-planner) [active]
  ✓ T101 passed           Keep greet implemented so pytest passes  (host-worker)
  ✓ T102 passed           Run the fake CLI worker again for the artifact  (fake-shell-worker)
Verification: 2 passed / 0 failed / 0 awaiting agent
Result: DONE
```

## Extras verified along the way

- `orx skill install` installed `orx-controller` and `orx-agent` to
  `~/.agents/skills/` and symlinked them into `~/.zcode/skills`,
  `~/.cursor/skills`, and `~/.codex/skills` (all pre-existing).
- `orx update --check` on this machine reports
  `orx 0.1.0 via editable (uv tool editable install)` with no upgrade command
  (editable installs are not self-upgradable — by design).
- Doctor on the fake `codex` reported `auth not_logged_in` and never ran a
  completion; on the real `agent` it reported `auth logged_in` with
  `operational probe not_run`.

## M0 limits (unchanged from the plan)

- CLI parallelism is effectively 1 (one process per `orx run` invocation);
  further runnable tasks are reported as `deferred`.
- Resource status is manual.
- No worktree isolation; one shared working tree.
- Real Codex/Cursor runs were never launched: the adapters implement the
  probed argv surface, and everything executable in tests/acceptance is a
  local fake. The first real paid run is a deliberate post-M0 step, and the
  exact Codex JSONL effort field is then confirmed against a captured fixture
  (parsing is already tolerant of `model_reasoning_effort` /
  `reasoning_effort` / `effort`, flat or nested).

## Post-M0: first real paid run (2026-10-03, same day)

The deliberate post-M0 step ran the same day. Environment: codex-cli 0.160.0
logged in with ChatGPT, model `gpt-6.1-sol` (user default; catalog levels
low–ultra), fresh repo `/tmp/orx-codex-real` with `codex-planner` /
`codex-worker` profiles (effort `standard`, so ORX passes
`-c model_reasoning_effort=medium` against the user config's `high`).

Outcome — first real completion and first real e2e run both succeeded:

- `orx plan` → real Codex planner, 42.7 s, valid Plan IR, revision 1 with 1
  task (planner_profile persisted as `codex-planner`).
- `orx run` → real Codex worker, 38.3 s, wrote `feature.py`, deterministic
  verification passed, task `passed`, run `done`, goal `done`.
  Worker attempt: `actual_effort=medium, effort_source=requested_validated`.
- Real token usage (from `turn.completed`): input 73844 / cached 63232 /
  output 308. The model discovered and used the installed `orx-agent` skill.

**Effort field confirmed (the deliberate open question):** the 0.160.0 exec
JSONL event stream — `thread.started`, `item.started/completed`
(`agent_message`, `command_execution`, `file_change`, `error`), `turn.started`,
`turn.completed` (usage only) — carries **no effort field at all**. So
`requested_validated` (the catalog-validated `-c` value) is the strongest
available source on 0.160, which is exactly what the adapter records. The
tolerant scan stays as forward-proofing. Trimmed fixture:
`tests/fixtures/codex-0.160-worker-exec.jsonl`.

The first real calls also surfaced and fixed three real defects plus one
audit gap (all regression-tested; suite 201 → **213 passed**):

1. **Planner `--output-schema` rejected by the API** (HTTP 400
   `invalid_json_schema ... 'additionalProperties' is required to be
   supplied and to be false`): pydantic's plain `model_json_schema()` is not
   a strict schema. `plan.strict_json_schema()` now rewrites it recursively
   (`additionalProperties: false` everywhere, every property in `required`
   including defaulted ones). The failed attempt was rejected pre-billing.
2. **Launches inherited the controller's stdin**: `codex exec` reads non-TTY
   stdin as additional prompt input. Codex and Cursor launches now pass an
   explicitly empty stdin.
3. **Model catalog read corrupted by stream truncation**: `codex debug
   models` emits >256 KiB; the default STREAM_LIMIT truncation broke the
   JSON parse and silently degraded every model to `provider_default`
   (observed live: the planner attempt recorded `provider_default` despite
   `medium` being supported). The catalog read now passes a 4 MiB
   `stream_limit` (`runtime.run_argv` grew an optional parameter).
4. **Successful planner runs left no exec log** (only failures were logged);
   paid planner calls now always persist `.orx/runs/<run>/exec/plan-N.log`.

Known M0 limits after this run: unchanged except the last bullet above — a
real Codex path (planner + worker + deterministic verify) is now exercised.
Real Cursor runs still wait for a login; prompt-as-positional for Cursor
remains fake-tested only.

## Post-M0: first real Cursor Agent run, three tiers (2026-10-03)

After the user logged in, the same recipe ran against Cursor `agent`
2026.10.01 with a three-tier profile split (user's tiering): planner
`cursor-frontier` (claude-opus-5-5, effort deep), worker `cursor-strong`
(grok-4.7, effort standard), verifier `cursor-economy` (composer-2.5,
effort quick; the account catalog lists no composer-2.7 and no bare
claude-opus-5-5/grok-4.7 slugs — every listed model bakes effort into the
slug, e.g. claude-opus-5-5-high, grok-4.7-medium).

Full pipeline green in `/tmp/orx-cursor-real`:

- `orx plan` → Claude Opus 5.5 (as claude-opus-5-5-high), 30.5 s, valid Plan
  IR (narrative-wrapped, recovered), revision 1; attempt records
  `deep→high, requested_validated`.
- `orx run` → Grok 4.7 (grok-4.7-medium) worker wrote feature.py; both
  command verifications passed; attempt `standard→medium,
  requested_validated`.
- `orx verify` → Composer 2.5 CLI verifier returned `ORX_VERDICT=pass`
  inside its result envelope; agent verification passed; run `done`, goal
  `done` (3/3 verifications).

The real calls surfaced three more Cursor-path defects, fixed with
regression tests (suite → **220 passed**):

5. **Bracket overrides rejected on listed slugs**: the CLI errors out with
   `Cannot use this model: gpt-5.3-codex[effort=high]` — bracket syntax is
   only for parameterized families, and this catalog has none. The adapter
   now reads `agent --list-models` (local, free) and resolves effort via
   catalog-confirmed slug variants: `<model>-<mapped>` listed → rewrite to
   it (requested_validated); base listed without a variant → bare slug
   (provider_default); unlisted family → bracket (previous behavior).
6. **Plan IR wrapped in narrative**: even with "produce ONLY a JSON
   document", the model's final message surrounds the JSON with prose
   inside the `result` payload. `plan.extract_json_object()` now recovers
   the first balanced JSON object from planner text.
7. **ORX_VERDICT invisible in raw stdout**: `--print --output-format json`
   emits a single-line JSON envelope with escaped `\n`, so a verdict line
   inside it never appears as a standalone line. The CLI-verifier path now
   scans the adapter-extracted message text first, then raw stdout/stderr.

Prompt-as-positional delivery is now verified by a real logged-in run (the
argv is captured in each exec log). Cursor effort remains
`requested_validated` (slug-confirmed), never `reported` — the envelope
carries usage but no effective-effort field.
