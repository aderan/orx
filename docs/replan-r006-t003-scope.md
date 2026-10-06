# R006 replan context: cover tests/test_inbox.py schema-assert sync gap

## Reason (what happened)

T002 (attempt 104, verdict passed, commit 4bd0c4b) raised the DB schema
version 9 -> 10. Three existing test files assert the current schema
version / table set and needed the routine sync; T002's allowed scope
covered two of them (`tests/test_m12_acceptance.py`,
`tests/test_subagent_contract.py`) and its worker correctly refused to
touch the rest:

- `tests/test_lifecycle.py:134` (`== 9` -> `== 10`) — inside T003 scope. Covered.
- `tests/test_timeline.py` (`SCHEMA_TABLES` missing `"attempt_progress"`) — inside T004 scope. Covered.
- `tests/test_inbox.py:401` (`reopened.schema_version() == 9` in
  `test_v2_to_v4_migration_preserves_rows`) — **not inside any task's
  allowed scope in revision 1.**

Consequence: T005's prescribed verification includes the full
`uv run pytest -q`, so under revision 1 T005's delivery gate can never go
green (currently 632 passed / 3 failed; after T003 and T004 land their
in-scope syncs exactly one failure, `tests/test_inbox.py`, would remain
and no task may write that file). This is a plan/verification
disagreement caused by a wrong assumption at plan time: the plan assumed
the schema-version asserts lived only in the files it listed.

## Intent (what should change)

Minimal revision 2 with the same five tasks, same objectives, same
acceptance, same dependency chain T001 -> T002 -> T003 -> T004 -> T005:

- T001: confirm — passed under revision 1 (attempt 103, verification
  passed; commit 3a03e15). Cite its recorded verification/evidence.
- T002: confirm — passed under revision 1 (attempt 104, verification
  4 passed / 0 failed; commit 4bd0c4b). Cite its recorded
  verification/evidence.
- T003: continue (not started) with ONE scope addition:
  `tests/test_inbox.py` joins `scope.allowed` so the one-line
  `== 9` -> `== 10` sync lands with the earliest in-scope sync
  (`tests/test_lifecycle.py` is already in T003's scope and carries the
  same class of fix). Objective, acceptance, verification commands, and
  dependencies unchanged.
- T004: continue — identical to revision 1 (not started).
- T005: continue — identical to revision 1 (not started).

## What must NOT change

- Goal text, objective, acceptance, constraints — untouched.
- No new task, no dependency change, no verification-command change.
- No reopen of T001/T002 work: their passes are recorded history cited
  as artifacts through the confirm correspondence; nothing re-executes.
- T003's objective/acceptance/verification stay exactly as revision 1;
  only `scope.allowed` grows by `tests/test_inbox.py`.

## Supporting material

- Full-suite state after T002: 632 passed / 3 failed
  (`uv run pytest -q`), failures listed above.
- T005 verification (revision 1): `["uv run pytest -q", "uv build", agent check ...]`.
- Revision 1 scopes (from plan IR):
  - T003 allowed: src/orx/dispatch.py, src/orx/cli.py, tests/test_cli.py, tests/test_lifecycle.py, tests/test_session_identity.py
  - T005 allowed: tests/test_host_progress_e2e.py, README.md, skills/orx-agent/SKILL.md, skills/orx-controller/SKILL.md, docs/host-progress-contract.md, docs/host-progress-report.md, IMPLEMENTATION_PLAN.md
- The one-line fix itself (for reference, to land inside T003):
  `tests/test_inbox.py` line 401: `assert reopened.schema_version() == 9`
  -> `== 10`.
