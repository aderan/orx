# ORX M0 implementation plan

Phase 1 only. This document is the implementation contract for Phases 2–8.
The workspace is an empty directory. No application code exists yet.

## Environment (verified 2026-10-03)

| Check | Result |
| --- | --- |
| Machine | macOS 26.6.2, arm64 |
| Python | 3.12.9 via pyenv (`requires-python >=3.12`) |
| uv | 0.8.2 at `/opt/homebrew/bin/uv` (`uv tool install` / `upgrade` exist) |
| git | 2.54.0 |
| Codex CLI | `codex` 0.160.0, `codex login status` → logged in with ChatGPT |
| Cursor editor CLI | `/usr/local/bin/cursor` is the IDE shim, not the Agent |
| Cursor Agent | `agent` 2026.10.01, `agent status` → **not logged in** |
| ZCode skills | `~/.zcode/skills/*` are symlinks into `~/.agents/skills` |
| Repo | empty, not a git repository |

Do not call paid models from tests, doctor, or acceptance. Codex being logged in is not permission to spend a completion. Cursor Agent cannot be an acceptance dependency until someone logs in; the fake shell adapter covers that path.

## Architectural review

The Goal is implementable. The role split, host-vs-CLI split, Plan IR, and SQLite authority are sound. These gaps are real and are closed below. They are not new product scope.

1. **`orx run` must not own host work.** A Controller that calls `orx run` cannot also claim a host task from inside that same blocking process. Mixed queues deadlock. `orx run` executes at most the configured CLI slice; external-driver tasks are parked as `waiting_external`, never executed by ORX. It then returns. Host work stays `waiting_host` until `orx task claim`.
2. **`task complete` is not success.** The only path to `passed` is verification. A completion claim moves the task to `verifying`, then to `passed` or `failed`.
3. **Done is an invariant, not a judgment call inside ORX.** A run becomes `done` only when every task on the active plan revision is `passed`. Prose acceptance is enforceable because plan validation requires each Goal acceptance string to appear on some task. ORX does not interpret natural language.
4. **One active task graph.** `orx replan` is rejected while any active-revision task is `running` or `verifying`. On success the previous revision is `superseded` and its unfinished tasks become `cancelled`. Status, claim, and run ignore superseded revisions.
5. **Profile definitions and runtime status are different stores.** `profiles.toml` is the definition. `state.db` stores resource status and the snapshot on each attempt. Loading TOML never rewrites resource status. A tight quota must not edit the user's preference order.
6. **`--profile` pins.** Fallback runs only down a configured profile list. An explicit profile that is `unavailable` or `exhausted` is an error, not a silent substitution.
7. **No class downgrade unless policy says so.** Deep planning refuses a candidate whose class is below `frontier` when `[plan] allow_class_downgrade = false` (the default).
8. **Auto depth never calls a model.** It is a keyword rule on the Goal text. `--depth` wins.
9. **Cursor harness binary is `agent`.** Probing `cursor` would validate the editor, not the Agent.
10. **Effort is claimed only when observed.** Adapters record `requested_effort` always. `actual_effort` is `provider_default` unless the child process acknowledges the setting. Codex 0.160 has no `--effort` flag; the config key `model_reasoning_effort` exists, and valid levels differ per model in `codex debug models`. Cursor exposes effort only as a `--model` bracket (`effort=high` in `agent --help`).
11. **Verification commands are shell, so they are constrained.** They run with cwd fixed at the project root, a timeout, and a denylist (`sudo`, `mkfs`, `rm -rf /`, force-push, disk-erase patterns). Submitting the plan is the user's authorization to run the listed checks. ORX still does not invent commands.
12. **Skill install matches this machine.** Canonical copy is `~/.agents/skills/<name>`. If `~/.zcode/skills`, `~/.cursor/skills`, or `~/.codex/skills` exist, install a symlink there. No per-repo copy.

13. **`unknown` resource status must be routable.** Every profile starts `unknown`. If routing skipped it, a fresh `orx init` could not plan anything.
14. **Acceptance coverage uses exact strings.** It is brittle on purpose. The planning assignment lists the Goal acceptance strings verbatim and tells the planner to copy them. A paraphrase is rejected with an error naming the missing string. That is better than ORX guessing whether two sentences mean the same thing.
15. **Replan guard scope.** Replan is rejected while any active-revision task is `running` or `verifying`. Parked work is not guarded: `waiting_host` tasks are unclaimed (the claim protocol is the gate, so cancellation is safe), and `waiting_external` tasks are cancelled on supersession — the external operator's later `task complete` fails loudly against the superseded revision instead of silently merging.

Non-issues, kept as specified: no MCP, no Explorer role, Explore lives inside Plan IR, three model classes are preferences, host driver does not launch ZCode subagents, model ids stay in config. The `controller` role and `[controller] profile` are validated but never routed in M0 (reserved for a future in-process controller; not consumed by any M0 command).

## Product shape

Distribution name `orx-agent`. Import package `orx`. Console script `orx`.

```
orx/
├── pyproject.toml
├── README.md
├── docs/m0-plan.md
├── docs/m0-report.md          # Phase 8
├── src/orx/
│   ├── cli.py                 # Typer surface and JSON envelopes
│   ├── records.py             # enums and records (renamed from model.py: no clash with the profile `model` field)
│   ├── config.py              # config.toml + profiles.toml
│   ├── state.py               # SQLite, migrations, repositories
│   ├── machine.py             # task transitions
│   ├── plan.py                # Plan IR validate + depth policy
│   ├── routing.py
│   ├── runtime.py             # spawn, timeout, cancel, capture, redact
│   ├── dispatch.py            # plan/run/verify orchestration
│   ├── verify.py
│   ├── skills.py
│   ├── update.py
│   └── adapters/
│       ├── base.py
│       ├── shell.py
│       ├── codex.py
│       └── cursor.py
├── skills/orx-controller/SKILL.md
├── skills/orx-agent/SKILL.md
├── examples/personal/         # illustrative only, not imported by core
└── tests/
```

No `core/` package of re-exports. No web, queue, or Docker. Stdlib `sqlite3`, `tomllib`, `subprocess`. Dependencies: `typer`, `pydantic>=2` (pydantic parses and type-validates the Plan IR document only; every other record is a plain dataclass/enum). Dev: `pytest`.

Project layout created by `orx init`:

```
.orx/config.toml
.orx/profiles.toml
.orx/state.db
.orx/runs/<run_id>/
```

Discovery walks parents for `.orx/`. The project root is the directory that contains `.orx`. Every spawned process and verification command uses that directory as cwd and rejects paths outside it.

## Domain model

Independent fields, never packed into one id string:

| Concept | M0 values |
| --- | --- |
| Role | `controller` `planner` `worker` `verifier` |
| Driver | `host` `cli` `external` |
| Harness | `zcode` `codex` `cursor` `shell` (more later without schema change) |
| Model | string from the profile |
| Class | `frontier` `strong` `economy` |
| Effort | `quick` `standard` `deep` `max` |
| Plan depth | `light` `standard` `deep` (not an effort) |
| Capabilities | set of strings, `coding` and `vision` are the ones M0 understands |

`Goal`, `Run`, and `PlanRevision` are different rows. M0 keeps one `active` Goal per project. `orx goal new` inserts the Goal and its Run.

A planning assignment is not a Task. Tasks exist only after a plan validates.

## Config

`config.toml` and `profiles.toml` carry `schema_version = 1`.

```toml
schema_version = 1

[controller]
profile = "zcode-glm53"

[plan]
depth = "auto"
allow_class_downgrade = false

[plan.light]
profiles = ["zcode-glm53"]

[plan.standard]
profiles = ["zcode-glm53", "cursor-grok"]

[plan.deep]
profiles = ["codex-frontier", "cursor-opus"]

[worker]
profiles = ["zcode-glm53", "cursor-grok", "cursor-opus"]

[verify]
profiles = ["zcode-flash", "codex-frontier"]

[runtime]
max_parallel = 1
command_timeout_sec = 1800
```

`max_parallel` defaults to 1 and M0 caps CLI execution at 1 even if the file says more. The field exists so a later worktree runtime can raise it without a config migration.

```toml
schema_version = 1

[profiles.example]
driver = "cli"          # host | cli | external
harness = "shell"       # zcode | codex | cursor | shell
model = "configured-id"
class = "strong"        # frontier | strong | economy
effort = "deep"         # quick | standard | deep | max
capabilities = ["coding"]
force = false           # cursor harness only: allow --force/--yolo launch flags

# shell harness only
executable = "fake-agent"
args = ["--prompt-file", "{prompt_file}"]
prompt_transport = "file"   # stdin | argument | file
```

Harness→adapter mapping: `shell`, `codex`, and `cursor` have launch adapters. `zcode` is a host harness — `driver = "cli"` + `harness = "zcode"` is a load error (no adapter exists; zcode work is done by the host).

Unknown class, effort, driver, or a profile reference that does not exist is a load error. Doctor reports it. Commands that need that config exit non-zero with `--json`.

Personal examples in `examples/personal/` may name GLM, Codex, and Cursor models. `src/orx/` must not.

## Plan depth

First match wins:

1. `--depth light|standard|deep`
2. Goal text matches high-risk tokens (`architect`, `migration`, `migrate`, `refactor`, `public api`, `breaking`) → `deep`
3. Goal text matches small-scope tokens (`typo`, `rename`, `comment`, `log line`, `one line`, `tiny`) and does not match high-risk → `light`
4. else `standard`

`--profile` selects that profile for the planner and disables fallback. It does not change the recorded depth.

## Plan IR

Authoritative artifact. Markdown rendering is derived and disposable.

```json
{
  "goal": "G001",
  "exploration": {
    "summary": "",
    "relevant_components": [],
    "unknowns": [],
    "assumptions": [],
    "risks": []
  },
  "approach": { "summary": "", "decisions": [] },
  "tasks": [
    {
      "id": "T001",
      "objective": "",
      "dependencies": [],
      "scope": { "allowed": ["src/"] },
      "acceptance": [],
      "verification": ["pytest -q"],
      "routing": {
        "complexity": "medium",
        "required_capabilities": ["coding"]
      }
    }
  ]
}
```

Validation rejects the whole document when:

- `goal` is not the active Goal id
- a required object/list is missing or the wrong type
- task ids are not `T` + digits, or are duplicated
- a dependency names a missing id, or the graph has a cycle
- `scope.allowed` is not a list of project-relative paths (`..` and absolute paths fail)
- `routing.complexity` is not `low|medium|high`
- any Goal acceptance string is absent from every task `acceptance` list
- `verification` is not a list of strings

Extra unknown fields are ignored so a host can round-trip notes without breaking validation. They are not persisted as authority.

`orx plan submit --file plan.json` on failure returns JSON `{ "ok": false, "errors": [...] }`, leaves the assignment `waiting_host`, and does not write tasks.

## Persistence

SQLite, WAL, `foreign_keys=ON`. Every child table keys on an explicit integer surrogate id of its parent (listed below), so FKs are writable with `foreign_keys=ON`. `meta.schema_version` starts at 1. Opening a newer or unknown version exits with a migration error and does not rewrite the file. Migrations are a list of functions keyed by integer version. M0 ships the v1 migration only.

Tables ( surrogate `id INTEGER PRIMARY KEY` on every table unless noted ):

- `goals` — id (text, `G00n`), objective, constraints_json, acceptance_json, context, status (`active|done|cancelled`), timestamps
- `runs` — id (text, `R00n`), goal_id, status (`planning|running|blocked|done`), timestamps
- `plan_revisions` — **id (integer PK)**, run_id, revision, depth, planner_profile, ir_json, status (`active|superseded`); child tables reference this integer id as `revision_id`
- `planning_assignments` — id (text, `P00n`), run_id, profile, depth, status (`waiting_host|submitted|failed|cancelled`), prompt, created_at, submitted_at. `failed`/`cancelled` are reserved: no M0 command sets them (a submit failure leaves the assignment `waiting_host`); they exist for the host planner-reporting path.
- `tasks` — **id (integer PK)**, revision_id + task_id (unique together), objective, scope/acceptance/verification/routing JSON, status, failure_reason
- `task_dependencies` — revision_id, task_id, depends_on (composite PK)
- `task_events` — id, revision_id, task_id, from_status (null for insert), to_status, event, reason, created_at — the audit log that makes a task's state path reconstructable
- `attempts` — **id (integer PK)**, revision_id, task_id nullable, assignment_id nullable, profile, driver, harness, model, requested_effort, actual_effort, **effort_source** (`provider_default | requested_validated | reported`; null for host attempts), fallback_used, routing_reason, start, end, result, failure_reason
- `evidence` — **id (integer PK)**, attempt_id, kind, path
- `verifications` — **id (integer PK)**, revision_id, task_id, attempt_id, kind (`command|agent`), command (the raw entry), **required_capabilities_json** (the decomposed `agent[cap]:` capability list — the bracket syntax is IR surface only; persisted state keeps fields independent), exit_code, passed, output_path
- `routing_decisions` — **id (integer PK)**, attempt_id, role, requested_json (what was asked for), candidates_json, selected, reason, downgrade_blocked
- `resource_status` — profile (PK), status, note, updated_at

Attempt rows are the observability record: who, profile, harness, model, requested vs actual effort, why that profile, whether fallback was used, duration, result, evidence, verification, and human intervention (`driver=host` or `driver=external`). No token or cost fields.

## Task state machine

States: `pending`, `runnable`, `running`, `waiting_host`, `waiting_external`, `verifying`, `passed`, `failed`, `blocked`, `cancelled`.

Transitions that code is allowed to make:

| From | Event | To |
| --- | --- | --- |
| (insert) | no unmet deps | `runnable` |
| (insert) | has deps | `pending` |
| `pending` | every dep is `passed` | `runnable` |
| `pending` | a dep is `failed` or `cancelled` | `blocked` |
| `runnable` | routed driver `host` | `waiting_host` |
| `runnable` | routed driver `cli`, process starts | `running` |
| `runnable` | routed driver `external` | `waiting_external` |
| `waiting_host` | `task claim` | `running` |
| `running` | `task complete` | `verifying` |
| `waiting_external` | `task complete` | `verifying` |
| `verifying` | every verification entry passed, or the list is empty | `passed` |
| `verifying` | any command exits non-zero, or an agent verifier fails | `failed` |
| `running` / `waiting_external` | `task fail` | `failed` |
| `failed` | `task retry` | `runnable` |
| `blocked` | deps become all `passed` | `runnable` |
| any unfinished | revision superseded | `cancelled` |

Empty `verification` means there is nothing deterministic to run; the completion claim plus the recorded evidence is the check, and the task can pass. A non-empty list that fails never passes. Goal acceptance still has to be copied onto a task, so an empty verification list does not skip acceptance coverage.

Claim is one transaction: `UPDATE ... WHERE status='waiting_host'`. Zero rows means conflict, exit non-zero, no second runner.

After any transition, recompute dependents and run status (single owner: `machine.py`):

- no active revision → run `planning`
- any task `running`, `waiting_host`, `waiting_external`, `verifying`, or `runnable` → run `running`
- a task `failed` or `blocked` and nothing runnable → run `blocked`
- every active-revision task `passed` → run `done` and Goal `done`

`cancelled` tasks exist only in superseded revisions (M0 has no command that cancels an active-revision task), so the active revision never contains them.

## Routing

Input: role, depth (planner only), capability requirements, optional pinned profile.

1. Candidates are the pinned profile, or the configured list for that role and depth.
2. Drop `unavailable` and `exhausted`. A missing resource row means `unknown`. `orx init` seeds every profile as `unknown`.
3. For planner depth `deep`, drop classes other than `frontier` unless `allow_class_downgrade` is true. Record `downgrade_blocked` when this removes the last candidate.
4. Prefer `abundant`, `available`, and `unknown` in list order. `unknown` is a usable state, not a blocked one; otherwise a freshly initialized project could not route anything. Use `constrained` only when none of those remain.
5. First survivor wins. Persist the candidate list and the reason (`primary`, `skipped_unavailable`, `fallback`, `constrained_last_resort`, `pinned`).

Worker capability filter: drop profiles that lack a required capability. If that removes everyone, fail the dispatch with a routing error rather than ignoring the capability.

`orx resource set <profile> <status>` writes only `resource_status`. Status values: `abundant`, `available`, `constrained`, `exhausted`, `unavailable`, `unknown`.

## CLI contract

Global `--json` on every command below. JSON goes to stdout. Human text is the default. Errors use, with `--json`, `{ "ok": false, "error": "...", "errors": [...] }`.

Exit codes: `0` success; `1` domain error (invalid config/state, routing failure, illegal transition, conflict, not-updateable install); `2` CLI usage error. No other values.

| Command | Behavior |
| --- | --- |
| `orx init` | Create `.orx/` if missing. Refuse to clobber a non-empty config. Seed SQLite v1 and resource rows. |
| `orx doctor` | Checks below. Exit 1 when config or state is invalid. Missing optional CLIs are warnings, not failures. |
| `orx version` | Package version. |
| `orx update --check` | Report install source and whether an upgrade path exists. No mutation. |
| `orx update` | `uv tool upgrade orx-agent` only for a non-editable uv tool install. Editable install exits 1 (domain error) with the reason. |
| `orx goal new --objective --acceptance --constraint --context` | Create active Goal `G001` (increment) and Run. Refused with an error while another Goal is `active` (M0 keeps one active Goal per project). |
| `orx goal show` | Active Goal plus run id. |
| `orx plan [--depth] [--profile]` | Route the planner. |
| `orx plan submit --file` | Validate and persist, or return errors. |
| `orx replan` | Same as plan, new revision, rules above. |
| `orx run` | Dispatch CLI tasks up to the parallel cap (1). Return host/external work as assignments. Do not block on the Controller. |
| `orx status` | Human layout from the Goal, plus JSON with the same fields. |
| `orx task list` | Active revision. |
| `orx task claim <id>` | Host claim. |
| `orx task complete <id> --evidence <file>` | Store evidence, enter `verifying`, run deterministic checks immediately. |
| `orx task fail <id> --reason` | `failed`. |
| `orx task retry <id>` | New attempt, `runnable`. |
| `orx verify` | Run any outstanding command checks for tasks in `verifying`, dispatch `agent:` checks, then recompute run status. |
| `orx verify submit <id> --result --evidence` | Record an agent verifier's verdict. |

Deterministic checks have exactly one executor (`verify.run_command_verifications`, idempotent — an entry with a recorded result is never re-run). `task complete` invokes it immediately; `orx verify` invokes the same executor for anything outstanding (e.g. after restart, or entries added by a later revision). There are not two code paths.
| `orx profiles` | Parsed profiles. |
| `orx resource list` / `orx resource set` | Runtime status. |
| `orx skill install` / `orx skill update` | User-level skills. Update replaces the canonical copy and refreshes symlinks. |

`orx plan --json` returns one of:

```json
{ "ok": true, "mode": "completed", "revision": 1, "profile": "...", "depth": "deep" }
```

```json
{
  "ok": true,
  "mode": "host_required",
  "depth": "standard",
  "assignment": {
    "id": "P001",
    "profile": "zcode-glm53",
    "role": "planner",
    "prompt": "...",
    "schema": {},
    "submit": "orx plan submit --file plan.json"
  }
}
```

`orx run --json` returns `{ "ok": true, "started": [...], "host_required": [...], "waiting_external": [...] }`.

`orx status --json` includes goal, run, plan depth, planner profile, revision, tasks (id, objective, status, profile, blocked_by), verification counts, resources, and result. Human output follows the Goal's layout (`✓ ● ○`, resource lines, `RUNNING` / `DONE` / `BLOCKED`).

Doctor lines, each with a state:

- Python >= 3.12
- `uv` on PATH (required for update; warn if missing)
- `git` on PATH
- inside a git repo (warn if not)
- Codex: binary, `exec` help contains `--json`, `-m`, `-C`, and `--output-last-message` (exactly the adapter's Required set, so doctor never passes a binary the adapter would reject), auth via `codex login status` (`logged_in` / `not_logged_in` / `unknown`). No completion is launched. Operational probe stays `not_run`.
- Cursor Agent: binary name `agent`, help contains `--print`, `--output-format`, `--workspace`, `--trust`, and `--model`, auth via `agent status`. Same operational rule.
- Shell profiles: configured executable exists
- config schema, profile references, classes, efforts, fallback lists
- SQLite opens and `schema_version` is supported
- skill canonical dirs present or not installed

Binary-found is never printed as "works".

## Execution

`runtime.py` is the only subprocess wrapper: argv, env (no secret dumping), cwd, timeout, cancel, stdout/stderr capture, truncation, redaction. Adapters translate a `Launch` record into argv. They do not call `subprocess` themselves.

Shared adapter vocabulary (one definition, no per-adapter copies): the effort map `quick→low, standard→medium, deep→high, max→max` lives once in `adapters/base.py`, and `(requested_effort, actual_effort, effort_source)` travels as one `EffortOutcome` record.

Redact before persisting output: `sk-` tokens, `Bearer `, `AKIA`, `ghp_`, `github_pat_`, and `api_key=` / `token=` assignments. Truncate each stream at 256 KiB.

### Shell adapter

Used for generic CLIs and for every automated test. `prompt_transport` chooses stdin, an argv slot `{prompt}`, or `{prompt_file}`. Exit code is the result. `actual_effort = provider_default` unless the fake prints a line `ORX_ACTUAL_EFFORT=<value>` that the adapter parses. Tests use that line; real shells usually stay `provider_default`.

### Codex adapter (0.160 probe)

Capability probe is help text plus a version parse, cached in memory for the process:

- Required: `codex exec`, `--json`, `-m`, `-C`, `--output-last-message`
- Effort: map `quick→low`, `standard→medium`, `deep→high`, `max→max`, then check the mapped level against `supported_reasoning_levels` for the configured model in `codex debug models` (local catalog, no completion). Supported → pass `-c model_reasoning_effort="<mapped>"`. Unsupported or model not in catalog → pass nothing and record `provider_default`. Verified 2026-10-03: levels differ per model (some list `ultra`, some stop at `max`), so a fixed flag would be wrong.
- Planner launch adds `--output-schema` pointing at the Plan IR schema and `-o` for the last message
- Sandbox: `-s workspace-write`
- Do not pass `--dangerously-bypass-approvals-and-sandbox`
- `--ephemeral` so ORX runs do not depend on Codex session files
- Prompt as the positional argument
- `actual_effort` is the effort reported in the JSONL event stream if one is present. Otherwise it is the catalog-validated value ORX passed, recorded with `effort_source = "requested_validated"`. If ORX passed nothing, it is `provider_default`. The exact event field is confirmed in Phase 4 against a captured fixture, not assumed.

If help no longer contains a required flag, the adapter fails the attempt with `capability_mismatch` instead of guessing.

### Cursor adapter (agent 2026.10.01 probe)

- Binary `agent`
- Args: `--print --output-format json --workspace <root> --trust --model <id>`, then the prompt as the final positional argument (same delivery as Codex; `prompt_transport` stays shell-only). Verified against `agent --help` output in Phase 4 before shipping.
- Effort: if the configured model contains no `[...]` and help mentions `effort=`, pass `--model '<id>[effort=<mapped>]'` with map `quick→low`, `standard→medium`, `deep→high`, `max→max`
- If the model string already has a bracket, do not rewrite it; `actual_effort` stays `provider_default` because ORX did not apply one
- When ORX adds the bracket, `actual_effort` is the mapped value only if the process starts and JSON output includes that model string; otherwise `provider_default`
- Do not pass `--force` / `--yolo` unless the profile sets `force = true`
- Do not pass `--api-key`

Future Claude Code / OpenCode adapters implement the same `Launch` surface. Goal, Plan, and Task tables do not change.

### Host and external

Host: no process. Dispatch parks the task or the planning assignment. The Controller Skill runs the subagent and calls `plan submit` or `task complete`.

External: park as `waiting_external`. The Controller finishes it with `task complete` or `task fail`. No GUI automation.

## Verification, retry, replan

Deterministic verification is the `verification` string list, executed by `verify.py` through the runtime (shell, timeout, denylist, project cwd). Results land in `verifications`.

Agent verification is a verification entry prefixed `agent:` (for example `agent: confirm the error message is user-readable`). It is never run as shell. `orx verify` creates a verifier attempt routed through `[verify].profiles`: host profiles return an assignment, CLI profiles are launched. The verifier answers with `orx verify submit <task> --result pass|fail --evidence <file>`. A task passes only when every command entry and every `agent:` entry passed. Vision is a required capability on the entry (`agent[vision]: ...`), not a separate role.

Retry (`orx task retry`) is for a `failed` task: new attempt, status `runnable`, same plan. It does not replan.

Replan (`orx replan`) is the structural path. It uses the planner pipeline again and writes `revision + 1`.

## Skills

Two skills, each a directory with `SKILL.md` frontmatter (`name`, `description`).

`orx-controller` tells the host Controller to:

- read `orx status --json` before acting
- treat `mode=host_required` as its cue to launch a subagent, never as a failure
- pass the assignment prompt and schema through unchanged
- submit Plan IR with `orx plan submit`
- claim a task before doing it
- write an evidence file and `task complete` or `task fail`
- retry on test, process, or incomplete-implementation failures
- replan when an assumption or the dependency graph is wrong
- run `orx verify` when work is in `verifying`
- treat the run as Done only when `orx status --json` says `done`

`orx-agent` tells a host Planner, Worker, or Verifier to:

- do the assigned objective only
- leave the Goal text alone
- stay inside `scope.allowed`
- fill exploration, approach, and tasks when the assignment is a plan
- run the requested checks
- emit the evidence JSON the assignment asks for
- return blockers instead of widening scope

Evidence file written by agents:

```json
{ "summary": "", "commands": [], "artifacts": [] }
```

`orx skill install` copies packaged skills to `~/.agents/skills/orx-controller` and `orx-agent`, then symlinks into existing `~/.zcode/skills`, `~/.cursor/skills`, and `~/.codex/skills`.

## Update and migration

Version comes from package metadata.

Install source detection, in order:

1. `$(uv tool dir)/orx-agent/uv-receipt.toml` exists and `sys.prefix` is that tool environment → source `uv-tool`, or `editable` when the receipt's requirement has an `editable` key
2. else `unknown` (pip, a venv, or running from source)

The receipt is uv's own record; parsing `uv tool list` text would break on format changes.

`orx update --check` prints source, current version, and the command it would run. `orx update` runs `uv tool upgrade orx-agent` for `uv-tool` only. Tests inject a fake `uv` executable and never upgrade the interpreter running pytest.

Config migration: if `schema_version` is absent or greater than the code supports, refuse. v1 reads v1. A later version adds a function; it does not delete TOML keys it does not understand. SQLite migration failure leaves the original file untouched (copy, migrate the copy, replace).

## Test strategy

`pytest` uses fixtures and a fake executable on `PATH`. No network and no real `codex` / `agent` process.

| Area | Cases |
| --- | --- |
| Config | parse, invalid class, invalid effort, missing profile, `--profile` over depth list, resource status not written into TOML |
| Plan IR | valid, malformed, duplicate ids, missing dependency, cycle, path escape |
| Depth | explicit light/standard/deep, tiny-token goal, architecture token, `--depth` overrides tokens |
| Machine | pending→runnable, claim conflict, complete stays out of `passed` until checks pass, fail, retry, dep unlock, blocked dep |
| Persistence | close and reopen DB, active Goal, revision, in-progress task |
| Routing | primary, skip unavailable, skip exhausted, constrained last, fallback reason, deep downgrade refused, pinned profile no fallback |
| Runtime | fake success, nonzero, timeout, cancel, garbage output, secret redaction |
| Verification | command pass, command fail, complete does not pass |
| Update | `--check` and source detection against a fake `uv` |
| Skills | package contains both skills; install writes the canonical dir and symlink |

## Acceptance scenario (Phase 8)

Use a temporary git repo and `uv tool install -e .` from this checkout. Acceptance is not part of pytest.

1. `orx version`, `orx doctor`.
2. `orx init` produces the three `.orx` files; doctor accepts them.
3. Goal: add a small feature with a failing test already in the repo.
4. Standard plan configured to a `driver=host` profile. `orx plan --depth standard --json` returns `host_required`. Submit a valid Plan IR. State contains the revision and tasks.
5. One task is host: `claim`, write evidence, `complete`. One task is `harness=shell` pointed at a fake CLI, started by `orx run`.
6. A verification command fails. That task is `failed`, not `passed`. Fix the fixture, `orx task retry`, verify passes.
7. Kill nothing special: run `orx status` in a new process and show the same task states.
8. Point the planner at a fake Codex-shaped shell adapter. `orx replan` runs it and persists the printed Plan IR.
9. `orx resource set <primary> unavailable`. Next plan selects the configured fallback and the routing row says why.
10. Run reaches `done` only after every active task is `passed`.

`docs/m0-report.md` records commands and outputs for each step. "Works" without that log is not done.

## Phase order

| Phase | Done when |
| --- | --- |
| 2 Core | config, sqlite v1, Goal, Plan IR, state machine; those tests green |
| 3 CLI | commands and `--json` envelopes above, still with no real Agent |
| 4 Execution | shell, Codex, Cursor adapters plus host claim; fake-process tests green |
| 5 Verify / route | verification gate, resource status, fallback tests green |
| 6 Skills | two skills install from the package |
| 7 Install / update | `uv tool install -e .` puts `orx` on PATH; update tests green |
| 8 Acceptance | scenario above, then `docs/m0-report.md` |

## Known M0 limits (intentional)

- Parallel writers share one working tree, so CLI parallelism stays 1. The runtime accepts a cwd so a later driver can pass a worktree.
- Resource status is manual.
- Doctor does not start an Agent session.
- `orx update` will not upgrade an editable install.
- Codex noninteractive approval depends on the user's Codex config. The adapter will not force the bypass flag.
- Cursor Agent on this machine is not logged in. Real Cursor runs wait for the user; tests use the shell harness.

## M0 amendments (Core Review Gate, 2026-10-03)

Applied after the Step 2 review gate. Baseline semantics are unchanged; these fix
underspecification the review flagged as P0/P1. Code already implements all of
them unless noted "Phase 4".

1. Persistence: every child table keys on the parent's explicit integer
   surrogate `id` (FK targets are now defined); `attempts.effort_source`
   (`provider_default | requested_validated | reported`, null for host) and
   `verifications.required_capabilities_json` added. The `agent[vision]:`
   bracket remains the IR surface syntax; persistence decomposes it into
   `kind` + `required_capabilities` so role/capability stay independent fields.
2. Deterministic verification has one idempotent executor invoked by both
   `task complete` and `orx verify`.
3. Doctor's required flag sets exactly match the adapter Required sets
   (codex: `--json -m -C --output-last-message`; cursor: `--print
   --output-format --workspace --trust --model`).
4. Exit codes: 0 success, 1 domain error (incl. non-updateable install),
   2 usage.
5. `orx goal new` while a Goal is active → explicit error (one active Goal).
6. Profile schema adds `force = false` (cursor-only launch flag). Unknown
   *values* are load errors; unknown *keys* are ignored (forward compat).
7. Harness→adapter mapping: `driver = "cli"` + `harness = "zcode"` is a load
   error; shell/codex/cursor are the launchable harnesses.
8. Cursor prompt delivery: final positional argument (Phase 4 verifies).
9. Status payloads use `objective` (never `title`).
10. Run-status recompute has one owner (`machine.py`): no active revision →
    `planning`; cancelled tasks exist only in superseded revisions.
11. Replan guard scope documented (review item 15): parked `waiting_host` work
    is safely cancellable because claim is the gate; `waiting_external` work is
    cancelled on supersession and a late `task complete` fails loudly.
12. `planning_assignments.failed`/`cancelled` are reserved (no M0 command sets
    them). The `controller` role/config is validated but never routed in M0.
13. `model.py` renamed `records.py` (no clash with the profile `model` field);
    pydantic is Plan-IR validation only.

## M0 amendments (Phases 4–8, 2026-10-03)

Implementation contracts added while building the adapters and finishing M0:

1. **Serial slice:** each `orx run` invocation starts at most
   `effective_parallelism` (= 1) CLI process, then returns so the Controller
   stays in the loop; further runnable tasks are reported as `deferred`.
   Parking host/external tasks is not execution and is not capped.
2. **CLI verifier verdict contract:** a launched agent verifier must print
   exactly one final line `ORX_VERDICT=pass` or `ORX_VERDICT=fail`. Anything
   else (or a non-zero exit) is recorded as a failed verification — a broken
   verifier must not hang the run forever. The verifier prompt carries this
   contract.
3. **Codex argv:** `codex exec --json -m <model> -C <root> -s workspace-write
   --ephemeral --output-last-message <file> [-c model_reasoning_effort=<level>]
   <prompt>`; the planner additionally passes `--output-schema <file>` where
   the file is pydantic's `PlanIR.model_json_schema()`. The long flag is used
   everywhere (`-o` is its short form, verified in `codex exec --help`).
   Effort validation reads `codex debug models` (local catalog; format:
   `{"models": [{"slug", "supported_reasoning_levels": [{"effort"}]}]}`).
4. **Cursor argv:** `agent --print --output-format json --workspace <root>
   --trust --model <id> <prompt positional>` (+ `--force` only when the
   profile sets `force = true`; never `--api-key`). Prompt-as-positional is
   fake-tested; the first real logged-in run re-verifies it.
5. **External planners are not dispatched** by ORX: route to a `host` planner
   profile, run the prompt externally yourself, then `orx plan submit`.
6. **CLI-completed plans** persist the adapter profile as the revision's
   `planner_profile` (never "host-manual" when a real adapter ran).
7. **Worker prompt contract:** objective, verbatim acceptance, scope paths,
   the verification list, and exit-code semantics. Workers report effort via
   `ORX_ACTUAL_EFFORT=<level>` (shell harness; parsed by the adapter).
8. **Skills packaging:** repo `skills/` is the source of truth and is
   force-included into the wheel at `orx/skills/`; editable checkouts fall
   back to the repo copy. `orx skill update` refreshes the canonical copy and
   every symlink, and is a no-op when nothing is installed.
9. **Install source:** uv's real receipt format is `[tool] requirements =
   [{name, editable = <path>}]`; `orx update` refuses editable and unknown
   sources (exit 1) and runs `uv tool upgrade orx-agent` only for uv-tool.
