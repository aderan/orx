"""Storage remains bounded under repeated reads and full reconciliations."""
import os
import sqlite3

import pytest

from orxanalytics import engine as eng, registry, query_model
from orxanalytics.importers import orxstate
from orxanalytics.store import Store
from test_engine import FakeClock, home, proj, register


def test_idle_wal_reads_do_not_invalidate_signature(home, proj, monkeypatch):
    keeper = sqlite3.connect(proj / '.orx/state.db')
    keeper.execute('PRAGMA journal_mode=WAL')
    keeper.execute('CREATE TABLE read_mark_fixture(x)')
    keeper.commit()
    original = orxstate.open_ro

    def read_mark_open(path):
        conn = original(path)
        # SQLite/platforms differ in when mapped read marks reach mtime.
        # Model the observed macOS metadata effect after an actual ro open.
        shm = path.with_name(path.name + '-shm')
        st = shm.stat()
        os.utime(shm, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return conn

    monkeypatch.setattr(orxstate, 'open_ro', read_mark_open)
    register(home, proj)
    clock = FakeClock()
    e = eng.Engine(home, clock=clock, intervals={"ui": 15, "active": 15, "idle": 15})
    e.acquire()
    store = Store(home)
    actions = []
    try:
        for n in range(5):
            report = e.tick()
            assert report['wake'] is False and report['job'] is None
            actions.append(report['lanes']['state']['projects'][str(proj)]['action'])
            clock.advance(16)
        assert actions == ['imported'] + ['unchanged'] * 4
        before = e._source_signature(str(proj))
        conn = read_mark_open(proj / '.orx/state.db')
        conn.execute('SELECT count(*) FROM tasks').fetchone()
        conn.close()
        assert e._source_signature(str(proj)) == before
    finally:
        store.close()
        e.release()
        keeper.close()


def test_retention_prunes_all_mirrors_and_is_project_scoped(world):
    store = world['store']
    from conftest import make_orx_db
    from pathlib import Path
    other_db = Path(world['proj_b']) / '.orx/state.db'
    other_db.parent.mkdir(parents=True, exist_ok=True)
    make_orx_db(other_db)
    other = orxstate.import_orx_project(store, world['proj_b'])['snapshot_id']
    first = orxstate.import_orx_project(store, world['proj_a'])['snapshot_id']
    last = orxstate.import_orx_project(store, world['proj_a'])['snapshot_id']
    for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'orx_%'"):
        table = row['name']
        if 'snapshot_id' in [c['name'] for c in store.conn.execute(f'PRAGMA table_info({table})')]:
            assert store.conn.execute(f'SELECT count(*) FROM {table} WHERE snapshot_id=?', (first,)).fetchone()[0] == 0, table
    assert {r[0] for r in store.conn.execute('SELECT snapshot_id FROM orx_snapshots')} == {other, last}


def test_configurable_retention(world, monkeypatch):
    monkeypatch.setenv('ORX_ANALYTICS_SNAPSHOT_KEEP', '2')
    ids = [orxstate.import_orx_project(world['store'], world['proj_a'])['snapshot_id'] for _ in range(4)]
    assert [r[0] for r in world['store'].conn.execute('SELECT snapshot_id FROM orx_snapshots ORDER BY snapshot_id')] == ids[-2:]


def test_failed_import_rolls_back_partial_snapshot(world, monkeypatch):
    store = world['store']
    first = orxstate.import_orx_project(store, world['proj_a'])['snapshot_id']
    monkeypatch.setattr(orxstate, '_TABLES', orxstate._TABLES + [('orx_tasks', 'missing_table', ['id'])])
    result = orxstate.import_orx_project(store, world['proj_a'])
    assert 'error' in result
    assert [r[0] for r in store.conn.execute('SELECT snapshot_id FROM orx_snapshots')] == [first]
    assert query_model._successful_snapshot_ids(store)[world['proj_a']] == first


def test_import_logs_are_bounded_without_losing_last_success(world):
    store = world['store']
    sid = orxstate.import_orx_project(store, world['proj_a'])['snapshot_id']
    source = 'orx:' + world['proj_a']
    running = store.begin_import(source)
    for _ in range(105):
        run = store.begin_import(source)
        store.finish_import(run, {}, error='synthetic failure')
    assert store.conn.execute('SELECT count(*) FROM import_runs WHERE source_id=?', (source,)).fetchone()[0] <= 102
    assert store.conn.execute('SELECT status FROM import_runs WHERE id=?', (running,)).fetchone()[0] == 'running'
    assert query_model._successful_snapshot_ids(store)[world['proj_a']] == sid


def test_repeated_full_rechecks_reuse_pages(world):
    store = world['store']
    sizes = []
    for n in range(150):
        assert 'error' not in orxstate.import_orx_project(store, world['proj_a'])
        if n > 120:
            sizes.append(store.path.stat().st_size)
    assert max(sizes) - min(sizes) <= 8192
    assert store.conn.execute('SELECT count(*) FROM orx_snapshots').fetchone()[0] == 1


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_invalid_retention_does_not_write(world, monkeypatch, value):
    monkeypatch.setenv('ORX_ANALYTICS_SNAPSHOT_KEEP', value)
    with pytest.raises(ValueError, match='positive integer'):
        orxstate.import_orx_project(world['store'], world['proj_a'])
    assert world['store'].conn.execute('SELECT count(*) FROM import_runs').fetchone()[0] == 0


def test_pruning_failure_rolls_back_new_snapshot_and_deletions(world, monkeypatch):
    store = world['store']
    first = orxstate.import_orx_project(store, world['proj_a'])['snapshot_id']
    original = store.prune_snapshots

    def fail_after_pruning(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic pruning failure')

    monkeypatch.setattr(store, 'prune_snapshots', fail_after_pruning)
    assert 'error' in orxstate.import_orx_project(store, world['proj_a'])
    assert [r[0] for r in store.conn.execute('SELECT snapshot_id FROM orx_snapshots')] == [first]
    assert store.conn.execute('SELECT count(*) FROM orx_attempts WHERE snapshot_id=?', (first,)).fetchone()[0] == 5



def test_log_retention_preserves_receipts_for_configured_snapshots(world, monkeypatch):
    monkeypatch.setenv('ORX_ANALYTICS_SNAPSHOT_KEEP', '3')
    store = world['store']
    ids = [orxstate.import_orx_project(store, world['proj_a'])['snapshot_id'] for _ in range(3)]
    for _ in range(105):
        run = store.begin_import('orx:' + world['proj_a'])
        store.finish_import(run, {}, error='synthetic failure')
    store.prune_snapshots(world['proj_a'])
    assert [r[0] for r in store.conn.execute('SELECT snapshot_id FROM orx_snapshots ORDER BY snapshot_id')] == ids
