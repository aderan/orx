"""Cross-package acceptance using real ORX dispatch and its current-row oracle.

Run with: uv run --locked pytest -q docs/diagnostics/0.5-closeout/check_evidence_contract.py
All writers operate on pytest's temporary projects; Analytics opens sources read-only.
"""
import hashlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'tools/orx-analytics/src'))
from test_adapters import bindir  # noqa: E402,F401
from test_verification_freshness import (  # noqa: E402,F401
    chain, pass_host_entries, resume, reviewing,
)
from orx import dispatch  # noqa: E402
from orx import verification_context  # noqa: E402
from orxanalytics import query_model, registry  # noqa: E402
from orxanalytics.importers.orxstate import import_orx_project  # noqa: E402
from orxanalytics.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_orx_registration(tmp_path, monkeypatch):
    monkeypatch.setenv('ORX_CONFIG_DIR', str(tmp_path/'orx-user-config'))
    monkeypatch.setenv('ORX_DATA_DIR', str(tmp_path/'orx-user-data'))
    monkeypatch.delenv('ORX_PROJECT', raising=False)


def snapshot_rows(project, directory, task):
    db = project.root / '.orx/state.db'
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with_store = Store(directory)
    try:
        registry.add_project(with_store, str(project.root))
        result = import_orx_project(with_store, str(project.root))
        assert not result.get('error'), result
        run = dispatch._active_context(project)[1]
        rev = project.store.revision_active(run.id)
        data = query_model.tasks_get(with_store, str(project.root), run.id, rev.id, task)
        assert data['found'] is True
        actual = [v['id'] for v in data['verifications'] if v['current'] is True]
        expected = [v.id for v in project.store.verifications_current(rev.id, task)]
        assert actual == expected
        assert len(data['verifications']) == len(project.store.verifications_for(rev.id, task))
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
        return actual, data['verifications']
    finally:
        with_store.close()


def test_unchanged_delivery_and_protocol_retry_preserve_current_rows(reviewing, tmp_path):
    project, bins = reviewing
    old, _ = snapshot_rows(project, tmp_path/'analytics-before', 'T001')
    resume(project, bins)
    new, history = snapshot_rows(project, tmp_path/'analytics-after', 'T001')
    assert new[:len(old)] == old
    assert len(new) == len(old) + 1


def test_artifact_change_retires_old_rows_without_deleting_history(reviewing, tmp_path):
    project, bins = reviewing
    old, _ = snapshot_rows(project, tmp_path/'analytics-before', 'T001')
    (project.root/'src/result.txt').write_text('changed\n')
    resume(project, bins)
    new, history = snapshot_rows(project, tmp_path/'analytics-after', 'T001')
    assert not set(old) & set(new)
    assert set(old) <= {v['id'] for v in history if v['current'] is False}


def test_dependency_change_retires_both_tasks_windows(chain, tmp_path):
    project, complete = chain
    complete('T002', 'consumer.txt')
    pass_host_entries(project)
    old1, _ = snapshot_rows(project, tmp_path/'analytics-a1', 'T001')
    old2, _ = snapshot_rows(project, tmp_path/'analytics-a2', 'T002')
    (project.root/'producer.txt').write_text('changed dependency\n')
    dispatch.verify_dispatch(project)
    pass_host_entries(project)
    new1, hist1 = snapshot_rows(project, tmp_path/'analytics-b1', 'T001')
    new2, hist2 = snapshot_rows(project, tmp_path/'analytics-b2', 'T002')
    assert not set(old1) & set(new1)
    assert not set(old2) & set(new2)


def test_task_detail_rejects_revision_from_another_run(reviewing, tmp_path):
    project, _ = reviewing
    analytics = Store(tmp_path/'analytics-identity')
    try:
        registry.add_project(analytics, str(project.root))
        import_orx_project(analytics, str(project.root))
        run = dispatch._active_context(project)[1]
        rev = project.store.revision_active(run.id)
        wrong = query_model.tasks_get(analytics, str(project.root), 'R999', rev.id, 'T001')
        assert wrong['found'] is False
        assert not wrong.get('verifications')
    finally:
        analytics.close()


def test_no_worker_context_keeps_unbound_rows_like_orx(chain, tmp_path):
    project, _ = chain
    run = dispatch._active_context(project)[1]
    rev = project.store.revision_active(run.id)
    task = project.store.task_get(rev.id, 'T003')
    verification_context.synchronize(project.store, project.root, rev.id, task)
    row = project.store.verification_add(rev.id, 'T003', 'command', 'legacy check', True)
    current, _ = snapshot_rows(project, tmp_path/'analytics-no-worker', 'T003')
    assert row.id in current
