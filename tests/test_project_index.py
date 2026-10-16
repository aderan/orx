"""User-level project index (G002/T004): ORX registers projects it
executes; orx-analytics consumes the index read-only for discovery.
Registration is best-effort — failures are recorded, never raised, and
never block routing."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from orx import dispatch, project_index
from conftest import HOST_CONFIG_TOML, ir_for, make_project, task_spec


def use_data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "orx-data"
    monkeypatch.setenv("ORX_DATA_DIR", str(d))
    return d


def make_proj_dir(tmp_path, name="proj") -> Path:
    root = tmp_path / name
    (root / ".orx").mkdir(parents=True)
    (root / ".orx" / "state.db").touch()
    return root


def read_index(d: Path) -> dict:
    return json.loads((d / "projects-index.json").read_text())


class TestRegisterProject:
    def test_creates_index_with_canonical_entry(self, tmp_path, monkeypatch):
        d = use_data_dir(tmp_path, monkeypatch)
        root = make_proj_dir(tmp_path)
        r = project_index.register_project(root)
        assert r["registered"] is True
        idx = read_index(d)
        assert idx["index_version"] == 1
        key = str(root.resolve())
        assert key in idx["projects"]
        e = idx["projects"][key]
        assert e["canonical_path"] == key
        assert e["has_state_db"] is True
        assert e["name"] == "proj"
        assert e["first_seen_at"] == e["last_seen_at"]

    def test_symlinked_root_keyed_canonical(self, tmp_path, monkeypatch):
        use_data_dir(tmp_path, monkeypatch)
        root = make_proj_dir(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(root)
        project_index.register_project(link)
        assert str(root.resolve()) in read_index(
            tmp_path / "orx-data")["projects"]

    def test_reregister_updates_last_seen_and_run_id(
            self, tmp_path, monkeypatch):
        use_data_dir(tmp_path, monkeypatch)
        root = make_proj_dir(tmp_path)
        project_index.register_project(root)
        project_index.register_project(root, run_id="R001")
        project_index.register_project(root, run_id="R002")
        e = read_index(tmp_path / "orx-data")["projects"][str(root.resolve())]
        assert e["last_run_id"] == "R002"
        assert e["last_seen_at"] >= e["first_seen_at"]

    def test_reregister_without_run_id_keeps_previous(
            self, tmp_path, monkeypatch):
        use_data_dir(tmp_path, monkeypatch)
        root = make_proj_dir(tmp_path)
        project_index.register_project(root, run_id="R001")
        project_index.register_project(root)
        e = read_index(tmp_path / "orx-data")["projects"][str(root.resolve())]
        assert e["last_run_id"] == "R001"

    def test_never_raises_on_unwritable_location(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        monkeypatch.setenv("ORX_DATA_DIR", str(blocker / "sub"))
        r = project_index.register_project(make_proj_dir(tmp_path))
        assert r["registered"] is False
        assert "error" in r and r["error"]

    def test_corrupt_index_refuses_overwrite(self, tmp_path, monkeypatch):
        d = use_data_dir(tmp_path, monkeypatch)
        d.mkdir(parents=True)
        p = d / "projects-index.json"
        p.write_text("{not json at all")
        r = project_index.register_project(make_proj_dir(tmp_path))
        assert r["registered"] is False
        assert "corrupt" in r["error"] or "shape" in r["error"]
        assert p.read_text() == "{not json at all"

    def test_wrong_shape_refuses_overwrite(self, tmp_path, monkeypatch):
        d = use_data_dir(tmp_path, monkeypatch)
        d.mkdir(parents=True)
        p = d / "projects-index.json"
        p.write_text(json.dumps({"index_version": 1, "projects": "nope"}))
        r = project_index.register_project(make_proj_dir(tmp_path))
        assert r["registered"] is False
        assert p.read_text() == json.dumps(
            {"index_version": 1, "projects": "nope"})

    def test_future_index_version_refused(self, tmp_path, monkeypatch):
        d = use_data_dir(tmp_path, monkeypatch)
        d.mkdir(parents=True)
        p = d / "projects-index.json"
        p.write_text(json.dumps(
            {"index_version": 99, "updated_at": "x", "projects": {}}))
        r = project_index.register_project(make_proj_dir(tmp_path))
        assert r["registered"] is False

    def test_concurrent_registrations_lose_nothing(
            self, tmp_path, monkeypatch):
        use_data_dir(tmp_path, monkeypatch)
        roots = [make_proj_dir(tmp_path, f"p{i}") for i in range(8)]
        results: list[dict] = []
        lock = threading.Lock()

        def go(root):
            r = project_index.register_project(root)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=go, args=(r,)) for r in roots]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r["registered"] for r in results), results
        idx = read_index(tmp_path / "orx-data")
        for root in roots:
            assert str(root.resolve()) in idx["projects"]


class TestRunSliceHook:
    def test_run_slice_registers_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        d = use_data_dir(tmp_path, monkeypatch)
        project = make_project(tmp_path, config_toml=HOST_CONFIG_TOML)
        try:
            goal = dispatch.create_goal(project, "g", ["a"])[0]
            dispatch.submit_plan(
                project, ir_for(goal, [task_spec("T001", acceptance=["a"])]))
            result = dispatch.run_slice(project)
            assert result["project_index"]["registered"] is True
            key = str(project.root.resolve())
            idx = read_index(d)
            assert key in idx["projects"]
            assert idx["projects"][key]["last_run_id"].startswith("R")
            assert idx["projects"][key]["has_state_db"] is True
        finally:
            project.close()
