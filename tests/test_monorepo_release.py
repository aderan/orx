"""Monorepo release entry fails closed before building on an invalid source."""

import json
from pathlib import Path
import subprocess
import sys


def test_monorepo_candidate_requires_resolvable_commit(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "joint_release.py"
    result = subprocess.run(
        [sys.executable, str(script), "monorepo", "--commit", "missing-commit",
         "--work-dir", str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, result.stderr
    assert "commit" in json.loads(result.stderr)["error"]
    assert not (tmp_path / "candidate.json").exists()
