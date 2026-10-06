"""Release acceptance checks for the ORX distribution (G007 T001).

The 0.3.1 packaging gap this guards: the wheel shipped ``orx/skills`` but
not ``orx/agents``, so on a clean (non-editable) install
``presets.source_agents_dir()`` fell back to a repo layout that does not
exist inside site-packages and ``orx preset install zcode`` reported every
native role definition as "definition not packaged with this ORX (install
manually)". The fix is the hatch force-include of ``agents/``; these checks
prove it against the BUILT wheel, never against the source tree.

Sections (each a plain function, importable from the package acceptance
tests and runnable via ``python scripts/check_release.py``):

1. ``build_wheel``      — build the wheel from the current source tree into
                          a caller-chosen output directory;
2. ``install_wheel``    — create a throwaway venv and install the wheel
                          into it (a genuinely clean environment: no
                          editable install, no repo on sys.path);
3. ``run_probe``        — execute the in-package resource probe with the
                          venv interpreter from a scratch directory OUTSIDE
                          the repository, under an isolated environment
                          (fresh HOME, ORX_CONFIG_DIR / ORX_DATA_DIR /
                          ORX_ZCODE_AGENTS_DIR, quota preflight off, no
                          PYTHONPATH). The probe imports orx from the temp
                          site-packages and reports where every resource
                          resolves from;
4. ``check_package_resources`` / ``check_preset_install`` / judge the
   probe's report: the three native agent definitions (orx-worker,
   orx-verifier, orx-verifier-strong) and the default skills must ship in
   the wheel, resolve from the installed package, install through the
   packaged preset with no "install manually" note, byte-match the packaged
   copies — and an existing definition must stay preserved (the preset
   never overwrites a live role file; updates are explicit, see
   docs/upgrade-0.3.1.md).

T004 (G007) extends the chain to the full release story, all against the
BUILT wheel — never the source tree, never a source-code fallback:

5. ``run_cli_flow``     — drive the INSTALLED ``orx`` CLI (the venv console
                          script) in an isolated user layer (fresh HOME,
                          ORX_CONFIG_DIR / ORX_DATA_DIR / ORX_ZCODE_AGENTS_DIR,
                          quota preflight off, no PYTHONPATH): install the
                          packaged preset (fresh + preserve case), install
                          EVERY packaged skill, drift one and prove
                          ``skill update`` refreshes it from the package;
6. the stand-in Goal    — outside the repository, through the installed CLI
                          with ``--json`` envelopes and exit-code checks:
                          init -> goal -> plan submit -> run (parked host
                          work, so no paid model and no analytics) -> claim
                          -> heartbeat -> legacy evidence shape REJECTED by
                          name -> structured delivery result accepted ->
                          independent verdict via ``verify submit`` -> run
                          done; plus a click usage-error exit 2 probe;
7. the v0.3.0 fixture   — ``generate_v030_fixture`` builds a schema-v8
                          sample database with the REAL v0.3.0 code from
                          ``git archive v0.3.0`` (never a downgrade of the
                          current schema), and
                          ``verify_fixture_provenance`` re-runs that
                          generation and compares the normalized logical
                          dump against the committed fixture
                          (tests/fixtures/release-0.3.0);
8. ``check_upgrade_chain`` — on a COPY of the fixture (the source sample is
                          read-only; sha256 asserted unchanged): §6.1-style
                          ``VACUUM INTO`` backup, upgrade by opening the
                          copy with the installed new version, row-by-row
                          identity check of every pre-existing table and
                          column, additive defaults (new tables empty,
                          legacy attempts keep nonce NULL), the REAL
                          v0.3.0 read entry refusing the upgraded v11 file
                          (and leaving it byte-identical), and the backup
                          restored + read back by the real v0.3.0 code.

``main()`` runs all sections against a temporary directory, prints one JSON
report, and exits non-zero when any problem was found. There is no skip
path: every section runs on every invocation. ``--generate-fixture DEST``
builds the committed fixture (section 7) into DEST and exits.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

NATIVE_AGENTS = ("orx-worker", "orx-verifier", "orx-verifier-strong")
PACKAGED_PRESET = "zcode"
DEFAULT_SKILLS = ("orx-controller", "orx-agent")

# The committed v0.3.0 sample fixture (G007 T004): a schema-v8 database
# produced by the REAL v0.3.0 code from the git tag, never by downgrading
# the current schema. See generate_v030_fixture / docs/package-acceptance.md.
V030_TAG = "v0.3.0"
V030_COMMIT = "0f2629e6988ba5082a876257c910d78457bd1338"
FIXTURE_DIR = Path("tests") / "fixtures" / "release-0.3.0"
REPLAN_V9_TABLES = (
    "replan_mappings", "replan_task_mappings", "replan_sources",
    "replan_superseded", "replan_reports", "replan_artifact_sources",
)
V11_ADDITIVE_TABLES = frozenset(REPLAN_V9_TABLES) | {"attempt_progress"}
# v8 tables that must carry non-trivial data for the upgrade identity check
# to mean anything (a nearly-empty fixture would pass vacuously).
V8_CORE_TABLES = (
    "goals", "runs", "plan_revisions", "tasks", "task_dependencies",
    "task_events", "attempts", "evidence", "verifications",
    "usage_observations", "resource_status",
)

_RUN_TIMEOUT_SEC = 600

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


class ReleaseCheckError(RuntimeError):
    """A release acceptance section could not run or failed."""


def _run(command: list[str], *, cwd: Path, env: dict | None = None,
         timeout: int = _RUN_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, cwd=str(cwd), env=env, timeout=timeout,
        capture_output=True, text=True,
    )
    return result


# ---------------------------------------------------------------------------
# Section 1: build


def build_wheel(dest: Path | str, repo_root: Path = REPO_ROOT) -> Path:
    """Build the wheel from the CURRENT source tree into ``dest``.

    Returns the built wheel path. Building into a caller-owned directory
    keeps the repo's dist/ untouched (release artifacts are T005's call).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    result = _run(["uv", "build", "--wheel", "--out-dir", str(dest)],
                  cwd=repo_root)
    if result.returncode != 0:
        raise ReleaseCheckError(
            f"uv build failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    wheels = sorted(dest.glob("orx_agent-*.whl"))
    if not wheels:
        raise ReleaseCheckError(f"no wheel produced in {dest}")
    return wheels[-1]


# ---------------------------------------------------------------------------
# Section 2: clean temporary install


def install_wheel(wheel: Path, venv_dir: Path | str) -> Path:
    """Create a throwaway venv, install the wheel, return its python path.

    The venv is the clean environment: nothing editable, no repo path can
    shadow the installed package.
    """
    venv_dir = Path(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    created = _run(["uv", "venv", str(venv_dir)], cwd=venv_dir.parent)
    if created.returncode != 0:
        raise ReleaseCheckError(
            f"uv venv failed (exit {created.returncode})\n{created.stderr}"
        )
    python = venv_dir / "bin" / "python"
    if not python.exists():
        raise ReleaseCheckError(f"venv python missing at {python}")
    installed = _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=venv_dir.parent,
    )
    if installed.returncode != 0:
        raise ReleaseCheckError(
            f"uv pip install failed (exit {installed.returncode})\n"
            f"stdout:\n{installed.stdout}\nstderr:\n{installed.stderr}"
        )
    return python


# ---------------------------------------------------------------------------
# Section 3: the in-package resource probe
#
# Runs INSIDE the venv interpreter, from a scratch cwd outside the repo,
# under an isolated environment. It reports (never judges): where orx
# imports from, where the native agent definitions and packaged skills
# resolve from, and what installing the packaged preset does in a fresh
# user layer — plus the preserve case with a pre-existing custom role file.

PROBE_SOURCE = '''
import json
import os
import sys
from pathlib import Path

import orx
from orx import skills
from orx.presets import install_preset, source_agents_dir

NATIVE_AGENTS = ("orx-worker", "orx-verifier", "orx-verifier-strong")


def agent_rows(report):
    return {
        a["name"]: {
            "installed": a["installed"],
            "preserved_existing": a["preserved_existing"],
            "note": a.get("note"),
        }
        for a in report["agents"]
    }


def main():
    # Every path is resolve()d before comparison: on macOS the temp venv
    # lives under /var/folders whose canonical form is /private/var/..., and
    # a mixed resolved/unresolved pair would break startswith for no reason.
    orx_file = Path(orx.__file__).resolve()
    site = orx_file.parent.parent
    agents_src = Path(source_agents_dir()).resolve()
    skills_src = Path(skills.packaged_skills_dir()).resolve()
    out = {
        "probe_cwd": os.getcwd(),
        "python": sys.executable,
        "orx_file": str(orx_file),
        "orx_version": orx.__version__,
        "site_packages": str(site),
        "orx_in_site_packages": "site-packages" in str(orx_file),
        "agents_source": str(agents_src),
        "agents_in_site_packages": str(agents_src).startswith(str(site)),
        "agents_present": {
            name: (agents_src / f"{name}.md").is_file()
            for name in NATIVE_AGENTS
        },
        "skills_source": str(skills_src),
        "skills_in_site_packages": str(skills_src).startswith(str(site)),
        "skills_present": {
            name: (skills_src / name / "SKILL.md").is_file()
            for name in ("orx-controller", "orx-agent")
        },
    }

    base = Path(os.environ["ORX_PROBE_HOME"])

    # Fresh install: empty agents dir -> every role must come from the
    # installed package, byte-identical to the packaged copy.
    fresh_dir = base / "agents-fresh"
    fresh = install_preset("zcode", agents_dir=fresh_dir)
    out["preset_fresh"] = {
        "profiles_added": fresh["profiles_added"],
        "agents": agent_rows(fresh),
        "installed_contents_match_package": {
            name: (fresh_dir / f"{name}.md").read_bytes()
            == (agents_src / f"{name}.md").read_bytes()
            for name in NATIVE_AGENTS
            if (fresh_dir / f"{name}.md").is_file()
        },
    }

    # Preserve case: a live custom definition wins; the preset must keep it
    # untouched and still install the missing roles.
    preserve_dir = base / "agents-preserve"
    preserve_dir.mkdir(parents=True, exist_ok=True)
    custom = preserve_dir / "orx-worker.md"
    custom.write_text("custom live definition (do not overwrite)\\n")
    preserved = install_preset("zcode", agents_dir=preserve_dir)
    out["preset_preserve"] = {
        "agents": agent_rows(preserved),
        "custom_content_intact": custom.read_text() == "custom live definition (do not overwrite)\\n",
    }

    print(json.dumps(out))


main()
'''


def run_probe(venv_python: Path, workdir: Path | str) -> dict:
    """Run the in-package resource probe; return its JSON report.

    The scratch ``workdir`` must live outside the repository (the caller
    passes a temporary directory): the probe's cwd being non-repo is part
    of what makes the import-location assertions meaningful.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    base = workdir / "home"
    (base / "config").mkdir(parents=True, exist_ok=True)
    # Deliberately minimal environment: no PYTHONPATH, no inherited
    # ORX_* state — only what the probe needs plus the isolated ORX
    # scratch layer. Quota preflight is off: no network from checks.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(base),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "ORX_CONFIG_DIR": str(base / "config"),
        "ORX_DATA_DIR": str(base / "data"),
        "ORX_ZCODE_AGENTS_DIR": str(base / "agents-fresh"),
        "ORX_QUOTA_PREFLIGHT": "0",
        "ORX_PROBE_HOME": str(base),
    }
    script = workdir / "probe.py"
    script.write_text(PROBE_SOURCE)
    result = _run([str(venv_python), str(script)], cwd=workdir, env=env)
    if result.returncode != 0:
        raise ReleaseCheckError(
            f"probe failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseCheckError(
            f"probe output is not JSON ({exc}): {result.stdout[:2000]}"
        ) from exc


# ---------------------------------------------------------------------------
# Section 4: judging the probe report


def check_package_resources(report: dict) -> list[str]:
    """Problems with the in-package resources of the installed wheel."""
    problems: list[str] = []
    if not report.get("orx_in_site_packages"):
        problems.append(
            f"orx imports from {report.get('orx_file')!r}, not the installed"
            " site-packages (an editable/repo copy is shadowing the wheel)"
        )
    if not report.get("agents_in_site_packages"):
        problems.append(
            "native agent definitions resolve from"
            f" {report.get('agents_source')!r}, not the installed package"
            " (agents/ missing from the wheel?)"
        )
    for name, present in report.get("agents_present", {}).items():
        if not present:
            problems.append(f"native agent definition {name} not packaged")
    if not report.get("skills_in_site_packages"):
        problems.append(
            "packaged skills resolve from"
            f" {report.get('skills_source')!r}, not the installed package"
        )
    for name, present in report.get("skills_present", {}).items():
        if not present:
            problems.append(f"packaged skill {name}/SKILL.md not in the wheel")
    return problems


def check_preset_install(report: dict) -> list[str]:
    """Problems with installing the packaged preset from the clean wheel.

    Covers both the fresh install (every role installed from the package,
    byte-identical, no 'install manually' note) and the preserve case (a
    live custom definition stays untouched; the missing roles still
    install).
    """
    problems: list[str] = []
    fresh = report.get("preset_fresh", {})
    for name, row in fresh.get("agents", {}).items():
        if row.get("note"):
            problems.append(f"preset fresh install: {name}: {row['note']}")
        if not row.get("installed"):
            problems.append(
                f"preset fresh install did not install {name}"
                f" (preserved_existing={row.get('preserved_existing')})"
            )
    for name, matches in fresh.get("installed_contents_match_package", {}).items():
        if not matches:
            problems.append(
                f"installed {name}.md differs from the packaged copy"
            )
    preserve = report.get("preset_preserve", {})
    worker = preserve.get("agents", {}).get("orx-worker", {})
    if not worker.get("preserved_existing"):
        problems.append(
            "preset overwrote an existing orx-worker definition"
            " (preserve semantics changed)"
        )
    if not preserve.get("custom_content_intact"):
        problems.append("preset modified a live custom definition's content")
    for name in ("orx-verifier", "orx-verifier-strong"):
        if not preserve.get("agents", {}).get(name, {}).get("installed"):
            problems.append(
                f"preset preserve case did not install missing role {name}"
            )
    return problems


# ---------------------------------------------------------------------------
# Shared plumbing for the CLI-driven sections (T004): every command runs the
# INSTALLED console script from the throwaway venv, from a scratch directory,
# under an environment that cannot see the developer's user layer and cannot
# reach the network (quota preflight off; no quota/watch/analytics command is
# ever invoked).


def isolated_env(home: Path) -> dict:
    """A minimal environment isolating every user-level ORX directory.

    Fresh HOME (config/data/agents/skills layers all live under it), no
    PYTHONPATH, no inherited ORX_* state, quota preflight disabled.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "ORX_CONFIG_DIR": str(home / "config"),
        "ORX_DATA_DIR": str(home / "data"),
        "ORX_ZCODE_AGENTS_DIR": str(home / "zcode-agents"),
        "ORX_QUOTA_PREFLIGHT": "0",
    }


def _venv_python_code(venv_python: Path, code: str, cwd: Path, env: dict) -> str:
    """Run a snippet under the venv interpreter (orx = the installed wheel)."""
    result = _run([str(venv_python), "-c", code], cwd=cwd, env=env)
    if result.returncode != 0:
        raise ReleaseCheckError(
            f"venv python snippet failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


STANDIN_OBJECTIVE = (
    "Package acceptance stand-in Goal: prove the installed wheel runs the "
    "full local loop (plan, execute, structured delivery, independent verdict)"
)
STANDIN_ACCEPTANCE = (
    "the stand-in artifact exists",
    "the structured delivery result is accepted",
)
STANDIN_AGENT_ENTRY = "agent: review the stand-in artifact"


def _standin_ir(goal_id: str) -> dict:
    return {
        "goal": goal_id,
        "exploration": {
            "summary": "stand-in exploration for package acceptance",
            "relevant_components": ["src/"],
            "unknowns": [],
            "assumptions": [],
            "risks": [],
        },
        "approach": {"summary": "two-task stand-in plan", "decisions": []},
        "tasks": [
            {
                "id": "T001",
                "objective": "deliver the stand-in artifact with the structured evidence",
                "dependencies": [],
                "scope": {"allowed": ["src/"]},
                "acceptance": [STANDIN_ACCEPTANCE[0]],
                "verification": ["true", STANDIN_AGENT_ENTRY],
                "routing": {"complexity": "medium", "required_capabilities": ["coding"]},
                "preread": [],
            },
            {
                "id": "T002",
                "objective": "close the stand-in loop with a command-verified delivery",
                "dependencies": ["T001"],
                "scope": {"allowed": ["src/"]},
                "acceptance": [STANDIN_ACCEPTANCE[1]],
                "verification": ["true"],
                "routing": {"complexity": "medium", "required_capabilities": ["coding"]},
                "preread": [],
            },
        ],
    }


def _standin_evidence(artifact: str, log: str) -> str:
    """The CURRENT structured delivery result (the 0.3.1 shape). The command
    entries are genuinely runnable: the delivery gate re-runs them fresh."""
    return json.dumps({
        "status": "passed",
        "summary": "stand-in delivery for the package acceptance chain",
        "checks": [
            {"command": f"test -f {artifact}", "exit_code": 0, "log": log}
        ],
        "artifacts": [artifact],
    })


def run_cli_flow(venv_python: Path, workdir: Path | str) -> dict:
    """Sections 5 and 6: the installed CLI against an isolated user layer.

    Preset install (fresh + preserve), every packaged skill installed,
    drifted and refreshed, and the stand-in Goal loop — every step through
    ``<venv>/bin/orx`` with ``--json`` envelopes, exit codes asserted, from
    directories outside the repository. Host work is parked (never
    dispatched to a model); no quota/network command runs.
    """
    workdir = Path(workdir)
    orx_bin = venv_python.parent / "orx"
    if not orx_bin.exists():
        raise ReleaseCheckError(
            f"console script missing in the wheel install: {orx_bin}"
        )
    home = workdir / "home"
    env = isolated_env(home)
    # A pre-existing ~/.zcode/skills so the install's symlink leg is real.
    (home / ".zcode" / "skills").mkdir(parents=True, exist_ok=True)
    agents_dir = home / "zcode-agents"
    report: dict = {"steps": [], "problems": [], "orx_bin": str(orx_bin)}

    def cli(label: str, args: list[str], cwd: Path, expect: int = 0) -> dict:
        proc = _run([str(orx_bin), *args], cwd=cwd, env=env)
        step: dict = {
            "step": label,
            "command": "orx " + " ".join(args),
            "exit_code": proc.returncode,
        }
        if "--json" in args:
            try:
                step["envelope"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                step["envelope"] = None
                step["stdout"] = proc.stdout[-2000:]
        else:
            step["stdout"] = proc.stdout[-2000:]
        if proc.returncode != 0:
            step["stderr"] = proc.stderr[-2000:]
        report["steps"].append(step)
        if proc.returncode != expect:
            report["problems"].append(
                f"cli[{label}]: exit {proc.returncode}, expected {expect}"
                f" ({step['command']}) stderr: {proc.stderr[-500:]}"
            )
        return step

    def envelope_of(step: dict) -> dict:
        env_ = step.get("envelope")
        return env_ if isinstance(env_, dict) else {}

    # -- version of the installed package, reported by the installed CLI ----
    version_step = cli("version", ["version", "--json"], cwd=workdir)
    report["orx_version"] = envelope_of(version_step).get("version")

    # -- every packaged skill, enumerated from the installed package -------
    listed = _venv_python_code(
        venv_python,
        "import json; from orx import skills;"
        " print(json.dumps(skills.available_skills()))",
        cwd=workdir, env=env,
    )
    skill_names = json.loads(listed)
    report["packaged_skills"] = skill_names
    if not skill_names:
        report["problems"].append("no packaged skills found in the wheel")

    # -- preset through the installed CLI: fresh install, then preserve ----
    fresh = envelope_of(cli("preset-fresh", ["preset", "install", "zcode", "--json"],
                            cwd=workdir))
    report["preset_fresh"] = fresh
    for row in fresh.get("agents", []):
        if not row.get("installed") or row.get("note"):
            report["problems"].append(
                f"cli preset fresh install: {row.get('name')}: installed="
                f"{row.get('installed')} note={row.get('note')!r}"
            )
    custom = agents_dir / "orx-worker.md"
    custom.write_text("custom live definition (do not overwrite)\n")
    preserved = envelope_of(cli("preset-preserve",
                                ["preset", "install", "zcode", "--json"],
                                cwd=workdir))
    report["preset_preserve"] = preserved
    rows = {row.get("name"): row for row in preserved.get("agents", [])}
    worker = rows.get("orx-worker", {})
    if not worker.get("preserved_existing"):
        report["problems"].append(
            "cli preset install overwrote a live role definition"
        )
    if custom.read_text() != "custom live definition (do not overwrite)\n":
        report["problems"].append("cli preset install modified a live definition")

    # -- skills: install all, drift one, refresh from the package ----------
    installed = envelope_of(cli("skill-install",
                                ["skill", "install", *skill_names, "--json"],
                                cwd=workdir))
    canonical = Path(installed.get("canonical_root", ""))
    report["skills"] = {
        "installed": installed.get("installed", []),
        "canonical_root": installed.get("canonical_root"),
        "symlinked_into": installed.get("symlinked_into", []),
    }
    if sorted(installed.get("installed", [])) != sorted(skill_names):
        report["problems"].append(
            f"cli skill install installed {installed.get('installed')},"
            f" expected all packaged skills {skill_names}"
        )
    if not str(canonical).startswith(str(home)):
        report["problems"].append(
            f"skills canonical root {canonical} is not under the isolated HOME"
        )
    links = installed.get("symlinked_into", [])
    if not links or not all(str(Path(p)).startswith(str(home)) for p in links):
        report["problems"].append(
            f"skill symlinks point outside the isolated HOME: {links}"
        )
    drift = canonical / "orx-controller" / "SKILL.md"
    if not drift.is_file():
        report["problems"].append(f"installed skill file missing: {drift}")
    else:
        drift.write_text("drifted copy (must be refreshed from the package)\n")
        refreshed = envelope_of(cli("skill-update", ["skill", "update", "--json"],
                                    cwd=workdir))
        packaged_root = Path(_venv_python_code(
            venv_python,
            "from orx import skills; print(skills.packaged_skills_dir())",
            cwd=workdir, env=env,
        ).strip())
        matches = drift.read_bytes() == (packaged_root / "orx-controller" / "SKILL.md").read_bytes()
        report["skills"]["refreshed"] = refreshed.get("refreshed", [])
        report["skills"]["refresh_matches_package"] = matches
        if "orx-controller" not in refreshed.get("refreshed", []):
            report["problems"].append(
                "cli skill update did not refresh the drifted orx-controller"
            )
        if not matches:
            report["problems"].append(
                "refreshed skill content differs from the packaged copy"
            )

    # -- the stand-in Goal loop, outside the repository, through the CLI ---
    proj = workdir / "standin-project"
    proj.mkdir(parents=True, exist_ok=True)
    standin: dict = {"project": str(proj)}
    report["standin"] = standin

    cli("init", ["init", ".", "--json"], cwd=proj)
    goal = envelope_of(cli(
        "goal-new",
        ["goal", "new", "--objective", STANDIN_OBJECTIVE,
         "--acceptance", STANDIN_ACCEPTANCE[0],
         "--acceptance", STANDIN_ACCEPTANCE[1],
         "--constraint", "no paid model, no analytics: host work stays parked",
         "--json"],
        cwd=proj,
    ))
    standin["goal_id"] = goal.get("goal", {}).get("id")
    standin["run_id"] = goal.get("run", {}).get("id")
    if not standin["goal_id"]:
        report["problems"].append("goal new returned no goal id")

    (proj / "plan-ir.json").write_text(json.dumps(_standin_ir(standin["goal_id"])))
    plan = envelope_of(cli("plan-submit",
                           ["plan", "submit", "--file", "plan-ir.json", "--json"],
                           cwd=proj))
    standin["revision"] = plan.get("revision")
    standin["plan_tasks"] = plan.get("tasks")

    def task_status(step_envelope: dict, task_id: str) -> str | None:
        for row in step_envelope.get("tasks", []):
            if row.get("id") == task_id:
                return row.get("status")
        return None

    status_plan = envelope_of(cli("status-after-plan", ["status", "--json"], cwd=proj))
    standin["single_goal_after_plan"] = status_plan.get("goal", {}).get("id") == standin["goal_id"]
    standin["tasks_after_plan"] = {
        row.get("id"): row.get("status") for row in status_plan.get("tasks", [])
    }
    if not standin["single_goal_after_plan"]:
        report["problems"].append("status after plan shows a different/absent active Goal")

    parked = envelope_of(cli("run-park-t001", ["run", "--json"], cwd=proj))
    standin["t001_parked"] = [e.get("task") for e in parked.get("host_required", [])]
    if "T001" not in standin["t001_parked"]:
        report["problems"].append(
            "run did not park T001 as host work (a paid model would be needed)"
        )

    claimed = envelope_of(cli("claim-t001",
                             ["task", "claim", "T001",
                              "--session", "sess-standin-worker", "--json"],
                             cwd=proj))
    standin["attempt_t001"] = claimed.get("attempt")
    beat = envelope_of(cli(
        "heartbeat-t001",
        ["task", "heartbeat", "T001", "--attempt", str(standin["attempt_t001"]),
         "--phase", "implementing", "--message", "stand-in round", "--json"],
        cwd=proj,
    ))
    standin["heartbeat_sequence"] = beat.get("sequence")

    # The legacy {summary, commands, artifacts} shape is REJECTED by name.
    (proj / "legacy-evidence.json").write_text(json.dumps({
        "summary": "legacy 0.3.0-era shape",
        "commands": [{"command": "true", "exit_code": 0, "log": "x.log"}],
        "artifacts": [],
    }))
    legacy = cli("legacy-evidence-rejected",
                 ["task", "complete", "T001", "--evidence", "legacy-evidence.json",
                  "--attempt", str(standin["attempt_t001"]), "--json"],
                 cwd=proj, expect=1)
    legacy_env = envelope_of(legacy) or {}
    standin["legacy_rejection"] = {
        "exit_code": legacy["exit_code"],
        "ok": legacy_env.get("ok"),
        "error": legacy_env.get("error") or legacy.get("stderr", ""),
    }
    if legacy["exit_code"] == 0:
        report["problems"].append(
            "legacy evidence shape was ACCEPTED (the by-name rejection is gone)"
        )
    else:
        error = standin["legacy_rejection"]["error"]
        for token in ("evidence rejected", "status", "checks"):
            if token not in error:
                report["problems"].append(
                    f"legacy evidence rejection does not name {token!r}: {error[:300]}"
                )
    status_rej = envelope_of(cli("status-after-rejection", ["status", "--json"], cwd=proj))
    standin["t001_status_after_rejection"] = task_status(status_rej, "T001")
    if standin["t001_status_after_rejection"] != "running":
        report["problems"].append(
            "the rejected delivery changed T001's status"
            f" ({standin['t001_status_after_rejection']!r}, expected 'running')"
        )

    artifact = "stand-in-artifact.txt"
    (proj / artifact).write_text("stand-in artifact\n")
    log_dir = proj / ".orx" / "runs" / "stand-in" / "check" / "T001"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "01-command.log").write_text("test -f stand-in-artifact.txt\n")
    (proj / "evidence.json").write_text(_standin_evidence(
        artifact, ".orx/runs/stand-in/check/T001/01-command.log"))
    completed = envelope_of(cli(
        "complete-t001",
        ["task", "complete", "T001", "--evidence", "evidence.json",
         "--attempt", str(standin["attempt_t001"]), "--json"],
        cwd=proj,
    ))
    standin["t001_after_complete"] = completed.get("status")

    verify = envelope_of(cli("verify-report", ["verify", "--json"], cwd=proj))
    agent_entries = [e.get("entry") for e in verify.get("agent_required", [])]
    standin["agent_entries"] = agent_entries
    if STANDIN_AGENT_ENTRY not in agent_entries:
        report["problems"].append(
            f"verify did not report the agent entry {STANDIN_AGENT_ENTRY!r}: {agent_entries}"
        )
    verdict = envelope_of(cli(
        "verify-submit",
        ["verify", "submit", "T001", "--result", "pass",
         "--entry", STANDIN_AGENT_ENTRY, "--json"],
        cwd=proj,
    ))
    standin["t001_verdict_status"] = verdict.get("status")

    parked2 = envelope_of(cli("run-park-t002", ["run", "--json"], cwd=proj))
    standin["t002_parked"] = [e.get("task") for e in parked2.get("host_required", [])]
    claimed2 = envelope_of(cli("claim-t002",
                              ["task", "claim", "T002",
                               "--session", "sess-standin-worker-2", "--json"],
                              cwd=proj))
    standin["attempt_t002"] = claimed2.get("attempt")
    cli("heartbeat-t002",
        ["task", "heartbeat", "T002", "--attempt", str(standin["attempt_t002"]),
         "--phase", "checking", "--message", "closing the loop", "--json"],
        cwd=proj)
    artifact2 = "stand-in-artifact-2.txt"
    (proj / artifact2).write_text("stand-in artifact 2\n")
    log2 = proj / ".orx" / "runs" / "stand-in" / "check" / "T002"
    log2.mkdir(parents=True, exist_ok=True)
    (log2 / "01-command.log").write_text("test -f stand-in-artifact-2.txt\n")
    (proj / "evidence-2.json").write_text(_standin_evidence(
        artifact2, ".orx/runs/stand-in/check/T002/01-command.log"))
    completed2 = envelope_of(cli(
        "complete-t002",
        ["task", "complete", "T002", "--evidence", "evidence-2.json",
         "--attempt", str(standin["attempt_t002"]), "--json"],
        cwd=proj,
    ))
    standin["t002_after_complete"] = completed2.get("status")

    final = envelope_of(cli("status-final", ["status", "--json"], cwd=proj))
    standin["final_run_status"] = final.get("run", {}).get("status")
    standin["final_task_statuses"] = {
        row.get("id"): row.get("status") for row in final.get("tasks", [])
    }
    standin["final_single_goal"] = final.get("goal", {}).get("id") == standin["goal_id"]
    if standin["final_run_status"] != "done":
        report["problems"].append(
            f"stand-in run finished as {standin['final_run_status']!r}, expected 'done'"
        )
    if standin["final_task_statuses"] != {"T001": "passed", "T002": "passed"}:
        report["problems"].append(
            f"stand-in final task statuses: {standin['final_task_statuses']}"
        )

    # A click usage error exits 2 without touching anything.
    usage = cli("usage-error-exit-2", ["status", "bogus-argument"], cwd=proj, expect=2)
    standin["usage_error_exit_code"] = usage["exit_code"]
    return report


# ---------------------------------------------------------------------------
# Section 7: the real v0.3.0 sample fixture. The v0.3.0 SOURCE comes from
# `git archive v0.3.0` (never a hand-downgraded current schema); the code
# runs on the wheel venv's interpreter, with the archive's orx package
# shadowing the installed one on sys.path[0] (typer/pydantic come from the
# wheel's own dependency set, satisfying v0.3.0's declared ranges).


V030_BOOTSTRAP = '''\
import sys
sys.path.insert(0, sys.argv[1])
import orx
if orx.__version__ != "0.3.0":
    raise SystemExit(f"expected orx 0.3.0, got {orx.__version__} at {orx.__file__}")
from orx.cli import app
sys.argv = ["orx"] + sys.argv[2:]
app()
'''

V030_GENERATOR = '''\
"""Build the schema-v8 sample database with the REAL v0.3.0 code.

Usage: python gen.py <v0.3.0-archive-src> <project-root>
The archive's orx package is forced onto sys.path[0], so every write below
goes through v0.3.0's own Store/dispatch; the current code never touches
the file. Prints a JSON report on stdout.
"""
import json
import sqlite3
import sys
from pathlib import Path

src = Path(sys.argv[1])
proj = Path(sys.argv[2])
sys.path.insert(0, str(src))

import orx
if orx.__version__ != "0.3.0":
    raise SystemExit(f"expected orx 0.3.0, got {orx.__version__} at {orx.__file__}")
from orx import dispatch

report = {"orx_version": orx.__version__, "orx_file": orx.__file__}

dispatch.init_project(proj)
project = dispatch.open_project()
goal, run = dispatch.create_goal(
    project,
    "0.3.0 sample: schema v8 library for the 0.3.1 package acceptance upgrade",
    ["the sample marker exists", "the sample summary is written"],
    ["no network during generation"],
    "Generated by the real v0.3.0 code from git archive v0.3.0; consumed by"
    " the 0.3.1 package acceptance chain (scripts/check_release.py).",
)

ir = {
    "goal": goal.id,
    "exploration": {"summary": "sample exploration", "relevant_components": ["src/"],
                    "unknowns": [], "assumptions": [], "risks": []},
    "approach": {"summary": "two-task sample plan", "decisions": []},
    "tasks": [
        {"id": "T001", "objective": "produce the sample marker",
         "dependencies": [], "scope": {"allowed": ["src/"]},
         "acceptance": [goal.acceptance[0]],
         "verification": ["true", "agent: review the sample marker"],
         "routing": {"complexity": "medium", "required_capabilities": ["coding"]},
         "preread": []},
        {"id": "T002", "objective": "write the sample summary",
         "dependencies": ["T001"], "scope": {"allowed": ["src/"]},
         "acceptance": [goal.acceptance[1]],
         "verification": ["true"],
         "routing": {"complexity": "medium", "required_capabilities": ["coding"]},
         "preread": []},
    ],
}
dispatch.submit_plan(project, ir)
dispatch.run_slice(project)
claimed = dispatch.task_claim(project, "T001", session="sess-030-worker")
attempt = project.store.attempt_get(claimed["attempt"])
project.store.usage_add(
    attempt.id, attempt.profile, run.id, "T001",
    1111, 222, 33, "native_cli", "exact",
)
evidence = proj / "evidence-030.json"
evidence.write_text(json.dumps({
    "summary": "v0.3.0-era legacy delivery shape",
    "commands": [{"command": "true", "exit_code": 0, "log": "evidence-030.log"}],
    "artifacts": ["sample-marker.txt"],
}))
dispatch.task_complete(project, "T001", str(evidence), attempt_id=attempt.id)
dispatch.verify_dispatch(project)
dispatch.verify_submit(
    project, "T001", "pass", "agent: review the sample marker", None,
)
dispatch.run_slice(project)
# T002 is claimed and left RUNNING: an in-flight task at upgrade time is the
# realistic 0.3.0 -> 0.3.1 scenario the upgrade doc describes.
dispatch.task_claim(project, "T002", session="sess-030-worker-2")
dispatch.resource_set(project, "orx-host", "available",
                      "sample resource note from the v0.3.0 generator")
project.close()

db = proj / ".orx" / "state.db"
conn = sqlite3.connect(db)
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
version = conn.execute(
    "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
counts = {
    name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'")
}
conn.close()
report["schema_version"] = int(version)
report["tables"] = counts
print(json.dumps(report))
'''


def v030_commit(repo_root: Path = REPO_ROOT) -> str:
    result = _run(["git", "rev-parse", V030_TAG + "^{commit}"], cwd=repo_root)
    if result.returncode != 0:
        raise ReleaseCheckError(
            f"git rev-parse {V030_TAG} failed (exit {result.returncode}):"
            f" {result.stderr}"
        )
    return result.stdout.strip()


def extract_v030_source(repo_root: Path, dest: Path) -> Path:
    """`git archive v0.3.0` extracted into dest; returns its src/ directory."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", V030_TAG],
        cwd=str(repo_root), capture_output=True,
    )
    if archive.returncode != 0:
        raise ReleaseCheckError(
            f"git archive {V030_TAG} failed (exit {archive.returncode}):"
            f" {archive.stderr.decode(errors='replace')}"
        )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    src = dest / "src"
    if not (src / "orx" / "__init__.py").is_file():
        raise ReleaseCheckError(f"v0.3.0 archive layout unexpected under {dest}")
    return src


def run_v030_cli(venv_python: Path, v030_src: Path, args: list[str], *,
                 cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run the REAL v0.3.0 CLI entry (its typer app) from the git-archive
    source on the wheel venv's interpreter."""
    return _run(
        [str(venv_python), "-c", V030_BOOTSTRAP, str(v030_src), *args],
        cwd=cwd, env=env,
    )


def generate_v030_fixture(dest: Path | str, repo_root: Path, venv_python: Path,
                          workdir: Path) -> dict:
    """Generate the schema-v8 sample fixture with the real v0.3.0 code.

    Writes state.db + config.toml + profiles.toml (a complete v0.3.0
    project) plus PROVENANCE.md into ``dest``. Returns the generator's
    report (versions, table counts, digests).
    """
    dest = Path(dest)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    src = extract_v030_source(repo_root, workdir / "v030-src")
    genproj = workdir / "genproj"
    genproj.mkdir(parents=True, exist_ok=True)
    script = workdir / "generate_v030.py"
    script.write_text(V030_GENERATOR)
    env = isolated_env(workdir / "home")
    result = _run([str(venv_python), str(script), str(src), str(genproj)],
                  cwd=genproj, env=env)
    if result.returncode != 0:
        raise ReleaseCheckError(
            f"v0.3.0 fixture generation failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    gen_report = json.loads(result.stdout)
    if gen_report.get("schema_version") != 8:
        raise ReleaseCheckError(
            f"v0.3.0 generator produced schema version"
            f" {gen_report.get('schema_version')!r}, expected 8"
        )
    state = genproj / ".orx" / "state.db"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(state, dest / "state.db")
    for name in ("config.toml", "profiles.toml"):
        shutil.copyfile(genproj / ".orx" / name, dest / name)
    digest = sha256_file(dest / "state.db")
    import datetime as _dt
    provenance = (
        f"# v0.3.0 sample fixture (schema v8)\n\n"
        f"Generated {_dt.datetime.now(_dt.timezone.utc).isoformat()} by:\n\n"
        f"    uv run python scripts/check_release.py"
        f" --generate-fixture {dest}\n\n"
        f"- Source: `git archive {V030_TAG}` of this repository — commit"
        f" {v030_commit(repo_root)}.\n"
        f"- The database file is written exclusively by the REAL v0.3.0 code"
        f" from that archive (its own `Store`/`dispatch`); the generator"
        f" asserts `orx.__version__ == \"0.3.0\"`. It is never a downgrade"
        f" of the current schema.\n"
        f"- Generator orx version: {gen_report['orx_version']}"
        f" (from {gen_report['orx_file']}).\n"
        f"- `meta.schema_version` at generation: 8.\n"
        f"- Table row counts: {json.dumps(gen_report['tables'], sort_keys=True)}.\n"
        f"- state.db sha256: `{digest}`.\n"
        f"- config.toml sha256: `{sha256_file(dest / 'config.toml')}`;"
        f" profiles.toml sha256: `{sha256_file(dest / 'profiles.toml')}`.\n"
        f"- In-flight state: T001 passed with legacy evidence + verdict;"
        f" T002 claimed and RUNNING — the realistic mid-flight upgrade\n"
        f"  scenario of docs/upgrade-0.3.1.md section 6.\n"
        f"- Consumers must treat this directory as READ-ONLY: upgrade and"
        f" restore checks copy `state.db` first and assert the source"
        f" digest is unchanged afterwards.\n"
    )
    (dest / "PROVENANCE.md").write_text(provenance)
    gen_report.update(
        fixture=str(dest), sha256=digest,
        v030_commit=v030_commit(repo_root),
        provenance=str(dest / "PROVENANCE.md"),
    )
    return gen_report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"):
        tables[name] = [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
    return tables


def _connect_immutable(db: Path) -> sqlite3.Connection:
    """Read-only, sidecar-free open. A WAL database creates -shm/-wal files
    next to itself even on plain reads; the committed fixture and other
    databases this chain must not touch are opened immutable instead."""
    return sqlite3.connect(f"file:{db}?immutable=1", uri=True)


def _rows(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple]:
    cols = ",".join(f'"{c}"' for c in columns)
    return [tuple(row) for row in
            conn.execute(f"SELECT {cols} FROM {table} ORDER BY rowid")]


def _mask(value):
    """Mask generated uuids/timestamps and the generator's scratch-root
    prefix (the recorded evidence path is absolute under the generation
    workdir, which differs per run) — every other value must be identical
    across generations."""
    if isinstance(value, str):
        value = _UUID_RE.sub("<uuid>", value)
        value = _ISO_TS_RE.sub("<ts>", value)
        value = re.sub(r"\S*/evidence-030\.json",
                       "<genproj>/evidence-030.json", value)
    return value


def logical_dump(db: Path) -> dict:
    """Normalized logical content (per table: columns + masked rows, in
    rowid order) — two v0.3.0 generations of the same script compare equal
    even though ids/timestamps differ."""
    conn = _connect_immutable(db)
    try:
        out: dict = {}
        for name, columns in _db_tables(conn).items():
            out[name] = {
                "columns": columns,
                "rows": [[_mask(v) for v in row]
                         for row in _rows(conn, name, columns)],
            }
        return out
    finally:
        conn.close()


def check_fixture_structure(fixture_dir: Path,
                            repo_root: Path = REPO_ROOT) -> tuple[dict, list[str]]:
    """The committed fixture is a genuine, non-trivial v0.3.0 v8 database
    whose provenance file names its source and its digest."""
    problems: list[str] = []
    report: dict = {"fixture": str(fixture_dir)}
    for name in ("state.db", "config.toml", "profiles.toml", "PROVENANCE.md"):
        if not (fixture_dir / name).is_file():
            problems.append(f"fixture file missing: {fixture_dir / name}")
            return report, problems
    report["sha256"] = sha256_file(fixture_dir / "state.db")
    provenance = (fixture_dir / "PROVENANCE.md").read_text()
    if V030_TAG not in provenance:
        problems.append(f"PROVENANCE.md does not name the source tag {V030_TAG}")
    if V030_COMMIT not in provenance:
        problems.append(f"PROVENANCE.md does not name the source commit {V030_COMMIT}")
    if report["sha256"] not in provenance:
        problems.append("PROVENANCE.md does not record the state.db sha256")
    conn = _connect_immutable(fixture_dir / "state.db")
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = row[0] if row else None
        report["schema_version"] = int(version) if version else None
        if version != "8":
            problems.append(f"fixture schema version is {version!r}, expected '8'")
        tables = _db_tables(conn)
        for table in sorted(V11_ADDITIVE_TABLES):
            if table in tables:
                problems.append(
                    f"fixture claims v8 but already has v9+ table {table}"
                )
        if "nonce" in tables.get("attempts", []):
            problems.append("fixture attempts table already has the v11 nonce column")
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in tables}
        report["tables"] = counts
        for table in V8_CORE_TABLES:
            if counts.get(table, 0) == 0:
                problems.append(
                    f"fixture table {table} is empty — the upgrade identity"
                    " check would pass vacuously"
                )
    finally:
        conn.close()
    try:
        commit = v030_commit(repo_root)
    except ReleaseCheckError as exc:
        problems.append(str(exc))
        commit = None
    report["tag_commit"] = commit
    if commit is not None and commit != V030_COMMIT:
        problems.append(
            f"local {V030_TAG} points at {commit}; the fixture provenance"
            f" says {V030_COMMIT}"
        )
    return report, problems


def verify_fixture_provenance(fixture_dir: Path, repo_root: Path,
                              venv_python: Path, workdir: Path) -> dict:
    """Re-run the real v0.3.0 generation and prove the committed fixture is
    exactly what that code produces (normalized over generated ids and
    timestamps) — not a hand-downgraded current database."""
    report: dict = {"problems": []}
    gen = generate_v030_fixture(workdir / "regen-fixture", repo_root,
                                venv_python, workdir / "regen-work")
    committed = logical_dump(fixture_dir / "state.db")
    regenerated = logical_dump((workdir / "regen-fixture" / "state.db"))
    report["regenerated_schema_version"] = gen.get("schema_version")
    report["regenerated_tables"] = gen.get("tables")
    if committed == regenerated:
        report["matches_regeneration"] = True
    else:
        differing = sorted(
            name for name in set(committed) | set(regenerated)
            if committed.get(name) != regenerated.get(name)
        )
        report["matches_regeneration"] = False
        report["differing_tables"] = differing
        report["problems"].append(
            f"committed fixture differs from a fresh {V030_TAG} regeneration"
            f" (tables: {', '.join(differing)}); regenerate it with"
            " scripts/check_release.py --generate-fixture"
        )
    for name in ("config.toml", "profiles.toml"):
        if (fixture_dir / name).read_bytes() != (workdir / "regen-fixture" / name).read_bytes():
            report["problems"].append(
                f"fixture {name} differs from the v0.3.0 regeneration"
            )
    return report


# ---------------------------------------------------------------------------
# Section 8: upgrade a COPY of the fixture with the installed new version
# and prove the whole upgrade/rollback contract of docs/upgrade-0.3.1.md §6.


def compare_v8_to_upgraded(old_db: Path, new_db: Path) -> tuple[dict, list[str]]:
    """Row-by-row identity of every pre-existing table and column, plus the
    additive defaults of the new schema."""
    problems: list[str] = []
    detail: dict = {}
    old = _connect_immutable(old_db)
    new = sqlite3.connect(new_db)
    try:
        old_tables = _db_tables(old)
        new_tables = _db_tables(new)
        added_tables = set(new_tables) - set(old_tables)
        if added_tables != set(V11_ADDITIVE_TABLES):
            problems.append(
                f"upgrade added tables {sorted(added_tables)}, expected exactly"
                f" {sorted(V11_ADDITIVE_TABLES)}"
            )
        for table in sorted(set(new_tables) & set(V11_ADDITIVE_TABLES)):
            count = new.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                problems.append(
                    f"additive table {table} is not empty after upgrade"
                    f" ({count} rows were backfilled)"
                )
        added_columns = {
            name: set(new_tables.get(name, [])) - set(cols)
            for name, cols in old_tables.items()
        }
        if added_columns.get("attempts") != {"nonce"}:
            problems.append(
                f"attempts gained columns {sorted(added_columns.get('attempts', []))},"
                " expected exactly ['nonce']"
            )
        for name, cols in sorted(added_columns.items()):
            if name != "attempts" and cols:
                problems.append(
                    f"table {name} gained unexpected columns {sorted(cols)}"
                )
        nonce_set = new.execute(
            "SELECT COUNT(*) FROM attempts WHERE nonce IS NOT NULL").fetchone()[0]
        if nonce_set:
            problems.append(
                f"{nonce_set} pre-existing attempts carry a nonce (must stay NULL)"
            )
        detail["tables_checked"] = {}
        for name in sorted(old_tables):
            if name == "meta":
                continue
            missing = set(old_tables[name]) - set(new_tables.get(name, []))
            if missing:
                problems.append(
                    f"upgraded table {name} lost columns {sorted(missing)}"
                )
                continue
            old_rows = _rows(old, name, old_tables[name])
            new_rows = _rows(new, name, old_tables[name])
            if old_rows != new_rows:
                problems.append(
                    f"table {name}: content differs after upgrade"
                    f" ({len(old_rows)} v8 rows vs {len(new_rows)} v11 rows)"
                )
            detail["tables_checked"][name] = {
                "columns": len(old_tables[name]),
                "rows": len(old_rows),
            }
        old_meta = dict(_rows(old, "meta", ["key", "value"]))
        new_meta = dict(_rows(new, "meta", ["key", "value"]))
        if set(old_meta) != set(new_meta):
            problems.append(
                f"meta keys changed: {sorted(set(old_meta) ^ set(new_meta))}"
            )
        for key in set(old_meta) & set(new_meta):
            if key == "schema_version":
                continue
            if old_meta[key] != new_meta[key]:
                problems.append(f"meta[{key!r}] changed by the upgrade")
        if old_meta.get("schema_version") != "8":
            problems.append("source fixture meta is not schema_version 8")
        if new_meta.get("schema_version") != "11":
            problems.append("upgraded meta is not schema_version 11")
        detail["rows_compared"] = sum(
            entry["rows"] for entry in detail["tables_checked"].values()
        )
    finally:
        old.close()
        new.close()
    return detail, problems


def check_upgrade_chain(fixture_dir: Path, venv_python: Path, orx_bin: Path,
                        workdir: Path, repo_root: Path = REPO_ROOT) -> dict:
    """§6 of the upgrade doc, executed on a copy of the real fixture:
    consistent backup -> automatic v8->v11 migration by the installed new
    version -> full row identity -> additive defaults -> the REAL v0.3.0
    read entry refusing the v11 file without touching it -> the backup
    restored and read back by the REAL v0.3.0 code."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    report: dict = {"steps": [], "problems": []}
    src_db = fixture_dir / "state.db"
    sha_before = sha256_file(src_db)

    def note(label: str, **kwargs) -> None:
        report["steps"].append({"step": label, **kwargs})

    # The working copy (the source sample is never opened for upgrade).
    proj = workdir / "upgraded-project"
    (proj / ".orx").mkdir(parents=True, exist_ok=True)
    for name in ("state.db", "config.toml", "profiles.toml"):
        shutil.copyfile(fixture_dir / name, proj / ".orx" / name)
    # §6.1: a consistent single-file backup BEFORE the new version opens it.
    backup = workdir / "backup-0.3.0.db"
    if backup.exists():
        backup.unlink()
    conn = sqlite3.connect(proj / ".orx" / "state.db")
    conn.execute("VACUUM INTO ?", (str(backup),))
    conn.close()
    note("backup-vacuum-into", path=str(backup), bytes=backup.stat().st_size)

    env = isolated_env(workdir / "home")
    # Any command migrates; status is the documented read entry.
    status = _run([str(orx_bin), "status", "--json"], cwd=proj, env=env)
    note("upgrade-open-new-version", exit_code=status.returncode)
    if status.returncode != 0:
        report["problems"].append(
            f"installed new version could not open the v8 copy"
            f" (exit {status.returncode}): {status.stdout[-400:]}"
            f" {status.stderr[-400:]}"
        )
        return report
    try:
        report["upgraded_envelope"] = json.loads(status.stdout)
    except json.JSONDecodeError:
        report["problems"].append("upgrade status did not emit a JSON envelope")
        return report
    conn = sqlite3.connect(proj / ".orx" / "state.db")
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    report["upgraded_schema_version"] = int(version)
    if version != "11":
        report["problems"].append(f"upgraded schema version {version!r} != '11'")

    detail, problems = compare_v8_to_upgraded(src_db, proj / ".orx" / "state.db")
    report["identity"] = detail
    report["problems"] += problems

    # The REAL v0.3.0 read entry refuses the v11 file and changes nothing.
    v030_src = extract_v030_source(repo_root, workdir / "v030-src")
    sanity = run_v030_cli(venv_python, v030_src, ["version", "--json"],
                          cwd=workdir, env=env)
    try:
        reported = json.loads(sanity.stdout).get("version")
    except json.JSONDecodeError:
        reported = None
    report["old_version_reported"] = reported
    if sanity.returncode != 0 or reported != "0.3.0":
        report["problems"].append(
            f"v0.3.0 bootstrap sanity check failed (exit {sanity.returncode},"
            f" version {reported!r}): {sanity.stderr[-400:]}"
        )
    sha_upgraded = sha256_file(proj / ".orx" / "state.db")
    rejected = run_v030_cli(venv_python, v030_src, ["status", "--json"],
                            cwd=proj, env=env)
    output = (rejected.stdout + rejected.stderr)
    report["old_rejection"] = {
        "exit_code": rejected.returncode,
        "output_tail": output[-800:],
    }
    if rejected.returncode == 0:
        report["problems"].append(
            "the real v0.3.0 read entry ACCEPTED the v11 database"
            " (§6.3 refusal is gone)"
        )
    if "newer than supported version 8" not in output:
        report["problems"].append(
            f"v0.3.0 rejection message unexpected: {output[-400:]}"
        )
    if sha256_file(proj / ".orx" / "state.db") != sha_upgraded:
        report["problems"].append(
            "the refused v0.3.0 read modified the v11 database"
        )

    # §6.4: the backup restores, and the REAL v0.3.0 reads it back.
    restore = workdir / "restored-project"
    (restore / ".orx").mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "profiles.toml"):
        shutil.copyfile(fixture_dir / name, restore / ".orx" / name)
    shutil.copyfile(backup, restore / ".orx" / "state.db")
    restored = run_v030_cli(venv_python, v030_src, ["status", "--json"],
                            cwd=restore, env=env)
    report["restore"] = {"exit_code": restored.returncode}
    if restored.returncode != 0:
        report["problems"].append(
            f"the real v0.3.0 could not read the restored backup:"
            f" {restored.stdout[-300:]} {restored.stderr[-300:]}"
        )
    else:
        try:
            envelope = json.loads(restored.stdout)
            goal_ok = (
                envelope.get("ok") is True
                and "0.3.0 sample" in json.dumps(envelope.get("goal", {}))
            )
            report["restore"]["goal_read_back"] = goal_ok
            if not goal_ok:
                report["problems"].append(
                    "restored backup did not read back the sample Goal"
                )
        except json.JSONDecodeError:
            report["problems"].append("restored backup status was not JSON")

    # The source sample is byte-identical after everything.
    sha_after = sha256_file(src_db)
    report["source_sha256"] = {"before": sha_before, "after": sha_after}
    if sha_before != sha_after:
        report["problems"].append("the chain modified the source fixture")
    return report


def full_report(repo_root: Path = REPO_ROOT, workdir: Path | str | None = None) -> dict:
    """Run every section against a fresh temp dir; returns the report dict
    (``problems`` empty == acceptance green)."""
    import tempfile

    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="orx-release-check-"))
    workdir = Path(workdir)
    wheel = build_wheel(workdir / "dist", repo_root)
    python = install_wheel(wheel, workdir / "venv")
    orx_bin = python.parent / "orx"
    report = run_probe(python, workdir / "scratch")
    report["wheel"] = str(wheel)
    report["repo_root"] = str(repo_root)

    cli = run_cli_flow(python, workdir / "cli-flow")
    report["cli_flow"] = cli

    fixture_dir = repo_root / FIXTURE_DIR
    structure, struct_problems = check_fixture_structure(fixture_dir, repo_root)
    report["fixture"] = structure
    problems = (
        check_package_resources(report)
        + check_preset_install(report)
        + cli["problems"]
        + struct_problems
    )
    if not struct_problems:
        upgrade = check_upgrade_chain(fixture_dir, python, orx_bin,
                                      workdir / "upgrade", repo_root)
        report["upgrade"] = upgrade
        problems += upgrade["problems"]
        provenance = verify_fixture_provenance(fixture_dir, repo_root, python,
                                               workdir / "regen")
        report["fixture_provenance"] = provenance
        problems += provenance["problems"]
    report["problems"] = problems
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=None,
                        help="keep the build/venv/scratch artifacts here"
                             " (default: a fresh temp dir)")
    parser.add_argument("--generate-fixture", type=Path, default=None,
                        metavar="DEST",
                        help="generate the real v0.3.0 schema-v8 sample"
                             " fixture into DEST (requires the local git tag"
                             f" {V030_TAG}) and exit")
    args = parser.parse_args(argv)

    workdir = args.workdir
    cleanup = False
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="orx-release-check-"))
        cleanup = True
    workdir = Path(workdir)
    try:
        if args.generate_fixture is not None:
            wheel = build_wheel(workdir / "dist", REPO_ROOT)
            python = install_wheel(wheel, workdir / "venv")
            report = generate_v030_fixture(args.generate_fixture, REPO_ROOT,
                                           python, workdir / "gen")
            print(json.dumps(report, indent=2))
            return 0
        report = full_report(REPO_ROOT, workdir)
    except ReleaseCheckError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    if report["problems"]:
        for problem in report["problems"]:
            print(f"problem: {problem}", file=sys.stderr)
        return 1
    if cleanup:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
