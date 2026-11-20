"""Execute the published migration example only against temporary directories."""
from pathlib import Path
import fcntl
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEXT = (ROOT/'tools/orx-analytics/docs/app-installation.md').read_text()
EXAMPLE = TEXT.split('### 0.5.0：从旧默认数据目录迁移', 1)[1].split('```python\n',1)[1].split('```',1)[0]


def migrate(old, new):
    code = EXAMPLE.replace('old = Path.home() / ".orx-analytics"', f'old = Path({str(old)!r})')
    code = code.replace('new = Path.home() / ".local/share/orx/analytics"', f'new = Path({str(new)!r})')
    exec(compile(code, 'app-installation.md migration example', 'exec'), {})


def source(path):
    path.mkdir()
    conn = sqlite3.connect(path/'analytics.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE history(tokens)')
    conn.execute('INSERT INTO history VALUES (1100)')
    conn.commit()
    (path/'backups').mkdir()
    (path/'backups/previous.db').write_bytes(b'preserved backup')
    (path/'watch-demand.pid').write_text('12345')
    return conn


def test_example_includes_committed_wal_preserves_history_and_omits_runtime(tmp_path):
    old, new = tmp_path/'old', tmp_path/'new'
    conn = source(old)
    try:
        assert (old/'analytics.db-wal').stat().st_size > 0
        migrate(old, new)
        with sqlite3.connect(new/'analytics.db') as result:
            assert result.execute('SELECT tokens FROM history').fetchall() == [(1100,)]
            assert result.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert (new/'backups/previous.db').read_bytes() == b'preserved backup'
        assert not (new/'watch-demand.pid').exists()
        assert (old/'analytics.db').exists()
        assert len(list(tmp_path.glob('analytics-before-050-*.db'))) == 1
    finally:
        conn.close()


def test_example_refuses_to_merge_existing_target(tmp_path):
    old, new = tmp_path/'old', tmp_path/'new'
    conn = source(old); new.mkdir(); (new/'sentinel').write_text('keep')
    try:
        with pytest.raises(SystemExit, match='目标已存在'):
            migrate(old, new)
        assert (new/'sentinel').read_text() == 'keep'
        assert not (new/'analytics.db').exists()
    finally:
        conn.close()


def test_example_refuses_active_engine_lock(tmp_path):
    old, new = tmp_path/'old', tmp_path/'new'
    conn = source(old)
    try:
        with (old/'engine.lock').open('a+b') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(SystemExit, match='仍持锁'):
                migrate(old, new)
        assert not new.exists()
    finally:
        conn.close()


def test_example_refuses_nested_directories(tmp_path):
    old = tmp_path/'old'; conn = source(old)
    try:
        with pytest.raises(SystemExit, match='不能互相包含'):
            migrate(old, old/'nested')
    finally:
        conn.close()
