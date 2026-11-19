#!/usr/bin/env python3
"""Generate wire fixtures for the Swift layer from the REAL Python query
layer (apps/macos cross-language contract check).

Builds the same synthetic world as tests/test_query_model.py (no real
projects, no real agent sessions — CONTRIBUTING: 测试用合成来源), then
serializes `overview` / `runs.get` / `tasks.get` responses exactly as the
engine would over the socket. The Swift test
`PythonWireContractTests` decodes these files when
`ORX_WIRE_FIXTURES` points at the output directory, proving the Swift
payload models track the Python wire format.

Usage (from the repository root, inside the repo virtualenv):

    .venv/bin/python apps/macos/scripts/gen_wire_fixtures.py <out-dir>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))

from conftest import make_codex_rollout, make_orx_db, make_zcode_db  # noqa: E402
from test_query_model import (  # noqa: E402
    T,
    _min_orx_db,
    seed_extra_runs,
    seed_subtree_conflict,
    seed_usage_rows,
)
from orxanalytics import correlate, query_model, registry  # noqa: E402
from orxanalytics.importers import import_codex, import_zcode  # noqa: E402
from orxanalytics.importers.orxstate import import_orx_project  # noqa: E402
from orxanalytics.store import Store  # noqa: E402


def build_world(tmp: Path) -> dict:
    proj_a = tmp / "ProjA"
    proj_a.mkdir()
    proj_b = tmp / "ProjBORX"
    (proj_b / ".orx").mkdir(parents=True)
    zcode_db = tmp / "zcode-db.sqlite"
    make_zcode_db(zcode_db, str(proj_a), str(tmp / "ProjB"))
    (proj_a / ".orx").mkdir()
    make_orx_db(proj_a / ".orx" / "state.db")
    make_orx_db(proj_b / ".orx" / "state.db")
    codex_dir = tmp / "codex-sessions"
    (codex_dir / "2026" / "10" / "04").mkdir(parents=True)
    make_codex_rollout(codex_dir / "2026" / "10" / "04"
                       / "rollout-test.jsonl", str(proj_a))
    store = Store(tmp / "analytics-home")
    import_zcode(store, zcode_db)
    import_codex(store, codex_dir)
    registry.add_project(store, str(proj_a))
    registry.add_project(store, str(proj_b))
    import_orx_project(store, str(proj_a))
    import_orx_project(store, str(proj_b))
    correlate.correlate(store, str(proj_a))
    correlate.correlate(store, str(proj_b))
    seed_usage_rows(store, str(proj_a))
    seed_extra_runs(store, str(proj_a))
    seed_subtree_conflict(store, str(proj_a))
    return {"store": store, "proj_a": str(proj_a), "proj_b": str(proj_b)}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="orx-wire-fixtures-") as td:
        # resolve() up front: registry/project keys canonicalize symlinks
        # (/var -> /private/var), so the world must live at the resolved
        # path or snapshots and queries disagree (pytest's tmp_path is
        # already resolved, which is why the fixture never hits this).
        world = build_world(Path(td).resolve())
        store = world["store"]

        def dump(name: str, resp: dict) -> None:
            path = out / f"{name}.json"
            path.write_text(json.dumps(resp, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            print(f"wrote {path}")

        dump("overview", query_model.overview(store))
        # a run with retries / verifiers / planner / native preview
        run_resp = query_model.runs_get(store, world["proj_a"], "R001")
        dump("runs_get", run_resp)
        # a run with only base rows (cross-project identity)
        dump("runs_get_b",
             query_model.runs_get(store, world["proj_b"], "R001"))
        # history query (T010): consumption sort over ALL revisions with
        # project + status filters; page 2 of the same query proves the
        # offset echo and the stable identity secondary key
        dump("runs_list",
             query_model.runs_list(store, project=world["proj_a"],
                                   status="done", sort="observed_tokens",
                                   order="desc", limit=3))
        dump("runs_list_page2",
             query_model.runs_list(store, project=world["proj_a"],
                                   status="done", sort="observed_tokens",
                                   order="desc", limit=3, offset=1))
        # unfiltered consumption sort: includes unknown-consumption runs
        # (engine sorts them LAST) so the Swift side must decode nil
        # totals with their coverage blocks
        dump("runs_list_all",
             query_model.runs_list(store, sort="observed_tokens",
                                   order="desc", limit=10))
        # the retry/verifier task: pick the first failed task of the
        # first revision (ids are snapshot row ids, not revision numbers)
        target = next((t for t in run_resp["tasks"]
                       if t["status"]["raw"] == "failed"),
                      run_resp["tasks"][0])
        dump("tasks_get",
             query_model.tasks_get(store, world["proj_a"], "R001",
                                   target["revision_id"], target["task_id"]))
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
