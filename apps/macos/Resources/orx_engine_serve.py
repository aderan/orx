"""Serve the ORX Analytics coordination engine for one data-dir.

Launched by the ORX Analytics app as::

    <python> orx_engine_serve.py <data-dir>

acquire the single-instance file lock, bind the user-private Unix
socket, then run the scheduler under bounded crash-restart supervision
(``EngineSupervisor``). All semantics — single instance arbitration,
user-private socket mode, tiered scheduling, restart backoff, the
diagnostics state file — come from the engine's own public API; this
wrapper only composes them.

Why a wrapper instead of ``python -m orxanalytics.engine``: that module
entry currently acquires the lock and runs the scheduler but never
binds the service socket (engine.main does not call ``start_server``),
so it cannot serve the app's socket client. The wrapper uses the same
public calls the engine's own tests use. When the module entry is
fixed, the app (and this file) can switch back to ``-m`` with no
behavior change.
"""
import sys
from pathlib import Path

# Bundle layout: this script sits in <app>/Contents/Resources next to
# the orxanalytics package; a dev checkout passes PYTHONPATH explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orxanalytics.engine import Engine, EngineSupervisor  # noqa: E402

if len(sys.argv) != 2:
    print("usage: orx_engine_serve.py <data-dir>", file=sys.stderr)
    raise SystemExit(2)

data_dir = Path(sys.argv[1])


def factory():
    engine = Engine(data_dir)
    engine.acquire()
    engine.start_server()
    return engine


report = EngineSupervisor(factory, state_dir=data_dir).run()
print(report)
raise SystemExit(1 if report.get("gave_up") else 0)
