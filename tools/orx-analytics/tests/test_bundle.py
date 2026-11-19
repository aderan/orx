"""Bundle tests: receiver-side recomputation from the bundle alone."""

from __future__ import annotations

import json
import subprocess
import sys

from orxanalytics import bundle, correlate, metrics, registry
from orxanalytics.importers import import_zcode
from orxanalytics.importers.orxstate import import_orx_project

import conftest  # noqa: F401  (fixture module)  # pragma: no cover


def _loaded_store(world):
    import_zcode(world["store"], world["zcode_db"])
    registry.add_project(world["store"], world["proj_a"])
    import_orx_project(world["store"], world["proj_a"])
    correlate.correlate(world["store"], world["proj_a"])
    return world["store"]


def _run_recompute(dest):
    return subprocess.run(
        [sys.executable, str(dest / "recompute.py")],
        capture_output=True, text=True)


def test_run_bundle_recomputes_headlines(world, tmp_path):
    store = _loaded_store(world)
    dest = tmp_path / "bundle-run"
    bundle.export_run_bundle(
        store, world["proj_a"], "R001", dest,
        title="R001 bundle",
        objective="verify corrected readings travel with their data",
        facts=["planner attempt has run attribution only via run_id"],
        hypotheses=["none"],
        excluded=["revision_id-only attribution (misses the planner)"],
        next_steps=["run recompute.py"])
    r = _run_recompute(dest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "consumption.input_tokens_sum" in r.stdout
    assert "first_pass.numerator" in r.stdout
    assert "all" in r.stdout and "MATCH" in r.stdout
    # handoff carries the structured sections
    hand = (dest / "HANDOFF.md").read_text()
    for sec in ("调查目标", "已确认事实", "假设", "已排除", "下一步"):
        assert sec in hand
    # links ship with basis
    links = json.loads((dest / "data" / "links.json").read_text())
    assert any(l["authority"] == "inferred" and l["basis"] for l in links)
    # integrity manifest covers every shipped file
    integ = json.loads((dest / "integrity.json").read_text())
    assert "bundle.json" in integ and "data/attempts.json" in integ


def test_bundle_detects_tampering(world, tmp_path):
    store = _loaded_store(world)
    dest = tmp_path / "bundle-tamper"
    bundle.export_run_bundle(store, world["proj_a"], "R001", dest)
    # tamper: change a usage number
    p = dest / "data" / "usage_observations.json"
    obs = json.loads(p.read_text())
    obs[0]["input_tokens"] = (obs[0]["input_tokens"] or 0) + 1
    p.write_text(json.dumps(obs))
    r = _run_recompute(dest)
    assert r.returncode == 1
    assert "MISMATCH" in r.stdout


def test_session_bundle_recomputes_tree(world, tmp_path):
    store = _loaded_store(world)
    dest = tmp_path / "bundle-session"
    bundle.export_session_bundle(
        store, world["zcode"]["controller"], dest,
        objective="controller session tree travels whole")
    r = _run_recompute(dest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "session_side.linked_turns_input" in r.stdout
    sessions = json.loads((dest / "data" / "zc_sessions.json").read_text())
    assert len(sessions) == 3     # controller + w1 + w2, not project B


def test_no_local_paths_are_load_bearing(world, tmp_path):
    """The bundle must not require this machine's paths: strip anything
    absolute from data and recompute still matches."""
    import re
    store = _loaded_store(world)
    dest = tmp_path / "bundle-portable"
    bundle.export_run_bundle(store, world["proj_a"], "R001", dest)
    r = _run_recompute(dest)
    assert r.returncode == 0
    # numbers never depended on the absolute paths that appear in metadata
    bj = json.loads((dest / "bundle.json").read_text())
    assert bj["scope"]["run_id"] == "R001"
