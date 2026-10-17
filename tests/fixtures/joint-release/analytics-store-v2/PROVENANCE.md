# Analytics store v2 sample (G011 joint release)

Generated 2026-10-17T12:24:17.606026+00:00 by:

    uv run python scripts/joint_release.py generate-samples --dest tests/fixtures/joint-release

- Source: `git archive 3f1b66c0234db3cf84d6c2957e34d00bab1b5cee` of the isolated analytics clone (.release-workspaces/orx-analytics) — the LAST commit before the store v2 → v3 migration (07ab676). The analytics repo has no tags; the full commit is the anchor.
- The analytics database is written exclusively by that REAL old code (its own `Store`, `registry`, `import_orx_project`); the generator asserts `orxanalytics.__version__ == "0.4.1"` and reads back `meta.store_version == "2"` before anything is committed. It is never a downgrade of a current store's version number.
- Generator orxanalytics version: 0.4.1 (from /Users/flb/Sources/Products/ORX/dist/g011/generate-work/old-src/src/orxanalytics/__init__.py).
- Imported ORX sample: the committed real-v0.3.0 fixture `tests/fixtures/release-0.3.0` (tag v0.3.0, commit 0f2629e6988ba5082a876257c910d78457bd1338), staged as `<tmp>/.orx/{state.db,config.toml,profiles.toml}` with the copy normalized to a rollback journal (self-contained for the read-only open). The fixture's own sha256 is asserted unchanged before and after.
- `meta.store_version` at generation: 2.
- Table row counts: {"cur_composers": 0, "cur_workspaces": 0, "cx_threads": 0, "cx_turn_ctx": 0, "cx_turn_events": 0, "cx_usage": 0, "finding_fixes": 0, "finding_refs": 0, "findings": 0, "import_runs": 1, "import_state": 0, "links": 0, "meta": 1, "orx_attempt_progress": 0, "orx_attempts": 3, "orx_evidence": 1, "orx_goals": 1, "orx_replan_maps": 0, "orx_replan_sources": 0, "orx_replan_superseded": 0, "orx_replan_task_maps": 0, "orx_revisions": 1, "orx_runs": 1, "orx_snapshots": 1, "orx_task_events": 9, "orx_tasks": 2, "orx_usage_obs": 1, "orx_verifications": 2, "projects": 1, "sources": 0, "zc_first_parts": 0, "zc_requests": 0, "zc_sessions": 0, "zc_tools": 0, "zc_turns": 0}.
- analytics.db sha256: `2a7d987324d00d8886699a4affa0eefe90f36c0edde3500c8ccd55301db9aaaf`.
- Regeneration comparison policy: `verify_samples` re-runs this exact generation and compares normalized logical dumps (generated uuids/timestamps and the generation root path are masked); the committed sample must match.
- Consumers must treat this directory as READ-ONLY: the v2 → v3 upgrade checks copy `analytics.db` first and assert the source digest is unchanged afterwards.
