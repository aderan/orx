-- Schema v9 subset for the observability read contract.
-- Column names and nullability match src/orx/state.py. This file is data
-- only: applying it does not open Store and does not migrate anything.
--
-- One database is one project. project_id is not a column; the consumer
-- binds it. Short ids repeat on purpose:
--   tasks.task_id T001 exists on revision 1 (tasks.id 10) and revision 2
--   (tasks.id 20).
-- Planner attempt 1 failed before any revision and is still tied to R001.
-- Planner attempt 2 has no revision, assignment, or run_id (unknown).
-- Attempt 5 stores native_cli and host_report. Attempt 8 stores two
-- native_cli rows; the later one must not be added.
-- Attempt 4 has cached_input_tokens NULL.
-- Attempts 4 and 5 overlap in time. Run R001's updated_at is later than
-- completed_at. R002 never started. R003 started and was reopened.
-- Revision 1 T001 is a retry story: worker attempt 4's round failed its
-- command gate (verification row 9, red, bound to attempt 4), the retry
-- routed worker attempt 5 whose round ran the same command green (row 10),
-- and the agent verifier (attempt 6, row 1) then failed the round. Rows 9
-- and 10 are the per-attempt history the pre-R002 semantics deleted on
-- retry; they stay, and only attempt 5's window is the current result.
--
-- v9 (G004) replan subset: revision 2 declares the correspondence that made
-- it effective — 2:T001 redoes 1:T001 (the round that failed its gate and
-- the agent verdict), every other revision 1 task is dropped with a note.
-- Artifact provenance rows hang on that declared edge and distinguish the
-- source task's two attempts (4 = the failed round, 5 = the retry) plus the
-- retry's completion evidence (evidence row 1). Identity everywhere is
-- (run, revision, task_id) and attempt/evidence row ids — the bare task
-- number T001, which exists on both revisions, resolves nothing.

PRAGMA foreign_keys = ON;

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE goals (
  id TEXT PRIMARY KEY,
  objective TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '[]',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active','done','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id),
  status TEXT NOT NULL CHECK (status IN ('planning','running','blocked','done')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
);

CREATE TABLE plan_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision INTEGER NOT NULL,
  depth TEXT NOT NULL,
  planner_profile TEXT NOT NULL,
  ir_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','superseded')),
  created_at TEXT NOT NULL,
  UNIQUE(run_id, revision)
);

CREATE TABLE planning_assignments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  profile TEXT NOT NULL,
  depth TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('waiting_host','submitted','failed','cancelled')),
  prompt TEXT NOT NULL,
  created_at TEXT NOT NULL,
  submitted_at TEXT
);

CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  objective TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  acceptance_json TEXT NOT NULL,
  verification_json TEXT NOT NULL,
  preread_json TEXT NOT NULL DEFAULT '[]',
  routing_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','runnable','running','waiting_host',
                                         'waiting_external','verifying','passed','failed',
                                         'blocked','cancelled')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(revision_id, task_id)
);

CREATE TABLE task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  event TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER REFERENCES plan_revisions(id),
  task_id TEXT,
  assignment_id TEXT,
  role TEXT NOT NULL,
  profile TEXT NOT NULL,
  driver TEXT NOT NULL,
  harness TEXT NOT NULL,
  model TEXT NOT NULL,
  requested_effort TEXT NOT NULL,
  actual_effort TEXT,
  effort_source TEXT,
  fallback_used INTEGER NOT NULL DEFAULT 0,
  routing_reason TEXT,
  started_at TEXT,
  ended_at TEXT,
  result TEXT,
  failure_reason TEXT,
  isolation TEXT,
  session_ref TEXT,
  run_id TEXT,
  usage_missing_reason TEXT
);

CREATE TABLE verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id INTEGER REFERENCES attempts(id),
  kind TEXT NOT NULL CHECK (kind IN ('command','agent')),
  command TEXT NOT NULL,
  required_capabilities_json TEXT NOT NULL DEFAULT '[]',
  exit_code INTEGER,
  passed INTEGER NOT NULL,
  output_path TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE usage_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  profile TEXT NOT NULL,
  run_id TEXT NOT NULL,
  task_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_input_tokens INTEGER,
  source TEXT NOT NULL CHECK (source IN ('native_cli', 'output_estimate', 'host_report')),
  accuracy TEXT NOT NULL CHECK (accuracy IN ('exact', 'estimated', 'unknown')),
  created_at TEXT NOT NULL
);

CREATE TABLE evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- v9 (G004) replan storage subset: correspondence, reports, provenance.

CREATE TABLE replan_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  prior_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(revision_id)
);

CREATE TABLE replan_task_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES replan_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  classification TEXT NOT NULL CHECK (classification IN ('new','confirm','redo','continue')),
  redo_reason TEXT,
  confirm_verification_json TEXT NOT NULL DEFAULT '[]',
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(revision_id, task_id)
);

CREATE TABLE replan_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_mapping_id INTEGER NOT NULL REFERENCES replan_task_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  source_task_row_id INTEGER NOT NULL REFERENCES tasks(id),
  part INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(task_mapping_id, source_revision, source_task_id)
);

CREATE TABLE replan_superseded (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES replan_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  source_task_row_id INTEGER NOT NULL REFERENCES tasks(id),
  disposition TEXT NOT NULL CHECK (disposition IN ('confirmed','continued','redone','split','merged','dropped')),
  successors_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(mapping_id, source_revision, source_task_id)
);

CREATE TABLE replan_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  prior_revision INTEGER NOT NULL,
  revision_id INTEGER REFERENCES plan_revisions(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE replan_artifact_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  artifact TEXT NOT NULL,
  attempt_id INTEGER REFERENCES attempts(id),
  evidence_id INTEGER REFERENCES evidence(id),
  created_at TEXT NOT NULL
);

INSERT INTO meta(key, value) VALUES ('schema_version', '9');

INSERT INTO goals(id, objective, status, created_at, updated_at)
VALUES ('G001', 'fixture', 'done',
        '2026-10-04T00:00:00.000000+00:00',
        '2026-10-04T04:00:00.000000+00:00');

INSERT INTO runs(id, goal_id, status, created_at, updated_at, started_at, completed_at)
VALUES
  ('R001', 'G001', 'done',
   '2026-10-04T00:00:00.000000+00:00',
   '2026-10-04T04:00:00.000000+00:00',
   '2026-10-04T01:00:00.000000+00:00',
   '2026-10-04T03:00:00.000000+00:00'),
  ('R002', 'G001', 'planning',
   '2026-10-03T00:00:00.000000+00:00',
   '2026-10-03T12:00:00.000000+00:00',
   NULL, NULL),
  ('R003', 'G001', 'planning',
   '2026-10-04T05:00:00.000000+00:00',
   '2026-10-04T06:00:00.000000+00:00',
   '2026-10-04T05:10:00.000000+00:00',
   NULL);

INSERT INTO plan_revisions(id, run_id, revision, depth, planner_profile, ir_json, status, created_at)
VALUES
  (1, 'R001', 1, 'standard', 'orx-host', '{}', 'superseded',
   '2026-10-04T01:05:00.000000+00:00'),
  (2, 'R001', 2, 'standard', 'orx-host', '{}', 'active',
   '2026-10-04T02:05:00.000000+00:00');

INSERT INTO planning_assignments(id, run_id, profile, depth, status, prompt, created_at, submitted_at)
VALUES
  ('P001', 'R001', 'orx-host', 'standard', 'submitted', 'fixture',
   '2026-10-04T00:40:00.000000+00:00', '2026-10-04T01:05:00.000000+00:00'),
  ('P002', 'R001', 'cursor-economy', 'standard', 'failed', 'fixture',
   '2026-10-04T00:20:00.000000+00:00', NULL),
  ('P003', 'R002', 'orx-host', 'standard', 'waiting_host', 'fixture',
   '2026-10-03T00:10:00.000000+00:00', NULL);

INSERT INTO tasks(
  id, revision_id, task_id, objective, scope_json, acceptance_json,
  verification_json, preread_json, routing_json, status, failure_reason,
  created_at, updated_at
) VALUES
  (10, 1, 'T001', 'first revision', '{}', '[]', '[]', '[]', '{}', 'failed',
   'tests failed: missing contract',
   '2026-10-04T01:05:00.000000+00:00', '2026-10-04T02:10:00.000000+00:00'),
  (11, 1, 'T002', 'clean pass', '{}', '[]', '[]', '[]', '{}', 'passed', NULL,
   '2026-10-04T01:05:00.000000+00:00', '2026-10-04T01:30:00.000000+00:00'),
  (12, 1, 'T003', 'passed after a failed verification', '{}', '[]', '[]', '[]', '{}',
   'passed', NULL,
   '2026-10-04T01:05:00.000000+00:00', '2026-10-04T02:05:00.000000+00:00'),
  (13, 1, 'T004', 'retry cleared verification rows', '{}', '[]', '[]', '[]', '{}',
   'passed', NULL,
   '2026-10-04T01:05:00.000000+00:00', '2026-10-04T02:35:00.000000+00:00'),
  (20, 2, 'T001', 'same short id, next revision', '{}', '[]', '[]', '[]', '{}',
   'passed', NULL,
   '2026-10-04T02:05:00.000000+00:00', '2026-10-04T02:50:00.000000+00:00');

INSERT INTO task_events(id, revision_id, task_id, from_status, to_status, event, reason, created_at)
VALUES
  (1, 1, 'T001', 'running', 'failed', 'fail',
   'tests failed: missing contract', '2026-10-04T01:40:00.000000+00:00'),
  (2, 1, 'T001', 'verifying', 'failed', 'fail',
   'agent verifier: contract section absent', '2026-10-04T02:10:00.000000+00:00'),
  (3, 1, 'T003', 'verifying', 'failed', 'fail',
   'agent verifier: fixture gap', '2026-10-04T01:55:00.000000+00:00'),
  (4, 1, 'T004', 'verifying', 'failed', 'fail',
   'agent verifier: stale result cleared on retry', '2026-10-04T02:15:00.000000+00:00');

INSERT INTO attempts(
  id, revision_id, task_id, assignment_id, role, profile, driver, harness, model,
  requested_effort, started_at, ended_at, result, failure_reason, session_ref, run_id,
  usage_missing_reason
) VALUES
  (1, NULL, NULL, 'P002', 'planner', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T00:30:00.000000+00:00', '2026-10-04T00:50:00.000000+00:00',
   'failed', 'planner rejected', NULL, 'R001', 'harness_omitted'),
  (2, NULL, NULL, NULL, 'planner', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   NULL, NULL, 'failed', NULL, NULL, NULL, NULL),
  (3, 1, NULL, 'P001', 'planner', 'codex-strong', 'cli', 'codex', 'm', 'medium',
   '2026-10-04T00:50:00.000000+00:00', '2026-10-04T01:05:00.000000+00:00',
   'completed', NULL, '11111111-1111-4111-8111-111111111111', 'R001', NULL),
  (4, 1, 'T001', NULL, 'worker', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T01:10:00.000000+00:00', '2026-10-04T01:40:00.000000+00:00',
   'failed', 'tests failed: missing contract', NULL, 'R001', NULL),
  (5, 1, 'T001', NULL, 'worker', 'orx-host', 'host', 'zcode', 'm', 'medium',
   '2026-10-04T01:30:00.000000+00:00', '2026-10-04T02:00:00.000000+00:00',
   'completed', NULL, 'host-sess-7', 'R001', NULL),
  (6, 1, 'T001', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T02:00:00.000000+00:00', '2026-10-04T02:10:00.000000+00:00',
   'fail', 'agent verifier: contract section absent', NULL, 'R001', 'harness_omitted'),
  (7, 1, 'T002', NULL, 'worker', 'shell-local', 'cli', 'shell', 'm', 'medium',
   '2026-10-04T01:00:00.000000+00:00', '2026-10-04T01:20:00.000000+00:00',
   'completed', NULL, NULL, 'R001', 'adapter_unsupported'),
  (8, 1, 'T002', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T01:20:00.000000+00:00', '2026-10-04T01:30:00.000000+00:00',
   'pass', NULL, '22222222-2222-4222-8222-222222222222', 'R001', NULL),
  (9, 2, 'T001', NULL, 'worker', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T02:10:00.000000+00:00', '2026-10-04T02:40:00.000000+00:00',
   'completed', NULL, NULL, 'R001', NULL),
  (10, 2, 'T001', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T02:40:00.000000+00:00', '2026-10-04T02:50:00.000000+00:00',
   'pass', NULL, NULL, 'R001', NULL),
  (11, 1, 'T003', NULL, 'worker', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T01:40:00.000000+00:00', '2026-10-04T01:50:00.000000+00:00',
   'completed', NULL, NULL, 'R001', NULL),
  (12, 1, 'T003', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T01:50:00.000000+00:00', '2026-10-04T01:55:00.000000+00:00',
   'fail', 'agent verifier: fixture gap', NULL, 'R001', 'truncated'),
  (13, 1, 'T003', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T01:55:00.000000+00:00', '2026-10-04T02:05:00.000000+00:00',
   'pass', NULL, NULL, 'R001', NULL),
  (14, 1, 'T004', NULL, 'worker', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T02:20:00.000000+00:00', '2026-10-04T02:30:00.000000+00:00',
   'completed', NULL, NULL, 'R001', NULL),
  (15, 1, 'T004', NULL, 'verifier', 'cursor-economy', 'cli', 'cursor', 'm', 'medium',
   '2026-10-04T02:30:00.000000+00:00', '2026-10-04T02:35:00.000000+00:00',
   'pass', NULL, NULL, 'R001', NULL);

INSERT INTO verifications(
  id, revision_id, task_id, attempt_id, kind, command, passed, created_at
) VALUES
  (1, 1, 'T001', 6, 'agent', 'agent: the summary is honest', 0,
   '2026-10-04T02:10:00.000000+00:00'),
  (2, 1, 'T002', 7, 'command', 'true', 1,
   '2026-10-04T01:20:00.000000+00:00'),
  (3, 1, 'T002', 8, 'agent', 'agent: the summary is honest', 1,
   '2026-10-04T01:30:00.000000+00:00'),
  (4, 1, 'T003', 12, 'agent', 'agent: the summary is honest', 0,
   '2026-10-04T01:55:00.000000+00:00'),
  (5, 1, 'T003', 13, 'agent', 'agent: the summary is honest', 1,
   '2026-10-04T02:05:00.000000+00:00'),
  (6, 1, 'T004', 15, 'agent', 'agent: the summary is honest', 1,
   '2026-10-04T02:35:00.000000+00:00'),
  (7, 2, 'T001', 9, 'command', 'true', 1,
   '2026-10-04T02:40:00.000000+00:00'),
  (8, 2, 'T001', 10, 'agent', 'agent: the summary is honest', 1,
   '2026-10-04T02:50:00.000000+00:00'),
  -- Retry history for revision 1 T001 (kept per attempt, not deleted):
  -- attempt 4's round ran the gate red, the retry's attempt 5 ran the same
  -- command green. Same command, two attempts, two rows, two log files.
  (9, 1, 'T001', 4, 'command', 'pytest tests/ -q', 0,
   '2026-10-04T01:40:00.000000+00:00'),
  (10, 1, 'T001', 5, 'command', 'pytest tests/ -q', 1,
   '2026-10-04T02:00:00.000000+00:00');

INSERT INTO usage_observations(
  id, attempt_id, profile, run_id, task_id, input_tokens, output_tokens,
  cached_input_tokens, source, accuracy, created_at
) VALUES
  (1, 3, 'codex-strong', 'R001', NULL, 10, 4, 500, 'native_cli', 'exact',
   '2026-10-04T01:05:00.000000+00:00'),
  (2, 4, 'cursor-economy', 'R001', 'T001', 8, 2, NULL, 'native_cli', 'unknown',
   '2026-10-04T01:40:00.000000+00:00'),
  (3, 5, 'orx-host', 'R001', 'T001', 100, 10, 400, 'native_cli', 'exact',
   '2026-10-04T02:00:00.000000+00:00'),
  (4, 5, 'orx-host', 'R001', 'T001', 100, 10, 400, 'host_report', 'estimated',
   '2026-10-04T02:01:00.000000+00:00'),
  (5, 8, 'cursor-economy', 'R001', 'T002', 3, 1, 9, 'native_cli', 'exact',
   '2026-10-04T01:30:00.000000+00:00'),
  (6, 9, 'cursor-economy', 'R001', 'T001', 7, 1, 0, 'output_estimate', 'estimated',
   '2026-10-04T02:40:00.000000+00:00'),
  (7, 8, 'cursor-economy', 'R001', 'T002', 99, 99, 99, 'native_cli', 'exact',
   '2026-10-04T01:31:00.000000+00:00');

-- The retry's completion evidence (attempt 5, the round that ran the gate
-- green before the agent verdict failed the task).
INSERT INTO evidence(id, attempt_id, kind, path, created_at)
VALUES (1, 5, 'completion', '.orx/runs/R001/evidence/T001-5.json',
        '2026-10-04T02:00:30.000000+00:00');

-- Revision 2's declared correspondence (G004 v9): 2:T001 redoes 1:T001;
-- every other revision 1 task is dropped with a note. Source identity is
-- the (revision, task_id) pair plus the resolved tasks row id.
INSERT INTO replan_mappings(id, run_id, revision_id, prior_revision, created_at)
VALUES (1, 'R001', 2, 1, '2026-10-04T02:04:00.000000+00:00');

INSERT INTO replan_task_mappings(
  id, mapping_id, run_id, revision_id, task_id, classification, redo_reason,
  confirm_verification_json, artifacts_json, created_at
) VALUES
  (1, 1, 'R001', 2, 'T001', 'redo',
   'revision 1 T001 failed its command gate and the agent verdict; the restructured plan does the work again',
   '[]', '["docs/observability-contract.md"]',
   '2026-10-04T02:04:00.000000+00:00');

INSERT INTO replan_sources(
  id, task_mapping_id, run_id, source_revision, source_task_id,
  source_task_row_id, part, created_at
) VALUES
  (1, 1, 'R001', 1, 'T001', 10, 0, '2026-10-04T02:04:00.000000+00:00');

INSERT INTO replan_superseded(
  id, mapping_id, run_id, source_revision, source_task_id, source_task_row_id,
  disposition, successors_json, note, created_at
) VALUES
  (1, 1, 'R001', 1, 'T001', 10, 'redone', '["T001"]', NULL,
   '2026-10-04T02:04:00.000000+00:00'),
  (2, 1, 'R001', 1, 'T002', 11, 'dropped', '[]',
   'clean pass retired with revision 1',
   '2026-10-04T02:04:00.000000+00:00'),
  (3, 1, 'R001', 1, 'T003', 12, 'dropped', '[]',
   'first-acceptance fixture row retired with revision 1',
   '2026-10-04T02:04:00.000000+00:00'),
  (4, 1, 'R001', 1, 'T004', 13, 'dropped', '[]',
   'retry-history fixture row retired with revision 1',
   '2026-10-04T02:04:00.000000+00:00');

-- The preflight report produced before revision 2 took effect; bound to the
-- revision that landed.
INSERT INTO replan_reports(
  id, run_id, prior_revision, revision_id, payload_json, created_at
) VALUES
  (1, 'R001', 1, 2,
   '{"correspondence": [{"from": "1:T001", "to": "2:T001", "classification": "redo"}], "classification_counts": {"new": 0, "confirm": 0, "redo": 1, "continue": 0}, "dropped": ["1:T002", "1:T003", "1:T004"]}',
   '2026-10-04T02:03:00.000000+00:00');

-- Traceable artifact provenance on the declared (2:T001 <-> 1:T001) edge:
-- the same source task's two attempts stay distinct rows (4 = the round
-- that failed the gate, 5 = the retry), and evidence row 1 binds the
-- retry's completion. The task number T001 appears on both revisions; only
-- the (revision, task_id, attempt) identity resolves these rows.
INSERT INTO replan_artifact_sources(
  id, run_id, revision_id, task_id, source_revision, source_task_id, artifact,
  attempt_id, evidence_id, created_at
) VALUES
  (1, 'R001', 2, 'T001', 1, 'T001', '.orx/runs/R001/check/T001/0001-a4-command.log',
   4, NULL, '2026-10-04T02:04:00.000000+00:00'),
  (2, 'R001', 2, 'T001', 1, 'T001', '.orx/runs/R001/check/T001/0002-a5-command.log',
   5, NULL, '2026-10-04T02:04:00.000000+00:00'),
  (3, 'R001', 2, 'T001', 1, 'T001', '.orx/runs/R001/evidence/T001-5.json',
   5, 1, '2026-10-04T02:04:00.000000+00:00');
