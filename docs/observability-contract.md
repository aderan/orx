# ORX observability read contract (schema v7)

Status: contract for external readers (2026-10-04). This document is the
supported way to analyze `.orx/state.db` from outside this repository.
The analysis layer lives at `~/Sources/Tools/orx-analytics/` and is not
shipped here. ORX stores the rows and defines how to read them.

Readers select named columns. They do not call `Store.open`, and they do
not migrate. Worked SQL below is the source the contract tests execute
against `tests/fixtures/observability/seed.sql`.

## Schema version, stability, and the v5-to-v7 amendment

`meta.schema_version` is the integer version of the file, stored as text.
This contract is version **7** only.

| File value | Reader action |
|---|---|
| missing `meta` table, missing `schema_version` row, or a non-integer value | refuse; do not infer a version |
| integer less than 7 | refuse; do not add columns and do not run ORX migrations |
| `7` | read |
| integer greater than 7 | refuse; do not rewrite the file |

ORX itself migrates only when application code opens the database through
`Store.open`. Migrations are additive functions keyed by the integer
version they produce. From v2 upward, ORX migrates a copy and replaces
the original only after success (backup-replace-restore). A failed
migration leaves the original file in place. Widening a CHECK constraint
rebuilds that table inside the copy. That machinery belongs to ORX.
Consumer examples never open Store and never trigger it.

Additive stability at v7 means a later ORX patch may add a column or a
table without removing the columns this contract names. Readers list the
columns they use. `SELECT *` is not part of the contract. A future schema
version is a new contract; until this document is amended, the gate below
refuses it.

The M1.2 baseline text named schema v5 because that was the next version
when the baseline was frozen. The repository had already shipped v6
(`tasks.preread_json`). The amendment dated 2026-10-04 makes **v7** the
observability schema. v7 adds:

| Table | Column | Meaning |
|---|---|---|
| `attempts` | `session_ref` | Opaque caller-supplied native session id. Never synthesized. |
| `attempts` | `run_id` | Run attribution copied from a run the caller held, from `plan_revisions.run_id`, or from `planning_assignments.run_id`. |
| `attempts` | `usage_missing_reason` | Why a finished CLI path stored no usage observation. |
| `runs` | `started_at` | First transition into `running`. |
| `runs` | `completed_at` | First transition into `done`. |

v7 also rebuilds `usage_observations` so `source` may be `native_cli`,
`output_estimate`, or `host_report`. `accuracy` stays `exact`,
`estimated`, or `unknown`. Existing observation rows are copied. Readers
of a v5 or v6 file do not perform that rebuild; they refuse the file.

```sql
-- query: schema_gate
SELECT CASE
  WHEN (
    SELECT value FROM meta WHERE key = 'schema_version'
  ) = '7' THEN 'ok'
  ELSE 'refuse'
END AS decision;
```

## WAL-aware read-only snapshot

`.orx/state.db` runs in WAL mode under ORX. Committed rows can sit in the
`-wal` sidecar until a checkpoint. Copying only the main file can drop
those frames. Deleting `-wal` or `-shm`, and `PRAGMA wal_checkpoint`, are
writes. Do not do them.

A reader opens the existing file in place:

```python
import sqlite3
from pathlib import Path

def open_readonly(db_path: Path) -> sqlite3.Connection:
    # mode=ro reads the main file and any committed WAL frames beside it.
    # It does not checkpoint. immutable=1 would skip the WAL and is wrong
    # for a live ORX database. Do not call Store.open.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn
```

Run `schema_gate` first. On `refuse`, stop. `query_only` rejects writes
on this connection.

When the report must keep a private file while ORX continues to write,
copy with the backup API into a new path the consumer owns. The source
stays read-only. Do not `os.replace` `.orx/state.db`.

```python
def snapshot(db_path: Path, dest: Path) -> sqlite3.Connection:
    src = open_readonly(db_path)
    if dest.exists():
        raise FileExistsError(dest)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    return open_readonly(dest)
```

`orx status` and `orx usage` are convenient JSON views of the same rows,
and they open `Store`. Opening `Store` on an older file migrates it.
A consumer that must not migrate uses the snapshot above, not the CLI.

## Identifiers

One `.orx/state.db` is one project. **`project_id` is not a column.** The
external consumer supplies it (a path, a slug, or whatever identity
`orx-analytics` uses) and binds it as `:project_id`. Two projects both
named `R001` / revision `1` / `T001` stay apart only because that bound
value is part of every result row.

Inside one database these identifiers are different things:

| Name | Where it lives | Scope |
|---|---|---|
| `project_id` | bound by the consumer, not stored | one per database |
| database-local row id | `plan_revisions.id`, `tasks.id`, `attempts.id`, `task_events.id`, `verifications.id`, `usage_observations.id` | unique inside that table in that file |
| `run_id` | `runs.id`, also copied onto `plan_revisions.run_id`, `planning_assignments.run_id`, `attempts.run_id`, `usage_observations.run_id` | text such as `R001`; unique inside the file; repeats across projects |
| revision number | `plan_revisions.revision` | integer, unique together with `run_id`; not the same value as `plan_revisions.id` |
| `task_id` | `tasks.task_id`, copied onto attempts, events, verifications, observations | short id such as `T001`; unique only together with `tasks.revision_id` |
| `attempt_id` | `attempts.id` | integer row id; the only attempt identity that does not repeat inside the file |

`tasks` has `UNIQUE(revision_id, task_id)`. The same `task_id` on two
revisions is two tasks, with two `tasks.id` values. Joining attempts to
tasks on `task_id` alone attaches both revisions. The join that does not
collide is:

```text
project_id + attempts.id
project_id + plan_revisions.id          (database-local revision row)
project_id + run_id + revision          (human revision number)
project_id + tasks.revision_id + tasks.task_id
```

`planning_assignments.id` (`P001`) is an assignment id, not an attempt id
and not a task id.

Planner attempts often have `revision_id` NULL because the attempt is
opened before a plan is accepted. They remain attributable when
`attempts.run_id` is set: the caller passed the run, or ORX copied it
from the waiting assignment. A rejected planner still has that `run_id`.
Read the stored `attempts.run_id`. Do not invent one from timestamps,
from `updated_at`, or by picking the latest run in the file.

When `attempts.run_id` is NULL, the association is **unknown**. That is
the legacy case: a row migrated from before v7 with no revision and no
assignment, or an attempt that never had either. A NULL here is not a
guess. Leave it unknown even if `planning_assignments` or a nearby
timestamp looks suggestive. v7 backfills `attempts.run_id` only where a
revision or an assignment already recorded the run; everyone else stays
NULL, and readers do not finish that backfill.

```sql
-- query: linkage
SELECT
  :project_id AS project_id,
  a.id AS attempt_id,
  a.role,
  a.revision_id AS revision_row_id,
  pr.revision AS revision,
  a.task_id,
  t.id AS task_row_id,
  a.run_id,
  pa.id AS assignment_id,
  pa.run_id AS assignment_run_id,
  a.session_ref,
  CASE
    WHEN a.run_id IS NOT NULL THEN 'known'
    ELSE 'unknown'
  END AS run_association
FROM attempts a
LEFT JOIN plan_revisions pr ON pr.id = a.revision_id
LEFT JOIN tasks t
  ON t.revision_id = a.revision_id AND t.task_id = a.task_id
LEFT JOIN planning_assignments pa ON pa.id = a.assignment_id
ORDER BY a.id;
```

`run_association = 'unknown'` is the whole answer for an unresolvable
row. `assignment_run_id` is context for rows that have an assignment. It
does not fill a NULL `attempts.run_id`.

## Tables

Timestamps are TEXT. JSON payloads are TEXT. Booleans that ORX writes are
integers 0 and 1 (`verifications.passed`, `attempts.fallback_used`).

### `meta`

| Column | Null | Role in this contract |
|---|---|---|
| `key` | no | `schema_version` is the only key readers need |
| `value` | no | text; v7 stores `7` |

### `runs`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | `run_id` |
| `goal_id` | no | goal that owns the run |
| `status` | no | `planning`, `running`, `blocked`, or `done` |
| `created_at` | no | row insert time |
| `updated_at` | no | last status write; not a lifecycle clock |
| `started_at` | yes | first time status became `running` |
| `completed_at` | yes | first time status became `done`; cleared on leave |

### `plan_revisions`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | database-local revision row id (`revision_id` elsewhere) |
| `run_id` | no | run that owns this revision |
| `revision` | no | revision number, unique with `run_id` |
| `depth` | no | plan depth |
| `planner_profile` | no | profile that produced the plan |
| `ir_json` | no | stored plan document |
| `status` | no | `active` or `superseded` |
| `created_at` | no | insert time |

### `planning_assignments`

Used to explain planner attempts that have no revision yet.

| Column | Null | Meaning |
|---|---|---|
| `id` | no | assignment id (`P001`) |
| `run_id` | no | run the assignment was opened for |
| `profile` | no | planner profile |
| `depth` | no | requested depth |
| `status` | no | `waiting_host`, `submitted`, `failed`, `cancelled` |
| `prompt` | no | assignment text |
| `created_at` | no | insert time |
| `submitted_at` | yes | set when a submit is accepted |

### `tasks`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | database-local task row id |
| `revision_id` | no | `plan_revisions.id` |
| `task_id` | no | short id; repeats across revisions |
| `objective` | no | task text |
| `scope_json` | no | allowed paths |
| `acceptance_json` | no | acceptance list |
| `verification_json` | no | verification entries |
| `preread_json` | no | v6 preread list; default `[]` |
| `routing_json` | no | routing hints |
| `status` | no | task status, including `passed` and `failed` |
| `failure_reason` | yes | current failure text; cleared on retry |
| `created_at` | no | insert time |
| `updated_at` | no | last task write |

### `task_events`

History survives `verifications` being deleted on retry.

| Column | Null | Meaning |
|---|---|---|
| `id` | no | event row id |
| `revision_id` | no | revision row id |
| `task_id` | no | short id; pair with `revision_id` |
| `from_status` | yes | previous status |
| `to_status` | no | new status |
| `event` | no | transition name |
| `reason` | yes | recorded reason; NULL means none was stored |
| `created_at` | no | event time |

### `attempts`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | `attempt_id` |
| `revision_id` | yes | revision row id; NULL when no plan was accepted |
| `task_id` | yes | short id; NULL for planners |
| `assignment_id` | yes | planner assignment, when the attempt has one |
| `role` | no | `planner`, `worker`, or `verifier` |
| `profile` | no | profile name |
| `driver` | no | `host`, `cli`, or `external` |
| `harness` | no | `zcode`, `cursor`, `codex`, `shell`, … |
| `model` | no | model id as routed |
| `requested_effort` | no | requested effort |
| `actual_effort` | yes | effort the harness reported |
| `effort_source` | yes | where `actual_effort` came from |
| `fallback_used` | no | 0 or 1 |
| `routing_reason` | yes | routing explanation |
| `started_at` | yes | span start; NULL if never stamped |
| `ended_at` | yes | span end; NULL if still open or never stamped |
| `result` | yes | completion result |
| `failure_reason` | yes | attempt failure text |
| `isolation` | yes | isolation the launch claimed |
| `session_ref` | yes | opaque session reference |
| `run_id` | yes | stored run attribution; NULL means unknown |
| `usage_missing_reason` | yes | see coverage |

### `usage_observations`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | observation row id |
| `attempt_id` | no | the attempt this fact belongs to |
| `profile` | no | profile copied at write time |
| `run_id` | no | run copied at write time |
| `task_id` | yes | short id copied at write time; NULL for planners |
| `input_tokens` | yes | native input count; NULL if that field was absent |
| `output_tokens` | yes | native output count |
| `cached_input_tokens` | yes | native cached-input count; may exceed `input_tokens` |
| `source` | no | `native_cli`, `output_estimate`, or `host_report` |
| `accuracy` | no | `exact`, `estimated`, or `unknown` |
| `created_at` | no | insert time |

There is no `session_ref` column on this table. The JSON usage view
attaches the attempt's `session_ref` at read time. SQL readers do the
same join. There is no fee column and no normalized token column.

### `verifications`

| Column | Null | Meaning |
|---|---|---|
| `id` | no | verification row id |
| `revision_id` | no | revision row id |
| `task_id` | no | short id; pair with `revision_id` |
| `attempt_id` | yes | verifier or worker attempt when one was recorded |
| `kind` | no | `command` or `agent` |
| `command` | no | the check text |
| `required_capabilities_json` | no | capability list |
| `exit_code` | yes | command exit; NULL when the check was not a process |
| `passed` | no | 1 or 0 |
| `output_path` | yes | captured output path |
| `created_at` | no | insert time |

A retry deletes verification rows for that revision and task. The failed
transition remains in `task_events`. First-pass math has to look at both.

Other tables (`goals`, `evidence`, `routing_decisions`, `resource_status`,
inbox) are real and are not required for the six readings below.

## Timestamps, lifecycle, sessions, spans

Every timestamp ORX writes is UTC, ISO-8601, with a `T` separator, a
microsecond fraction, and an offset of `+00:00`
(`2026-10-04T01:00:00.000000+00:00`). A NULL timestamp was not recorded.
Readers do not replace it with `created_at`, `updated_at`, or "now".

SQLite's `julianday` does not accept the offset or a six-digit fraction.
The SQL in this contract truncates each stored value to
`YYYY-MM-DD HH:MM:SS` (the UTC clock, with the offset removed and no
zone conversion) and rounds the difference to whole seconds. Sub-second
duration is outside these examples. Stored text keeps its microseconds.

### Run creation, completion, reopen

- Insert of a run writes `planning` with `started_at` NULL and
  `completed_at` NULL. `created_at` is not `started_at`.
- `started_at` is stamped the first time status becomes `running`.
  Repeating `running` does not move it.
- `completed_at` is stamped the first time status becomes `done`.
  Repeating `done` does not move it. `updated_at` may still move.
  `updated_at` is never a completion time and is never copied into
  `completed_at`.
- Leaving `done` (reopen to `planning`, or any other status) clears
  `completed_at` and keeps `started_at`. Waiting time for a reopened run
  is unknown until a later `done` stamps `completed_at` again.
- Legacy rows: migration does not invent `runs.started_at`,
  `runs.completed_at`, `attempts.started_at`, `attempts.ended_at`,
  `attempts.session_ref`, or `attempts.usage_missing_reason`. A NULL in
  those columns on an old row stays unknown.

### Session references

`attempts.session_ref` is an opaque string. ORX stores it and does not
resolve it to a harness session, a transcript, or a ZCode thread. A NULL
means no reference was supplied or parsed. Readers must not synthesize
one from `request_id`, a numeric thread id, or a blank environment
value. Joining sessions to a vendor store is the external analysis
layer's job; the key it may carry out of this database is the opaque
text plus `project_id` and `attempt_id`.

### Execution spans and user waiting

An execution span is `ended_at - started_at` for one attempt, and only
when both stamps are present. The span sum adds those durations even
when the intervals overlap, and it includes attempts that ran before
`runs.started_at`. Overlap is not merged. A missing stamp drops that
attempt out of the sum and increments the incomplete count. If a run has
incomplete spans and no complete span, the sum is NULL (unknown), not
zero. A run with no attempts has a real zero sum and an incomplete count
of zero.

User waiting is `runs.completed_at - runs.started_at`, and only when
both are present. Otherwise waiting is NULL. It includes idle time
between attempts. It is not the span sum. Span sum may be larger than
waiting because of overlap and because of work outside the run window.
Waiting may be larger than the span sum because of idle time. Neither
number is derived from `created_at` or `updated_at`.

Attempts whose `run_id` is NULL are not folded into a run's span sum.
Their own span is reported on a row with `run_id` NULL. If their stamps
are missing, that span is unknown.

## Token fields, accuracy, and coverage

Token columns keep the runner's own meaning. Cursor cache reads are
stored in `cached_input_tokens` as reported, including when the number
is greater than `input_tokens`. Do not compute `input_tokens -
cached_input_tokens`. Do not rescale one harness onto another. Do not
convert tokens to money; this database has no billing basis, and a fee
column is not part of the contract.

NULL in a token column means that field was not in the observation. It
is not zero. A stored zero is a reported zero.

`accuracy` is a property of one observation:

| Value | Meaning |
|---|---|
| `exact` | the writer had all three counts as non-negative integers |
| `estimated` | the writer marked the figures as an estimate (`host_report` or `output_estimate`) |
| `unknown` | legal; a partial observation (a missing field) is `unknown`, and so is any observation the writer could not classify |

`unknown` is a successful stored state. Readers do not upgrade it.

Coverage is not accuracy. It always publishes its denominator.

**Attempt coverage** for a dimension (role, profile, or harness):

| Count | Definition |
|---|---|
| `attempts` | rows in `attempts` in that dimension (the denominator) |
| `observed` | those attempts with at least one `usage_observations` row |
| `missing_recorded` | no observation, and `usage_missing_reason` IS NOT NULL |
| `unassessed` | no observation, and `usage_missing_reason` IS NULL |

`observed + missing_recorded + unassessed = attempts`. An unassessed row
was not classified. Legacy rows are not backfilled with a reason. A NULL
reason does not mean the attempt was cheap, and it does not mean a
reason of `harness_omitted`. Recorded reasons are only:

| Reason | When it is stored |
|---|---|
| `adapter_unsupported` | the adapter has no usage hook |
| `harness_omitted` | the hook saw a finished stream and no usage event |
| `malformed_output` | a usage event was present and not usable |
| `truncated` | the parsed text ends in the capture truncation marker and no usage object survived |
| `execution_failure` | the launch failed or never started, and the stream had no usable usage object |

A stored observation clears `usage_missing_reason`. Host attempts are
not assessed by the CLI miss path unless something actually recorded a
miss or an observation.

**Partial-field coverage** uses stored observation rows as the
denominator, before the consumption collapse below. `input_present` /
`observations` is the coverage of that field. A row with `input_tokens`
set and `cached_input_tokens` NULL counts as present for input and
missing for cached. That missing cached value is why the row's accuracy
is `unknown` when the writer followed the capture rule. Field coverage
and the accuracy label are both reported; one does not replace the other.

### One consumption row per attempt

`usage_add` can insert more than one row per attempt. `host_report` is
idempotent per attempt for that source, and a conflicting second host
report is rejected at write time. Readers still collapse, because more
than one `native_cli` row can exist and because `native_cli` plus
`host_report` on the same attempt are the same work seen twice.

Consumption identity inside this file is `(attempt_id, source)`. The
canonical row for an attempt is the single observation with the best
source, then the lowest `usage_observations.id`:

| Order | `source` |
|---|---|
| 1 | `native_cli` |
| 2 | `host_report` |
| 3 | `output_estimate` |

Token sums add the canonical rows only. SQLite `SUM` skips NULL, so a
sum is a **partial sum** whenever the matching `*_missing` count is
greater than zero. Do not publish it as a complete total in that case.
Do not fill the gap with zero. Accuracy counts (`exact`, `estimated`,
`unknown`) are counts of canonical rows, not a single blended label.

The JSON `orx usage` object is a different aggregate. See below. The six
readings use this SQL, not that blend.

## Manual-ledger dedupe

A manual ledger is data the external tool holds. This repository does
not store it, does not create a ledger table, and does not implement the
dedupe product. The rule the external tool applies is:

- Identity is `(project_id, attempt_id, source)`.
- `source` is one of `native_cli`, `output_estimate`, `host_report`.
  A ledger line that was typed in and then recorded with `orx usage
  record` is `host_report`.
- If `usage_observations` already has that `attempt_id` and that
  `source`, the ledger line is a duplicate and is excluded from any sum.
- If it has the attempt but not that source, the line is eligible to be
  added by the external tool. This repository does not insert it.
- If the ledger line has no `attempt_id`, disposition is `unknown`. A
  short `task_id` is not an identity: `T001` can be two tasks. Do not
  attach the line to both.

```sql
-- query: manual_ledger_dedupe
WITH manual_ledger(attempt_id, task_id, source, input_tokens) AS (
  VALUES
    (5, 'T001', 'host_report', 100),
    (11, 'T003', 'host_report', 12),
    (NULL, 'T001', 'host_report', 9)
)
SELECT
  :project_id AS project_id,
  m.attempt_id,
  m.task_id,
  m.source,
  CASE
    WHEN m.attempt_id IS NULL THEN 'unknown'
    WHEN EXISTS (
      SELECT 1 FROM usage_observations u
      WHERE u.attempt_id = m.attempt_id AND u.source = m.source
    ) THEN 'excluded_duplicate'
    ELSE 'eligible'
  END AS ledger_disposition
FROM manual_ledger m
ORDER BY m.attempt_id IS NULL, m.attempt_id;
```

The `VALUES` clause is the consumer's ledger for one project, not a
table in `.orx/state.db`. The duplicate on attempt 5 matches the stored
`host_report` and drops out. The line with `task_id` `T001` and no
attempt stays one unknown row.

## JSON surfaces

These objects are what `orx status` and `orx usage` return inside the
CLI `--json` envelope (`{"ok": true, ...}` on success). They are not a
second schema. They open `Store`.

`status` run object: `id`, `status`, `started_at`, `completed_at`.
`created_at` and `updated_at` are not in that object. Task rows in
`status` are the active revision only. `session_ref` on each task is the
latest attempt for that task, or null.

`usage` adds these objects beside the per-profile aggregate:

| Object | Fields |
|---|---|
| `profiles[]` | `profile`, `tasks`, `runtime_sec`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `accuracy` |
| `observations[]` | `id`, `attempt_id`, `profile`, `run_id`, `task_id`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `source`, `accuracy`, `created_at`, `session_ref` |
| `coverage[]` | `profile`, `attempts`, `observed`, `measurement_accuracy` |
| `sessions[]` | `attempt`, `role`, `profile`, `task_id`, `run_id`, `session_ref`, `started_at`, `ended_at` |
| `runs[]` | `id`, `status`, `started_at`, `completed_at` |

Differences a SQL reader must not copy by accident:

- `profiles[].tasks` counts distinct `task_id` strings. `T001` on two
  revisions counts once. Delivery quality below does not.
- `profiles[].runtime_sec` adds 0 when either span stamp is missing. The
  span sum in this contract excludes that attempt and counts it
  incomplete instead. A 0 from JSON is not a measured duration.
- `profiles[].input_tokens` (and output, cached) is null when the
  profile has no observations, and also when any observation in the
  profile has that field null. A partial field suppresses the whole sum.
  `profiles[].accuracy` is `unknown` when no observation exists. When
  some attempts in the profile have no observation, that label becomes
  `estimated` unless the worst stored accuracy is already `unknown`, in
  which case it stays `unknown`. Coverage and accuracy are mixed in
  that one label.
- `coverage[].measurement_accuracy` is the worst accuracy among stored
  rows only, or `unknown` when that profile has no row. Unobserved
  attempts do not change it. `coverage[].observed` uses `attempts` as
  the denominator. That split is the JSON view of "accuracy and coverage
  are different". It is still per profile, not per role or per field.
- Cached values are returned as stored. The JSON does not subtract them
  from input and does not add a fee.
- `observations[].session_ref` is the attempt's opaque reference, not a
  column on `usage_observations`.

## Six readings

Each fence is one statement. Bind `:project_id`. The fixture numbers in
`tests/fixtures/observability/seed.sql` are the check values the tests
assert: repeated `T001`, a planner failure with `run_id`, a planner with
unknown run association, a missing observation, a NULL cached field, two
sources on one attempt, two `native_cli` rows on another, and overlapping
spans.

### Delivery quality

Terminal status of each task, keyed by revision row and short id.
Superseded revisions stay in the result. `passed_in_revision` /
`terminal_in_revision` is the quality ratio for that revision; the
denominator is tasks in `passed`, `failed`, or `cancelled`.

```sql
-- query: delivery_quality
SELECT
  :project_id AS project_id,
  pr.run_id,
  pr.id AS revision_row_id,
  pr.revision,
  t.task_id,
  t.id AS task_row_id,
  t.status,
  SUM(CASE WHEN t.status = 'passed' THEN 1 ELSE 0 END)
    OVER (PARTITION BY pr.id) AS passed_in_revision,
  SUM(CASE WHEN t.status IN ('passed', 'failed', 'cancelled') THEN 1 ELSE 0 END)
    OVER (PARTITION BY pr.id) AS terminal_in_revision
FROM tasks t
JOIN plan_revisions pr ON pr.id = t.revision_id
ORDER BY pr.run_id, pr.revision, t.id;
```

### Consumption

Canonical observation per attempt, then partial sums. `cached_missing > 0`
means `cached_input_tokens_sum` is not a complete total. The sum is
allowed to be greater than `input_tokens_sum`.

```sql
-- query: consumption
WITH ranked AS (
  SELECT
    u.*,
    ROW_NUMBER() OVER (
      PARTITION BY u.attempt_id
      ORDER BY CASE u.source
        WHEN 'native_cli' THEN 1
        WHEN 'host_report' THEN 2
        WHEN 'output_estimate' THEN 3
        ELSE 4
      END,
      u.id
    ) AS n
  FROM usage_observations u
),
canonical AS (
  SELECT * FROM ranked WHERE n = 1
)
SELECT
  :project_id AS project_id,
  COUNT(*) AS canonical_observations,
  SUM(input_tokens) AS input_tokens_sum,
  SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) AS input_missing,
  SUM(CASE WHEN input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS input_present,
  SUM(output_tokens) AS output_tokens_sum,
  SUM(CASE WHEN output_tokens IS NULL THEN 1 ELSE 0 END) AS output_missing,
  SUM(CASE WHEN output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS output_present,
  SUM(cached_input_tokens) AS cached_input_tokens_sum,
  SUM(CASE WHEN cached_input_tokens IS NULL THEN 1 ELSE 0 END) AS cached_missing,
  SUM(CASE WHEN cached_input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS cached_present,
  SUM(CASE WHEN accuracy = 'exact' THEN 1 ELSE 0 END) AS accuracy_exact,
  SUM(CASE WHEN accuracy = 'estimated' THEN 1 ELSE 0 END) AS accuracy_estimated,
  SUM(CASE WHEN accuracy = 'unknown' THEN 1 ELSE 0 END) AS accuracy_unknown
FROM canonical;
```

### Elapsed time

`span_sum_sec` is the sum of complete execution spans.
`waiting_sec` is user wall-clock from `started_at` to `completed_at`.
They are separate columns. Incomplete unattributed attempts come back as
`run_id` NULL and a NULL span sum.

```sql
-- query: elapsed
WITH attempt_spans AS (
  SELECT
    a.id AS attempt_id,
    a.run_id,
    CASE
      WHEN a.started_at IS NOT NULL AND a.ended_at IS NOT NULL THEN
        CAST(ROUND((
          julianday(substr(replace(a.ended_at, 'T', ' '), 1, 19))
          - julianday(substr(replace(a.started_at, 'T', ' '), 1, 19))
        ) * 86400) AS INTEGER)
      ELSE NULL
    END AS span_sec
  FROM attempts a
),
per_run AS (
  SELECT
    r.id AS run_id,
    SUM(s.span_sec) AS complete_sum,
    SUM(CASE WHEN s.span_sec IS NOT NULL THEN 1 ELSE 0 END) AS spans_complete,
    SUM(CASE WHEN s.run_id IS NOT NULL AND s.span_sec IS NULL THEN 1 ELSE 0 END)
      AS spans_incomplete,
    CASE
      WHEN r.started_at IS NOT NULL AND r.completed_at IS NOT NULL THEN
        CAST(ROUND((
          julianday(substr(replace(r.completed_at, 'T', ' '), 1, 19))
          - julianday(substr(replace(r.started_at, 'T', ' '), 1, 19))
        ) * 86400) AS INTEGER)
      ELSE NULL
    END AS waiting_sec
  FROM runs r
  LEFT JOIN attempt_spans s ON s.run_id = r.id
  GROUP BY r.id, r.started_at, r.completed_at
)
SELECT
  project_id,
  run_id,
  span_sum_sec,
  spans_complete,
  spans_incomplete,
  waiting_sec
FROM (
  SELECT
    :project_id AS project_id,
    run_id,
    CASE
      WHEN spans_incomplete > 0 AND COALESCE(spans_complete, 0) = 0 THEN NULL
      ELSE COALESCE(complete_sum, 0)
    END AS span_sum_sec,
    COALESCE(spans_complete, 0) AS spans_complete,
    COALESCE(spans_incomplete, 0) AS spans_incomplete,
    waiting_sec
  FROM per_run
  UNION ALL
  SELECT
    :project_id AS project_id,
    NULL AS run_id,
    CASE
      WHEN SUM(CASE WHEN span_sec IS NULL THEN 1 ELSE 0 END) > 0
       AND SUM(CASE WHEN span_sec IS NOT NULL THEN 1 ELSE 0 END) = 0
      THEN NULL
      ELSE SUM(span_sec)
    END AS span_sum_sec,
    COALESCE(SUM(CASE WHEN span_sec IS NOT NULL THEN 1 ELSE 0 END), 0) AS spans_complete,
    COALESCE(SUM(CASE WHEN span_sec IS NULL THEN 1 ELSE 0 END), 0) AS spans_incomplete,
    NULL AS waiting_sec
  FROM attempt_spans
  WHERE run_id IS NULL
) AS elapsed_rows
-- A compound SELECT may only ORDER BY result columns. The wrapper is a
-- simple SELECT, so NULL run_id rows sort last without that restriction.
ORDER BY elapsed_rows.run_id IS NULL, elapsed_rows.run_id;
```

Per-attempt spans, so a consumer can see that overlapping intervals were
added and not merged:

```sql
-- query: elapsed_spans
SELECT
  :project_id AS project_id,
  a.id AS attempt_id,
  a.run_id,
  a.revision_id AS revision_row_id,
  a.task_id,
  a.started_at,
  a.ended_at,
  CASE
    WHEN a.started_at IS NOT NULL AND a.ended_at IS NOT NULL THEN
      CAST(ROUND((
        julianday(substr(replace(a.ended_at, 'T', ' '), 1, 19))
        - julianday(substr(replace(a.started_at, 'T', ' '), 1, 19))
      ) * 86400) AS INTEGER)
    ELSE NULL
  END AS span_sec
FROM attempts a
ORDER BY a.id;
```

### First acceptance pass rate

Denominator: tasks that have at least one remaining verification row,
identified by `(revision_id, task_id)`, not by `task_id` alone.
Numerator: the earliest remaining verification (`created_at`, then
`verifications.id`) has `passed = 1`, and `task_events` has no
`to_status = 'failed'` for that same pair.

The event clause is required because retry deletes verification rows.
A later pass with a surviving failed event is not a first pass. Command
and agent rows both count. A task that never reached verification is
outside the rate, not a failure.

```sql
-- query: first_acceptance
WITH first_ver AS (
  SELECT
    revision_id,
    task_id,
    passed,
    ROW_NUMBER() OVER (
      PARTITION BY revision_id, task_id
      ORDER BY created_at, id
    ) AS n
  FROM verifications
),
failed_events AS (
  SELECT revision_id, task_id, COUNT(*) AS failed_events
  FROM task_events
  WHERE to_status = 'failed'
  GROUP BY revision_id, task_id
),
judged AS (
  SELECT
    fv.revision_id,
    fv.task_id,
    CASE
      WHEN fv.passed = 1 AND COALESCE(fe.failed_events, 0) = 0 THEN 1
      ELSE 0
    END AS first_pass
  FROM first_ver fv
  LEFT JOIN failed_events fe
    ON fe.revision_id = fv.revision_id AND fe.task_id = fv.task_id
  WHERE fv.n = 1
)
SELECT
  :project_id AS project_id,
  pr.run_id,
  pr.revision,
  j.task_id,
  t.id AS task_row_id,
  j.first_pass,
  t.status AS final_status,
  SUM(j.first_pass) OVER () AS first_pass_numerator,
  COUNT(*) OVER () AS first_pass_denominator
FROM judged j
JOIN tasks t ON t.revision_id = j.revision_id AND t.task_id = j.task_id
JOIN plan_revisions pr ON pr.id = j.revision_id
ORDER BY pr.revision, t.id;
```

### Rework with reasons

One row per failed task transition. The reason is the text ORX stored.
NULL means the event had no reason (unknown), not an empty explanation.
Attempt `failure_reason` can repeat the same fact and is not unioned in.
The same short `task_id` on another revision is a different row.

```sql
-- query: rework
SELECT
  :project_id AS project_id,
  pr.run_id,
  pr.revision,
  e.task_id,
  t.id AS task_row_id,
  e.reason
FROM task_events e
JOIN plan_revisions pr ON pr.id = e.revision_id
JOIN tasks t ON t.revision_id = e.revision_id AND t.task_id = e.task_id
WHERE e.to_status = 'failed'
ORDER BY e.id;
```

### Coverage by dimension

Attempt coverage by role, profile, and harness. Field coverage is the
next statement: its denominator is stored observation rows, so a second
`native_cli` row counts there and does not count twice in consumption.

```sql
-- query: coverage
WITH attempt_coverage AS (
  SELECT
    a.role,
    a.profile,
    a.harness,
    CASE
      WHEN EXISTS (
        SELECT 1 FROM usage_observations u WHERE u.attempt_id = a.id
      ) THEN 1 ELSE 0
    END AS observed,
    CASE
      WHEN NOT EXISTS (
        SELECT 1 FROM usage_observations u WHERE u.attempt_id = a.id
      ) AND a.usage_missing_reason IS NOT NULL THEN 1 ELSE 0
    END AS missing_recorded,
    CASE
      WHEN NOT EXISTS (
        SELECT 1 FROM usage_observations u WHERE u.attempt_id = a.id
      ) AND a.usage_missing_reason IS NULL THEN 1 ELSE 0
    END AS unassessed
  FROM attempts a
)
SELECT
  :project_id AS project_id,
  'role' AS dimension,
  role AS dimension_value,
  COUNT(*) AS attempts,
  SUM(observed) AS observed,
  SUM(missing_recorded) AS missing_recorded,
  SUM(unassessed) AS unassessed
FROM attempt_coverage
GROUP BY role
UNION ALL
SELECT
  :project_id,
  'profile',
  profile,
  COUNT(*),
  SUM(observed),
  SUM(missing_recorded),
  SUM(unassessed)
FROM attempt_coverage
GROUP BY profile
UNION ALL
SELECT
  :project_id,
  'harness',
  harness,
  COUNT(*),
  SUM(observed),
  SUM(missing_recorded),
  SUM(unassessed)
FROM attempt_coverage
GROUP BY harness
ORDER BY dimension, dimension_value;
```

```sql
-- query: coverage_fields
SELECT
  :project_id AS project_id,
  'token_field' AS dimension,
  COUNT(*) AS observations,
  SUM(CASE WHEN input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS input_present,
  SUM(CASE WHEN output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS output_present,
  SUM(CASE WHEN cached_input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS cached_present,
  SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) AS input_missing,
  SUM(CASE WHEN output_tokens IS NULL THEN 1 ELSE 0 END) AS output_missing,
  SUM(CASE WHEN cached_input_tokens IS NULL THEN 1 ELSE 0 END) AS cached_missing
FROM usage_observations;
```
