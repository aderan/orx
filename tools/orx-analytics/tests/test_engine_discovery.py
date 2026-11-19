"""Periodic discovery feeds new indexed/scanned projects into engine queries."""
from orxanalytics import engine as eng, query_model
from orxanalytics.store import Store
from test_engine import FakeClock, home, seed_project
from test_discovery import write_index


def test_engine_discovers_new_index_project_without_manual_registration(home, tmp_path):
    index = write_index(tmp_path / 'projects-index.json', {})
    clock = FakeClock()
    engine = eng.Engine(home, clock=clock, index_file=index)
    engine.acquire()
    try:
        engine.tick()
        project = seed_project(tmp_path / 'new-project')
        write_index(index, {str(project): {}})
        # Discovery is low-frequency even when tick is called frequently.
        clock.advance(16)
        engine.tick()
        with_store = Store(home)
        assert with_store.conn.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
        with_store.close()
        clock.advance(eng.DISCOVERY_INTERVAL)
        report = engine.tick()
        assert report['discovery']['registered'][0]['action'] == 'registered'
        store = Store(home)
        try:
            assert str(project) in query_model._successful_snapshot_ids(store)
            assert store.conn.execute('SELECT count(*) FROM orx_runs').fetchone()[0] > 0
        finally:
            store.close()
    finally:
        engine.release()


def test_engine_discovery_scan_is_bounded_and_corrupt_index_is_isolated(home, tmp_path):
    index = tmp_path / 'broken-index.json'
    index.write_text('{broken')
    project = seed_project(tmp_path / 'scan-root/project')
    excluded = seed_project(tmp_path / 'scan-root/node_modules/ignored')
    engine = eng.Engine(home, index_file=index, scan_roots=[tmp_path / 'scan-root'])
    engine.acquire()
    try:
        report = engine.tick()
        assert report['discovery']['index']['status'] == 'unreadable'
        store = Store(home)
        try:
            keys = {r[0] for r in store.conn.execute('SELECT project_key FROM projects')}
            assert str(project) in keys
            assert str(excluded) not in keys
        finally:
            store.close()
    finally:
        engine.release()
