# v0.3.0 sample fixture (schema v8)

Generated 2026-10-06T15:51:57.881320+00:00 by:

    uv run python scripts/check_release.py --generate-fixture tests/fixtures/release-0.3.0

- Source: `git archive v0.3.0` of this repository — commit 0f2629e6988ba5082a876257c910d78457bd1338.
- The database file is written exclusively by the REAL v0.3.0 code from that archive (its own `Store`/`dispatch`); the generator asserts `orx.__version__ == "0.3.0"`. It is never a downgrade of the current schema.
- Generator orx version: 0.3.0 (from /tmp/orx-fixture-gen/gen/v030-src/src/orx/__init__.py).
- `meta.schema_version` at generation: 8.
- Table row counts: {"attempts": 3, "evidence": 1, "external_events": 0, "goals": 1, "inbox_items": 0, "meta": 1, "plan_revisions": 1, "planning_assignments": 0, "resource_status": 1, "routing_decisions": 3, "runs": 1, "task_dependencies": 1, "task_events": 9, "tasks": 2, "usage_observations": 1, "verifications": 2}.
- state.db sha256: `ec5cbf9164b733f8a26099fd81d6fb351ebff5d58db1802bc4f500f32c74901d`.
- config.toml sha256: `7d4f47142560fdd4b2f5e396ca6bd662553d7891a6cd0b793c0f2c34918d303f`; profiles.toml sha256: `6e710515a2b01c124c0aae6ef24d74cd9b9c51431d7fadc9a3be3d145eb396cc`.
- In-flight state: T001 passed with legacy evidence + verdict; T002 claimed and RUNNING — the realistic mid-flight upgrade
  scenario of docs/upgrade-0.3.1.md section 6.
- Consumers must treat this directory as READ-ONLY: upgrade and restore checks copy `state.db` first and assert the source digest is unchanged afterwards.
