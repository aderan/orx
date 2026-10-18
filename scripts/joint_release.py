"""Joint frozen-build + acceptance driver for the ORX 0.4.x release combo
(G011 T004): ORX v0.4.0 + orx-analytics v0.4.2.

G012 (analytics-only 0.4.3, minimal extension): the SAME harness releases
orx-analytics 0.4.3 on top of the PUBLISHED ORX v0.4.0 — pass
``--goal-id g012`` (default work dir ``dist/g012``, decision file
``docs/diagnostics/g012/distribution.json``). The ORX product input is
pinned to the published ``v0.4.0`` tag commit (``ORX_V040_COMMIT``): ORX
is not rebuilt from HEAD and NOT re-released, and freeze fails closed
(``orx-pin-moved``) if the published tag ever moves. Every fail-closed
gate, the sample provenance, the channel separation and the
protected-report exclusion are shared verbatim with G011; the one
goal-scoped exemption is the untracked GENERATED build output inside the
analytics clone (``apps/macos/.build``), which can never enter a build
(every build materializes a committed tree). See
docs/diagnostics/g012/joint-acceptance-runbook.md.

Two repositories, one frozen release. Every stage below works from FULL
GIT COMMITS — ``git archive`` checkouts, never the working trees — so
uncommitted workspace content (reports, edits, anything) can never enter
a build. The manifest is the single binding artifact: both source
commits, both versions, the channel, the dependency runtime, the final
artifact SHA256s and per-stage log references. Historical tags are used,
never moved: the existing package-acceptance chain keeps consuming the
real ``v0.3.0`` tag exactly where it is.

Stages (``uv run python scripts/joint_release.py <stage>``):

``self-test``
    Exercise the harness machinery END TO END with a STAND-IN app
    delivery (real frozen commits, real wheels, the app's own engine
    serve entry on the dev runtime) and assert the fail-closed
    behaviors. The report explicitly does NOT claim product acceptance:
    no .app bundle, no GUI run, no signature. Required evidence that is
    absent in the real stages stays absent here too.
``freeze``
    Resolve both repos' HEAD commits, read both versions FROM THE
    COMMITS, verify sample provenance (including regenerating the
    analytics store-v2 sample with the real old code and comparing
    normalized dumps), record channel + runtime, and write the manifest.
``battery``
    G011 T006: the full + package-level checks from ONE-SHOT clean
    checkouts of the frozen commits (local git clones with HEAD detached
    exactly at the frozen commit — real checkouts carrying history and
    tags so the package-acceptance chain can still ``git archive
    v0.3.0``): ORX full pytest + ``scripts/check_release.py``; Analytics
    full pytest + Swift tests + ``scripts/check_install.py``. The
    analytics checkout is placed at the ORX checkout's
    ``.release-workspaces/orx-analytics`` so the joint suite sees the
    release layout. Every check must genuinely pass (exit 0); a failure
    fails the stage naming the failed checks.
``build``
    One-shot checkouts of the frozen commits; ORX wheel + analytics
    sdist/wheel built from them; the final App archive bound (built via
    the frozen checkout's ``build_app.sh`` with ``--build-app``, or
    bound from an existing archive with ``--app-archive``). Without the
    app artifact the stage FAILS CLOSED with a clear error — the app is
    G011 T006's deliverable, and a missing artifact is never a skip.
``candidate``
    G011 T006: write ``candidate.json`` — the frozen candidate binding.
    Requires freeze + battery + build passed; re-digests every artifact,
    verifies package metadata (wheel/sdist METADATA vs the frozen
    versions), the App's version / source commit / channel / bundled
    runtime (Info.plist, in-app manifest, version file, the bundled
    interpreter itself), the channel-honest signature, the distribution
    decision (by sha256), the historical-tag snapshot and the
    uncommitted-inputs guard (only the baseline-protected user report
    may be dirty at freeze time). Never creates a published conclusion
    (``published: false``).
``exercise``
    Install the frozen ORX wheel into a throwaway venv, drive an
    isolated project through the installed CLI (pause / resume / cancel,
    completion DURING the pause, receipt re-transmission), and connect
    the FINAL APP's built-in engine (the app's own
    ``orx_engine_serve.py`` entry on the bundled runtime) over its
    socket protocol: two collects (idempotent), lifecycle facts from
    ``overview``, and read-only assertions (the ORX project database is
    byte-identical after collection; no real-session mirrors appear).
    Fails closed when the app artifacts are absent.
``verify``
    Re-digest every bound artifact, re-check artifact identity against
    the manifest binding, require every stage's evidence and log, and
    REQUIRE app / GUI / signature evidence. Missing evidence fails the
    stage naming it; nothing is ever marked skipped-and-passed.
    ``verify --manifest <candidate.json> --phase candidate`` instead
    re-verifies a FROZEN CANDIDATE read-only (T006's binding: digests,
    identity, clean-checkout battery evidence, package/app metadata,
    signature, decision sha, historical tags, sample provenance) — the
    operator GUI acceptance (T007) and the published channel root (T008)
    are explicitly NOT part of the candidate phase.
``verify-remote``
    The consumer side: verify a published channel root (T008 populates
    it) — byte-identical artifacts, manifest digests, and channel
    separation (internal preview vs official distribution). Fails closed
    until the root exists.

``generate-samples``
    Build the committed joint-release samples into
    ``tests/fixtures/joint-release``: the analytics store-v2 sample is
    produced by the REAL old analytics commit (``git archive`` source,
    its own ``Store``/importer — never a downgrade of a current store),
    with commit, generation command and SHA256 recorded in
    PROVENANCE.md; the old ORX sample is the existing traceable
    ``tests/fixtures/release-0.3.0`` fixture, referenced not copied.

Exit codes: 0 stage green; 1 stage failure (honest, recorded in the
manifest with a machine code); 2 usage error. See
docs/diagnostics/g011/joint-acceptance-runbook.md for the operator run
book; docs/package-acceptance.md §7 for the joint layer.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import time
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYTICS_REPO = REPO_ROOT / ".release-workspaces" / "orx-analytics"
ANALYTICS_VENV_PYTHON = ANALYTICS_REPO / ".venv" / "bin" / "python"

DEFAULT_WORK_DIR = Path("dist") / "g011"

# The joint fixture root (committed) and its two samples.
JOINT_FIXTURE_DIR = Path("tests") / "fixtures" / "joint-release"
STORE_V2_SAMPLE_DIR = JOINT_FIXTURE_DIR / "analytics-store-v2"
V030_FIXTURE_DIR = Path("tests") / "fixtures" / "release-0.3.0"

# The genuine store-v2-era analytics commit: the LAST commit before the
# store v2 -> v3 migration landed (07ab676). At this commit
# orxanalytics.__version__ == 0.4.1 and STORE_VERSION == "2". There is no
# analytics tag for this era (the analytics repo has no tags at all), so
# the full commit is the provenance anchor.
ANALYTICS_STORE_V2_COMMIT = (
    "3f1b66c0234db3cf84d6c2957e34d00bab1b5cee"  # verified via rev-parse
)
ANALYTICS_STORE_V2_VERSION = "0.4.1"

# Kept in lockstep with scripts/check_release.py (the joint chain consumes
# the same real tag; tests/test_joint_release.py asserts the equality so
# the two constants cannot drift).
V030_TAG = "v0.3.0"
V030_COMMIT = "0f2629e6988ba5082a876257c910d78457bd1338"

# Historical tags that must stay exactly where they are for the existing
# package-acceptance chain to keep working.
REQUIRED_ORX_TAGS = ("v0.1.0", "v0.2.0", "v0.2.1", "v0.3.0", "v0.3.1")

CHANNELS = ("internal-preview", "official")
# build_app.sh modes: dev -> "ORX Analytics (Dev).app" + zip;
# dist -> "ORX Analytics.app" + DMG. Internal preview ships the dev
# flavor zip (ad-hoc/Apple Development signing, clearly labeled);
# official distribution ships the dist DMG (Developer ID + notarized).
CHANNEL_APP_MODE = {"internal-preview": "dev", "official": "dist"}

STAGES = (
    "self-test", "freeze", "battery", "build", "candidate",
    "exercise", "verify", "verify-remote",
)

# The fixed distribution decision (G011 T005): the channel + basis are
# chosen BEFORE any build; build_app.sh and the candidate stage both
# consume this file and bind it by sha256.
DECISION_FILE = REPO_ROOT / "docs" / "diagnostics" / "g011" / "distribution.json"

# The one baseline-protected user file (docs/diagnostics/g011/baseline.json
# protected_report): its worktree modification is the ONLY dirt allowed at
# freeze time; it never enters any build (git archive materializes the
# committed state) and is never staged or committed by G011.
PROTECTED_REPORT_ENTRY = " M docs/m1.2-report.md"

# G012: the ORX product input for the analytics-only 0.4.3 release is the
# PUBLISHED v0.4.0 tag commit — ORX is not rebuilt from HEAD and NOT
# re-released. Published tags are immutable (same discipline as v0.3.0
# above): the pin must stay exactly here or freeze/candidate fail closed.
ORX_V040_TAG = "v0.4.0"
ORX_V040_COMMIT = "f1cc7757c953d3fee5bff5630613a6d2fb380a74"

# Goal profiles. The default stays g011 so every existing G011 invocation
# reproduces unchanged; g012 is selected explicitly with --goal-id g012
# (or inferred from an existing work-dir manifest at freeze time).
GOAL_CONFIGS = {
    "g011": {
        "label": "G011",
        "default_work_dir": DEFAULT_WORK_DIR,
        "decision_file": DECISION_FILE,
        # ORX product input = the repo HEAD at freeze time (the joint combo)
        "orx_input": "head",
        "candidate_task": "T006",
        "publish_note": (
            "a candidate is NOT a release: T007 records the operator GUI"
            " acceptance and T008 publishes (push, tags, artifacts); this"
            " file creates no published conclusion"),
    },
    "g012": {
        "label": "G012",
        "default_work_dir": Path("dist") / "g012",
        "decision_file": REPO_ROOT / "docs" / "diagnostics" / "g012"
                         / "distribution.json",
        # ORX product input = the PUBLISHED v0.4.0 tag (analytics-only
        # release; ORX is not re-published)
        "orx_input": f"tag:{ORX_V040_TAG}",
        "candidate_task": None,
        "publish_note": (
            "a candidate is NOT a release: the operator GUI acceptance and"
            " the publish step (push, tag, artifacts) happen afterwards and"
            " are recorded separately; this file creates no published"
            " conclusion. ORX is NOT re-published: the ORX product input"
            " stays the published v0.4.0 tag"),
    },
}

# G012: generated build-output directories inside the analytics clone
# (SwiftPM's apps/macos/.build cache) that `git status --porcelain` shows
# as untracked but that can never enter a build — every build materializes
# a COMMITTED tree (git archive / git clone of the freeze commit), so
# these paths are pure local toolchain output. The exemption is EXACTLY
# these untracked generated prefixes and only for goal g012: every other
# uncommitted analytics entry still refuses the candidate, and the whole
# G011 guard is unchanged.
ANALYTICS_GENERATED_UNTRACKED = ("apps/macos/.build/",)


def analytics_generated_exempt(entry: str) -> bool:
    return entry.startswith("?? ") and any(
        entry[3:].startswith(prefix)
        for prefix in ANALYTICS_GENERATED_UNTRACKED)


def goal_config(goal_id: str | None) -> dict:
    goal_id = goal_id or "g011"
    try:
        return GOAL_CONFIGS[goal_id]
    except KeyError:
        raise JointReleaseError(
            f"unknown goal id {goal_id!r} (known: {sorted(GOAL_CONFIGS)})")

# The clean-checkout battery (G011 T006): the required check names whose
# evidence the candidate phase re-verifies.
REQUIRED_BATTERY_CHECKS = (
    "orx_pytest", "orx_check_release",
    "analytics_pytest", "analytics_swift_test", "analytics_check_install",
)

_RUN_TIMEOUT_SEC = 600
_BATTERY_TIMEOUT_SEC = 3600   # a full suite in a fresh env can take minutes
_APP_BUILD_TIMEOUT_SEC = 1800
_ENGINE_START_TIMEOUT_SEC = 30
_ENGINE_REFRESH_TIMEOUT_SEC = 45

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


class JointReleaseError(RuntimeError):
    """The harness itself could not run (tool missing, repo missing...)."""


class StageFailure(RuntimeError):
    """A stage ran and honestly FAILED. ``code`` is a stable machine code
    (verified by the tests); the message is operator-facing."""

    def __init__(self, stage: str, code: str, message: str, **detail):
        super().__init__(f"[{stage}] {code}: {message}")
        self.stage = stage
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _run(command: list[str], *, cwd: Path, env: dict | None = None,
         timeout: int = _RUN_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=str(cwd), env=env, timeout=timeout,
        capture_output=True, text=True,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", *args], cwd=repo)
    if result.returncode != 0:
        raise JointReleaseError(
            f"git {' '.join(args)} failed in {repo} (exit {result.returncode}):"
            f" {result.stderr.strip()}"
        )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def isolated_env(home: Path) -> dict:
    """Minimal environment isolating every user-level layer (ORX AND
    analytics), with notifications off and quota preflight disabled.
    No PYTHONPATH, no inherited ORX_*/ORX_ANALYTICS_* state. Bytecode
    writing is disabled everywhere: harness-run interpreters (bundled
    runtimes included) must never drop __pycache__ into an extracted
    bundle — that breaks the code-signature seal."""
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "ORX_CONFIG_DIR": str(home / "orx-config"),
        "ORX_DATA_DIR": str(home / "orx-data"),
        "ORX_ZCODE_AGENTS_DIR": str(home / "zcode-agents"),
        "ORX_QUOTA_PREFLIGHT": "0",
        "ORX_ANALYTICS_HOME": str(home / "analytics-data"),
        "ORX_ANALYTICS_NOTIFY": "0",  # never pop a system notification
        "PYTHONDONTWRITEBYTECODE": "1",  # never write into a bundle
    }


class StageLog:
    """Append-only log file for a stage; the manifest carries the
    RELATIVE path so the committed evidence index stays sanitized."""

    def __init__(self, work_dir: Path, stage: str):
        self.work_dir = work_dir
        logs = work_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        existing = sorted(logs.glob(f"*-{stage}.log"))
        number = len(existing) + 1
        self.path = logs / f"{number:02d}-{stage}.log"
        self.rel = f"logs/{self.path.name}"
        self._fh = self.path.open("w", encoding="utf-8")

    def write(self, text: str) -> None:
        self._fh.write(text)
        if not text.endswith("\n"):
            self._fh.write("\n")
        self._fh.flush()

    def command(self, command: list[str], result: subprocess.CompletedProcess,
                env_note: str = "isolated") -> None:
        self.write(f"$ ({env_note}) {' '.join(command)}")
        self.write(f"[exit {result.returncode}]")
        if result.stdout.strip():
            self.write("stdout: " + result.stdout.strip()[-4000:])
        if result.stderr.strip():
            self.write("stderr: " + result.stderr.strip()[-4000:])

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Commit bindings: versions come FROM THE COMMIT, never the working tree
# ---------------------------------------------------------------------------


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def worktree_status(repo: Path) -> dict:
    """Honest dirtiness record. Dirty files NEVER enter a build (builds
    are git archives/clones of the frozen commit); the manifest records
    the state so a reviewer can see what was excluded. Lines are kept
    VERBATIM (``--porcelain`` XY codes carry a leading space for
    worktree-only changes — the candidate's uncommitted-inputs guard
    matches those bytes exactly)."""
    result = _run(["git", "status", "--porcelain"], cwd=repo)
    if result.returncode != 0:
        raise JointReleaseError(
            f"git status --porcelain failed in {repo} (exit"
            f" {result.returncode}): {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {"dirty_files": len(lines), "entries": lines[:50]}


def _version_in_source(text: str, what: str) -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise JointReleaseError(f"no __version__ found in {what}")
    return match.group(1)


def orx_version_at(repo: Path, commit: str) -> str:
    text = _git(repo, "show", f"{commit}:src/orx/__init__.py")
    return _version_in_source(text, f"{repo} @ {commit} src/orx/__init__.py")


def analytics_version_at(repo: Path, commit: str) -> str:
    text = _git(repo, "show", f"{commit}:src/orxanalytics/__init__.py")
    return _version_in_source(
        text, f"{repo} @ {commit} src/orxanalytics/__init__.py")


def archive_commit(repo: Path, commit: str, dest: Path) -> Path:
    """`git archive <commit>` extracted into dest: a one-shot checkout of
    exactly the committed tree (no working-tree files, no .git)."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=str(repo), capture_output=True,
    )
    if proc.returncode != 0:
        raise JointReleaseError(
            f"git archive {commit} failed in {repo}:"
            f" {proc.stderr.decode(errors='replace')}"
        )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def clone_commit(repo: Path, commit: str, dest: Path) -> Path:
    """A one-shot GIT checkout of exactly ``commit``: a local clone with
    HEAD detached at the commit. Unlike ``archive_commit`` this carries
    history and tags, which the clean-checkout battery needs (the
    package-acceptance chain runs ``git archive v0.3.0``; build_app.sh
    reads the COMPLETE source commit via ``git rev-parse HEAD``).
    Uncommitted worktree content of the source never enters a clone."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = _run(["git", "clone", "--no-hardlinks", "--quiet",
                   str(repo), str(dest)], cwd=repo.parent)
    if cloned.returncode != 0:
        raise JointReleaseError(
            f"git clone {repo} -> {dest} failed (exit"
            f" {cloned.returncode}): {cloned.stderr.strip()}")
    checked = _run(["git", "checkout", "--detach", "--quiet", commit],
                   cwd=dest)
    if checked.returncode != 0:
        raise JointReleaseError(
            f"git checkout --detach {commit} failed in {dest}:"
            f" {checked.stderr.strip()}")
    head = _git(dest, "rev-parse", "HEAD")
    _require(head == commit,
             f"clone of {repo} checked out {head}, expected {commit}")
    return dest


def app_pinned_runtime(analytics_repo: Path, commit: str) -> dict:
    """The pinned python-build-standalone runtime the app bundles
    (parsed from the frozen commit's build_app.sh — the app's own
    single source for the runtime pin)."""
    text = _git(analytics_repo, "show", f"{commit}:scripts/build_app.sh")

    def grab(name: str) -> str:
        m = re.search(rf'^{name}="([^"]+)"', text, re.MULTILINE)
        if not m:
            raise JointReleaseError(
                f"{name} not found in build_app.sh @ {commit}")
        return m.group(1)

    return {
        "pbs_tag": grab("PBS_TAG"),
        "pbs_python": grab("PBS_PYTHON"),
        "pbs_name": (
            f"{grab('PBS_PYTHON')}+{grab('PBS_TAG')}"
            "-aarch64-apple-darwin-install_only"
        ),
        "pbs_sha256": grab("PBS_SHA256"),
    }


# ---------------------------------------------------------------------------
# Samples: the analytics store-v2 sample from the REAL old commit
# ---------------------------------------------------------------------------

# Runs under the DEV interpreter with the archived old source forced onto
# sys.path[0] (the old code is stdlib-only, so the dev interpreter
# satisfies it — same technique check_release.py uses for v0.3.0).
STORE_V2_GENERATOR = '''\
"""Build an analytics.db at store v2 with the REAL pre-v3 analytics code.

Usage: python gen.py <archived-old-src/src> <staged-orx-project> <home>
The archive's orxanalytics package shadows everything; every write goes
through the OLD code's own Store/importer. Prints a JSON report.
"""
import json
import sqlite3
import sys
from pathlib import Path

src, proj, home = (Path(a) for a in sys.argv[1:4])
sys.path.insert(0, str(src))

import orxanalytics
assert orxanalytics.__version__ == "0.4.1", orxanalytics.__version__
from orxanalytics.store import Store
from orxanalytics import registry
from orxanalytics.importers.orxstate import import_orx_project

store = Store(home)
registry.add_project(store, str(proj), name="release-0.3.0-sample")
result = import_orx_project(store, str(proj))
assert "error" not in result, result
store.close()

conn = sqlite3.connect(f"file:{home}/analytics.db?immutable=1", uri=True)
report = {
    "orxanalytics_version": orxanalytics.__version__,
    "orxanalytics_file": orxanalytics.__file__,
    "store_version": conn.execute(
        "SELECT value FROM meta WHERE key='store_version'").fetchone()[0],
    "import_counts": result["counts"],
    "snapshots": conn.execute(
        "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0],
    "tables": {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")
    },
}
conn.close()
print(json.dumps(report))
'''


def stage_orx_sample(orx_fixture: Path, dest_proj: Path) -> Path:
    """Stage the committed v0.3.0 fixture as an importable project: copy
    the three files under ``<dest>/.orx/`` and normalize the copy to a
    rollback journal (the committed fixture is a cleanly-closed WAL file
    without sidecars; the analytics read-only open (mode=ro) needs a
    self-contained file — the same discipline the M1.2 snapshot uses).
    The SOURCE fixture is never touched."""
    if not (orx_fixture / "state.db").is_file():
        raise JointReleaseError(f"v0.3.0 fixture missing: {orx_fixture}")
    dest_proj.mkdir(parents=True, exist_ok=True)
    (dest_proj / ".orx").mkdir(parents=True, exist_ok=True)
    for name in ("state.db", "config.toml", "profiles.toml"):
        shutil.copyfile(orx_fixture / name, dest_proj / ".orx" / name)
    conn = sqlite3.connect(dest_proj / ".orx" / "state.db")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()
    return dest_proj


def generate_store_v2_sample(dest: Path, orx_repo: Path, analytics_repo: Path,
                             workdir: Path, log: StageLog | None = None) -> dict:
    """Generate the analytics store-v2 sample with the REAL old commit's
    code; write analytics.db + PROVENANCE.md into ``dest``."""
    dest = Path(dest)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    commit = _git(analytics_repo, "rev-parse",
                  f"{ANALYTICS_STORE_V2_COMMIT}^{{commit}}")
    archive = archive_commit(analytics_repo, commit, workdir / "old-src")
    old_pkg = archive / "src"
    if not (old_pkg / "orxanalytics" / "__init__.py").is_file():
        raise JointReleaseError(
            f"archived analytics @ {commit} has no src/orxanalytics package")

    orx_fixture = orx_repo / V030_FIXTURE_DIR
    source_sha_before = sha256_file(orx_fixture / "state.db")
    staged = stage_orx_sample(orx_fixture, workdir / "sample-project")
    home = workdir / "analytics-home"
    if home.exists():
        shutil.rmtree(home)

    script = workdir / "generate_store_v2.py"
    script.write_text(STORE_V2_GENERATOR)
    env = isolated_env(workdir / "gen-home")
    result = _run([sys.executable, str(script), str(old_pkg),
                   str(staged), str(home)], cwd=workdir, env=env)
    if log:
        log.command([sys.executable, str(script), str(old_pkg),
                     str(staged), str(home)], result)
    if result.returncode != 0:
        raise JointReleaseError(
            f"store-v2 sample generation failed (exit {result.returncode})"
            f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    report = json.loads(result.stdout)
    _require(
        report["store_version"] == "2"
        and report["orxanalytics_version"] == ANALYTICS_STORE_V2_VERSION,
        "old analytics code did not identify as the store-v2 era",
    )
    _require(
        sha256_file(orx_fixture / "state.db") == source_sha_before,
        "generation modified the committed v0.3.0 fixture",
    )

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(home / "analytics.db", dest / "analytics.db")
    digest = sha256_file(dest / "analytics.db")
    generation_command = (
        "uv run python scripts/joint_release.py generate-samples"
        f" --dest {JOINT_FIXTURE_DIR}"
    )
    provenance = (
        f"# Analytics store v2 sample (G011 joint release)\n\n"
        f"Generated {now_iso()} by:\n\n"
        f"    {generation_command}\n\n"
        f"- Source: `git archive {commit}` of the isolated analytics clone"
        f" (.release-workspaces/orx-analytics) — the LAST commit before the"
        f" store v2 → v3 migration (07ab676). The analytics repo has no"
        f" tags; the full commit is the anchor.\n"
        f"- The analytics database is written exclusively by that REAL old"
        f" code (its own `Store`, `registry`, `import_orx_project`); the"
        f" generator asserts `orxanalytics.__version__ =="
        f" \"{ANALYTICS_STORE_V2_VERSION}\"` and reads back"
        f" `meta.store_version == \"2\"` before anything is committed. It is"
        f" never a downgrade of a current store's version number.\n"
        f"- Generator orxanalytics version: {report['orxanalytics_version']}"
        f" (from {report['orxanalytics_file']}).\n"
        f"- Imported ORX sample: the committed real-v0.3.0 fixture"
        f" `tests/fixtures/release-0.3.0` (tag {V030_TAG}, commit"
        f" {V030_COMMIT}), staged as `<tmp>/.orx/{{state.db,config.toml,"
        f"profiles.toml}}` with the copy normalized to a rollback journal"
        f" (self-contained for the read-only open). The fixture's own"
        f" sha256 is asserted unchanged before and after.\n"
        f"- `meta.store_version` at generation: 2.\n"
        f"- Table row counts:"
        f" {json.dumps(report['tables'], sort_keys=True)}.\n"
        f"- analytics.db sha256: `{digest}`.\n"
        f"- Regeneration comparison policy: `verify_samples` re-runs this"
        f" exact generation and compares normalized logical dumps"
        f" (generated uuids/timestamps and the generation root path are"
        f" masked); the committed sample must match.\n"
        f"- Consumers must treat this directory as READ-ONLY: the v2 → v3"
        f" upgrade checks copy `analytics.db` first and assert the source"
        f" digest is unchanged afterwards.\n"
    )
    (dest / "PROVENANCE.md").write_text(provenance)
    report.update(
        fixture=str(dest), sha256=digest, commit=commit,
        provenance=str(dest / "PROVENANCE.md"),
        generation_command=generation_command,
        source_fixture_sha256=source_sha_before,
    )
    return report


def _db_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"):
        tables[name] = [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
    return tables


def _connect_immutable(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?immutable=1", uri=True)


def _mask(value, roots: tuple[str, ...] = ()):
    """Mask generated uuids/timestamps and every absolute path so two
    generations of the same script under different scratch roots compare
    equal. Paths in these tables are always generation-rooted (project
    keys, source ids, source paths), so replacing each absolute-path
    token with one placeholder is deterministic and layout-independent.
    (``roots`` is accepted for call-site symmetry and intentionally
    unused: the path mask below already covers generation roots.)"""
    if isinstance(value, str):
        value = _UUID_RE.sub("<uuid>", value)
        value = _ISO_TS_RE.sub("<ts>", value)
        value = re.sub(r"(?:/[\w.@+-]+)+", "<path>", value)
    return value


def analytics_logical_dump(db: Path, roots: tuple[str, ...] = ()) -> dict:
    conn = _connect_immutable(db)
    try:
        out: dict = {}
        for name, columns in _db_tables(conn).items():
            cols = ",".join(f'"{c}"' for c in columns)
            rows = [[_mask(v, roots) for v in row] for row in
                    conn.execute(f"SELECT {cols} FROM {name} ORDER BY rowid")]
            out[name] = {"columns": columns, "rows": rows}
        return out
    finally:
        conn.close()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise JointReleaseError(message)


def verify_samples(orx_repo: Path, analytics_repo: Path,
                   joint_fixture: Path | None = None,
                   workroot: Path | None = None) -> tuple[dict, list[str]]:
    """Both committed samples are genuine, traceable and intact:

    - the old ORX sample is the existing release-0.3.0 fixture (schema
      v8, PROVENANCE sha matches, the real tag still points at the
      provenance commit);
    - the analytics store-v2 sample exists, its PROVENANCE names the
      real old commit / the generation command / its own sha256,
      ``meta.store_version`` is 2, the commit exists in the analytics
      repo, and a FRESH regeneration with that same old code produces a
      normalized-identical logical dump (never a hand-downgraded store).
    """
    import tempfile

    problems: list[str] = []
    report: dict = {}
    joint_fixture = joint_fixture or (orx_repo / JOINT_FIXTURE_DIR)
    if workroot is None:
        workroot = Path(tempfile.mkdtemp(prefix="jr-verify-samples-"))

    # -- the old ORX sample: the existing traceable fixture --------------
    orx_fixture = orx_repo / V030_FIXTURE_DIR
    report["orx_old_sample"] = {"path": str(V030_FIXTURE_DIR)}
    if not (orx_fixture / "state.db").is_file():
        problems.append(f"old ORX sample fixture missing: {orx_fixture}")
    else:
        digest = sha256_file(orx_fixture / "state.db")
        report["orx_old_sample"]["sha256"] = digest
        provenance = (orx_fixture / "PROVENANCE.md").read_text()
        if digest not in provenance:
            problems.append("release-0.3.0 PROVENANCE sha mismatch")
        tag_commit = _git(orx_repo, "rev-parse", f"{V030_TAG}^{{commit}}")
        report["orx_old_sample"]["tag_commit"] = tag_commit
        if tag_commit != V030_COMMIT:
            problems.append(
                f"tag {V030_TAG} moved: {tag_commit} != {V030_COMMIT}")
        conn = _connect_immutable(orx_fixture / "state.db")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            report["orx_old_sample"]["schema_version"] = row[0] if row else None
            if not row or row[0] != "8":
                problems.append("old ORX sample is not schema v8")
        finally:
            conn.close()

    # -- historical tags kept for the existing package-acceptance chain --
    tags = _git(orx_repo, "tag", "--list").splitlines()
    missing = [t for t in REQUIRED_ORX_TAGS if t not in tags]
    report["orx_tags"] = sorted(t for t in tags if t.startswith("v0."))
    if missing:
        problems.append(f"historical ORX tags missing: {missing}")

    # -- the analytics store-v2 sample -----------------------------------
    sample = joint_fixture / STORE_V2_SAMPLE_DIR.name
    report["analytics_store_v2"] = {"path": str(STORE_V2_SAMPLE_DIR)}
    db = sample / "analytics.db"
    if not db.is_file():
        problems.append(f"analytics store-v2 sample missing: {db}")
        return report, problems
    digest = sha256_file(db)
    report["analytics_store_v2"]["sha256"] = digest
    provenance_path = sample / "PROVENANCE.md"
    if not provenance_path.is_file():
        problems.append("store-v2 sample has no PROVENANCE.md")
        return report, problems
    provenance = provenance_path.read_text()
    commit = _git(analytics_repo, "rev-parse",
                  f"{ANALYTICS_STORE_V2_COMMIT}^{{commit}}")
    report["analytics_store_v2"]["commit"] = commit
    for needle, what in (
        (commit, "the generating commit"),
        (digest, "its own sha256"),
        ("generate-samples", "the generation command"),
    ):
        if needle not in provenance:
            problems.append(f"store-v2 PROVENANCE does not record {what}")
    conn = _connect_immutable(db)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone()
        version = row[0] if row else None
        report["analytics_store_v2"]["store_version"] = version
        if version != "2":
            problems.append(
                f"store-v2 sample meta.store_version is {version!r},"
                " expected '2'")
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in _db_tables(conn)}
        report["analytics_store_v2"]["tables"] = counts
        if counts.get("orx_attempts", 0) == 0 or counts.get("orx_snapshots", 0) == 0:
            problems.append("store-v2 sample is trivially empty")
    finally:
        conn.close()

    # -- regeneration: the committed sample IS what the old code makes ---
    regen_dir = workroot / "regen"
    regen = generate_store_v2_sample(regen_dir, orx_repo, analytics_repo,
                                     workroot / "regen-work")
    committed = analytics_logical_dump(db)
    fresh = analytics_logical_dump(
        regen_dir / "analytics.db",
        roots=(str(workroot), str(workroot.resolve())),
    )
    # The committed sample was generated under a different scratch root;
    # normalize both through the same mask by re-dumping the committed
    # file with its own recorded generation context masked generically.
    report["analytics_store_v2"]["regenerated_store_version"] = \
        regen["store_version"]
    if committed == fresh:
        report["analytics_store_v2"]["matches_regeneration"] = True
    else:
        differing = sorted(
            name for name in set(committed) | set(fresh)
            if committed.get(name) != fresh.get(name))
        report["analytics_store_v2"]["matches_regeneration"] = False
        report["analytics_store_v2"]["differing_tables"] = differing
        problems.append(
            "committed store-v2 sample differs from a fresh regeneration by"
            f" the real old code (tables: {', '.join(differing)});"
            " regenerate with scripts/joint_release.py generate-samples")
    return report, problems


# ---------------------------------------------------------------------------
# The old-code rejection behaviors (source rollback / upgrade chain)
# ---------------------------------------------------------------------------


OLD_REFUSAL_PROBE = '''\
"""The REAL old analytics importer against a NEWER ORX schema: the gate
must refuse honestly (rolling the analytics source back never silently
drops newer data)."""
import json
import sys
from pathlib import Path

src, proj = (Path(a) for a in sys.argv[1:3])
sys.path.insert(0, str(src))
import orxanalytics
from orxanalytics.store import Store
from orxanalytics.importers.orxstate import import_orx_project

store = Store(Path(sys.argv[3]))
result = import_orx_project(store, str(proj))
store.close()
print(json.dumps({"version": orxanalytics.__version__, "result": result}))
'''


def old_analytics_refuses_newer_orx(orx_repo: Path, analytics_repo: Path,
                                    workdir: Path) -> dict:
    """源码回退导入: the archived store-v2-era analytics code meets a
    current schema-v15 ORX project and REFUSES it (gate), leaving the
    source untouched. The v15 database is written by the CURRENT repo's
    own orx.state (the code under release, in the dev environment)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    commit = _git(analytics_repo, "rev-parse",
                  f"{ANALYTICS_STORE_V2_COMMIT}^{{commit}}")
    old_pkg = archive_commit(analytics_repo, commit,
                             workdir / "old-src") / "src"

    proj = workdir / "v15-project"
    (proj / ".orx").mkdir(parents=True, exist_ok=True)
    code = (
        "import sys; from pathlib import Path;"
        "from orx.state import Store;"
        "s = Store.open(Path(sys.argv[1]) / '.orx' / 'state.db');"
        "s.goal_create('joint v15 sample', ['c'], [], '');"
        "s.close()"
    )
    made = _run([sys.executable, "-c", code, str(proj)], cwd=workdir)
    if made.returncode != 0:
        raise JointReleaseError(
            f"could not create the v15 sample project: {made.stderr}")
    sha_before = sha256_file(proj / ".orx" / "state.db")

    script = workdir / "old_refusal.py"
    script.write_text(OLD_REFUSAL_PROBE)
    env = isolated_env(workdir / "home")
    probe = _run([sys.executable, str(script), str(old_pkg), str(proj),
                  str(workdir / "analytics-home")], cwd=workdir, env=env)
    _require(probe.returncode == 0, f"old-code refusal probe failed: {probe.stderr}")
    payload = json.loads(probe.stdout)
    result = payload["result"]
    _require("error" in result, f"old analytics ACCEPTED a v15 project: {result}")
    _require("schema gate refuses" in json.dumps(result),
             f"refusal is not the schema gate: {result}")
    _require(
        sha256_file(proj / ".orx" / "state.db") == sha_before,
        "the refused old-code import modified the v15 source project")
    return {
        "old_analytics_version": payload["version"],
        "old_commit": commit,
        "refusal": result["error"],
        "gaps": result.get("gaps", []),
        "source_untouched": True,
    }


def current_analytics_upgrades_store_v2(analytics_repo: Path, sample_db: Path,
                                        workdir: Path) -> dict:
    """The CURRENT analytics code (the clone's own venv — the release
    environment) opens a COPY of the store-v2 sample and upgrades it in
    place through every additive step to the CURRENT store version (v3
    receipts mirror, v4 zc_requests per-request token columns), keeps
    every row, keeps the source byte-identical, and still refuses a
    foreign future version. Uses the clone's interpreter exactly like
    its own tests."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if not ANALYTICS_VENV_PYTHON.is_file():
        raise JointReleaseError(
            f"analytics clone venv interpreter missing: {ANALYTICS_VENV_PYTHON}"
            " (G011 T001 creates it; see docs/diagnostics/g011/baseline.json)")
    sha_before = sha256_file(sample_db)

    def run_code(code: str, home: Path):
        return _run(
            [str(ANALYTICS_VENV_PYTHON), "-c", textwrap.dedent(code), str(home)],
            cwd=workdir, env=isolated_env(workdir / "home"),
        )

    upgrade_home = workdir / "up-home"
    upgrade_home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample_db, upgrade_home / "analytics.db")
    upgraded = run_code(
        """
        import json, sqlite3, sys
        from pathlib import Path
        from orxanalytics.store import Store, STORE_VERSION
        store = Store(Path(sys.argv[1]))
        conn = sqlite3.connect(
            f"file:{Path(sys.argv[1])/'analytics.db'}?immutable=1", uri=True)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orx_receipts)")}
        zc_cols = {r[1] for r in conn.execute("PRAGMA table_info(zc_requests)")}
        version = conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone()[0]
        kept = conn.execute("SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
        conn.close(); store.close()
        import orxanalytics
        print(json.dumps({
            "version": orxanalytics.__version__,
            "store_version": version,
            "current_store_version": STORE_VERSION,
            "receipts_columns": sorted(cols),
            "zc_request_columns": sorted(zc_cols),
            "snapshots_kept": kept,
        }))
        """,
        upgrade_home,
    )
    _require(upgraded.returncode == 0,
             f"store v2 upgrade chain failed: {upgraded.stderr}")
    facts = json.loads(upgraded.stdout)
    _require(facts["store_version"] == "4",
             f"upgraded store_version {facts['store_version']!r} != '4'")
    _require(facts["store_version"] == facts["current_store_version"],
             "upgraded store_version disagrees with the analytics module's"
             f" STORE_VERSION {facts['current_store_version']!r}")
    _require("delivery_digest" in facts["receipts_columns"],
             "orx_receipts mirror missing after upgrade")
    _require({"input_tokens", "output_tokens", "cache_read",
              "computed_total"} <= set(facts["zc_request_columns"]),
             "zc_requests per-request token columns missing after upgrade")
    _require(facts["snapshots_kept"] > 0, "upgrade lost the sample rows")
    _require(sha256_file(sample_db) == sha_before,
             "the upgrade touched the committed store-v2 sample")

    future_home = workdir / "future-home"
    future_home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample_db, future_home / "analytics.db")
    conn = sqlite3.connect(future_home / "analytics.db")
    conn.execute("UPDATE meta SET value='9' WHERE key='store_version'")
    conn.commit()
    conn.close()
    refused = run_code(
        """
        import sys
        from pathlib import Path
        from orxanalytics.store import Store
        try:
            Store(Path(sys.argv[1]))
        except RuntimeError as exc:
            print(str(exc)); raise SystemExit(0)
        raise SystemExit("future store version was ACCEPTED")
        """,
        future_home,
    )
    _require(refused.returncode == 0 and "store_version 9" in refused.stdout,
             f"future-version refusal missing: {refused.stdout}{refused.stderr}")
    return {
        "current_analytics_version": facts["version"],
        "upgraded_store_version": facts["store_version"],
        "receipts_mirror": True,
        "zc_request_token_columns": True,
        "snapshots_kept": facts["snapshots_kept"],
        "future_version_refused": True,
        "source_unchanged": True,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def manifest_path(work_dir: Path) -> Path:
    return work_dir / "manifest.json"


def load_manifest(work_dir: Path) -> dict:
    path = manifest_path(work_dir)
    if not path.is_file():
        raise StageFailure("manifest", "manifest-missing",
                           f"no manifest under {work_dir}; run freeze first")
    return json.loads(path.read_text())


def save_manifest(work_dir: Path, manifest: dict) -> Path:
    path = manifest_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def record_stage(manifest: dict, stage: str, status: str, log_rel: str,
                 **evidence) -> dict:
    record = {
        "stage": stage,
        "status": status,  # passed | failed  (never 'skipped')
        "finished_at": now_iso(),
        "log": log_rel,
    }
    record.update(evidence)
    manifest.setdefault("stages", {})[stage] = record
    return record


def evidence_index(work_dir: Path, manifest: dict) -> Path:
    """The SANITIZED evidence index: relative paths only, no environment
    dumps, no absolute user paths. Raw logs stay local under the work
    dir (gitignored); this file is the committable index."""
    binding = manifest.get("binding", {})
    goal_label = binding.get("goal_label") or "G011"
    lines = [
        f"# {goal_label} joint release — sanitized evidence index",
        "",
        f"Updated: {now_iso()}",
        "",
        "Run materials live under this work directory (gitignored); only",
        "this index is meant for committing. Paths are relative to the",
        "work directory. Every stage record must say passed; a missing or",
        "skipped stage is a failure, never a pass.",
        "",
        "## Binding",
        "",
        f"- channel: {binding.get('channel')}",
        f"- ORX: {binding.get('orx', {}).get('version')}"
        f" @ {str(binding.get('orx', {}).get('commit'))[:12]}"
        f" (full commit in manifest.json)",
        f"- orx-analytics: {binding.get('analytics', {}).get('version')}"
        f" @ {str(binding.get('analytics', {}).get('commit'))[:12]}",
        f"- samples: orx-old {binding.get('samples', {}).get('orx_old', {}).get('sha256', '')[:16]}…,"
        f" analytics-store-v2 {binding.get('samples', {}).get('analytics_store_v2', {}).get('sha256', '')[:16]}…",
        "",
        "## Artifacts",
        "",
    ]
    artifacts = manifest.get("artifacts", {})
    for name, row in sorted(artifacts.items()):
        if isinstance(row, dict) and row.get("sha256"):
            lines.append(
                f"- {name}: `{row.get('path')}` sha256 `{row['sha256'][:16]}…`"
                f" (built_from {str(row.get('built_from'))[:12]})")
    lines += ["", "## Stages", ""]
    for stage in STAGES:
        record = manifest.get("stages", {}).get(stage)
        if record is None:
            lines.append(f"- {stage}: NOT RUN")
        else:
            lines.append(
                f"- {stage}: {record['status']} (log `{record['log']}`)")
    if manifest.get("selftest", {}).get("stand_in_app_delivery"):
        lines += [
            "",
            "## Stand-in disclosure",
            "",
            "The self-test record marks its app delivery as a STAND-IN",
            "(dev runtime, no .app bundle, unsigned). It exercises harness",
            "machinery only and does not claim product acceptance.",
        ]
    lines.append("")
    path = work_dir / "EVIDENCE-INDEX.md"
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Stage: freeze
# ---------------------------------------------------------------------------


def stage_freeze(work_dir: Path, channel: str, log: StageLog,
                 goal_id: str = "g011") -> dict:
    if channel not in CHANNELS:
        raise JointReleaseError(f"unknown channel {channel!r}")
    config = goal_config(goal_id)
    if config["orx_input"] == f"tag:{ORX_V040_TAG}":
        # G012: the ORX product input is the PUBLISHED v0.4.0 tag commit —
        # never the (moving) HEAD, never a re-pin.
        orx_head = _git(REPO_ROOT, "rev-parse", f"{ORX_V040_TAG}^{{commit}}")
        if orx_head != ORX_V040_COMMIT:
            raise StageFailure(
                "freeze", "orx-pin-moved",
                f"tag {ORX_V040_TAG} no longer points at the published"
                f" commit {ORX_V040_COMMIT} (found {orx_head}); the G012"
                " ORX product input is the published release — a moved tag"
                " is a stop, never a re-pin")
        orx_input = {
            "input": f"tag:{ORX_V040_TAG}",
            "pinned_tag": {
                "tag": ORX_V040_TAG,
                "commit": orx_head,
                "published_commit": ORX_V040_COMMIT,
            },
        }
    else:
        orx_head = head_commit(REPO_ROOT)
        orx_input = {"input": "head"}
    an_head = head_commit(ANALYTICS_REPO)
    orx_version = orx_version_at(REPO_ROOT, orx_head)
    an_version = analytics_version_at(ANALYTICS_REPO, an_head)
    tag_commit = _git(REPO_ROOT, "rev-parse", f"{V030_TAG}^{{commit}}")
    if tag_commit != V030_COMMIT:
        raise StageFailure(
            "freeze", "historical-tag-moved",
            f"tag {V030_TAG} no longer points at {V030_COMMIT}"
            f" (found {tag_commit}); historical tags must stay as-is for"
            " the existing package-acceptance chain")
    log.write(
        f"goal {config['label']} ({goal_id})\n"
        f"frozen ORX {orx_version} @ {orx_head}"
        f" (input: {orx_input['input']})\n"
        f"frozen orx-analytics {an_version} @ {an_head}\n"
        f"channel {channel}; v0.3.0 tag intact at {tag_commit}")

    samples, problems = verify_samples(REPO_ROOT, ANALYTICS_REPO,
                                       workroot=work_dir / "freeze-samples")
    if problems:
        raise StageFailure("freeze", "samples-invalid",
                           "sample provenance check failed", problems=problems)
    log.write(f"samples verified: {json.dumps(samples, sort_keys=True)[:2000]}")

    uv_version = _run(["uv", "--version"], cwd=REPO_ROOT).stdout.strip()
    binding = {
        "goal_id": goal_id,
        "goal_label": config["label"],
        "channel": channel,
        "frozen_at": now_iso(),
        "orx": {
            "commit": orx_head,
            "version": orx_version,
            **orx_input,
            "worktree": worktree_status(REPO_ROOT),
            "old_tag_for_package_acceptance": {
                "tag": V030_TAG, "commit": tag_commit},
        },
        "analytics": {
            "commit": an_head,
            "version": an_version,
            "worktree": worktree_status(ANALYTICS_REPO),
            "store_v2_sample_commit": _git(
                ANALYTICS_REPO, "rev-parse",
                f"{ANALYTICS_STORE_V2_COMMIT}^{{commit}}"),
            "app_pinned_runtime": app_pinned_runtime(ANALYTICS_REPO, an_head),
        },
        "samples": {
            "orx_old": {
                "path": str(V030_FIXTURE_DIR),
                "tag": V030_TAG,
                "commit": tag_commit,
                "sha256": samples["orx_old_sample"]["sha256"],
                "schema_version": samples["orx_old_sample"]["schema_version"],
            },
            "analytics_store_v2": {
                "path": str(STORE_V2_SAMPLE_DIR),
                "commit": samples["analytics_store_v2"]["commit"],
                "orxanalytics_version": ANALYTICS_STORE_V2_VERSION,
                "store_version": "2",
                "sha256": samples["analytics_store_v2"]["sha256"],
                "generation_command":
                    "uv run python scripts/joint_release.py"
                    " generate-samples --dest tests/fixtures/joint-release",
            },
        },
        "runtime": {
            "harness_python": sys.version.split()[0],
            "harness_sqlite": sqlite3.sqlite_version,
            "uv": uv_version,
            "platform": sys.platform,
        },
    }
    if goal_id == "g012":
        # The G012 acceptance driver commit (the ORX commit carrying the
        # extended driver/runbook) is recorded SEPARATELY from the two
        # product inputs: provenance only, never a build input.
        binding["acceptance_driver"] = {
            "repo": "orx",
            "commit": head_commit(REPO_ROOT),
            "note": (
                "the ORX commit carrying the G012 driver/runbook — recorded"
                " for provenance only, NOT a build input: the ORX product"
                " input is the pinned v0.4.0 tag (orx.pinned_tag) and the"
                " analytics freeze commit is analytics.commit"),
        }
    manifest = load_manifest(work_dir) if manifest_path(work_dir).is_file() \
        else {"binding": None, "artifacts": {}, "stages": {}}
    previous = manifest.get("binding") or {}
    if previous.get("orx", {}).get("commit") != orx_head \
            or previous.get("analytics", {}).get("commit") != an_head:
        # The frozen commits moved: every downstream stage record and
        # artifact belongs to the PREVIOUS freeze and must be re-earned
        # against the new binding — never carried over.
        stale = sorted(s for s in manifest.get("stages", {})
                       if s not in ("freeze", "self-test"))
        if stale or manifest.get("artifacts"):
            log.write(
                f"binding changed; resetting downstream stage records"
                f" ({', '.join(stale) or 'none'}) and artifacts from the"
                " previous freeze — the chain re-runs against the new"
                " frozen commits")
        manifest["stages"] = {
            key: value for key, value in manifest.get("stages", {}).items()
            if key in ("freeze", "self-test")}
        manifest["artifacts"] = {}
    manifest["binding"] = binding
    record_stage(manifest, "freeze", "passed", log.rel,
                 goal_id=goal_id, goal_label=config["label"],
                 commits={"orx": orx_head, "analytics": an_head},
                 versions={"orx": orx_version, "analytics": an_version},
                 channel=channel, samples=samples)
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Stage: battery — the full + package-level checks from clean checkouts
# ---------------------------------------------------------------------------


def provision_dogfood_snapshot(dest_repo: Path) -> str | None:
    """A self-contained snapshot of the release workspace's dogfood
    database, taken with the sqlite backup API (consistent read; the
    source is never modified). The m12 acceptance measures this LIVE
    fixture — it is gitignored runtime state, so provisioning it into a
    clean checkout is environment setup, never release source."""
    live = REPO_ROOT / ".orx" / "state.db"
    if not live.is_file():
        return None
    dest_dir = dest_repo / ".orx"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "state.db"
    source = sqlite3.connect(str(live))
    snapshot = sqlite3.connect(str(dest))
    try:
        source.backup(snapshot)
    finally:
        source.close()
        snapshot.close()
    return str(dest)


def provision_analytics_venv(an_clone: Path, log: StageLog) -> Path:
    """The T001 procedure applied to a clean analytics checkout: a venv
    pinned to the harness interpreter (the SQLite WAL discipline recorded
    in the runbook) with the frozen source installed editable. The
    current-analytics probes and the joint suite expect
    ``<analytics repo>/.venv/bin/python`` to exist."""
    venv_python = an_clone / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return venv_python
    created = _run(["uv", "venv", "--python", sys.executable,
                    str(an_clone / ".venv")], cwd=an_clone)
    _require(created.returncode == 0,
             f"uv venv failed for the clean analytics checkout:"
             f" {created.stderr}")
    installed = _run(["uv", "pip", "install", "--python", str(venv_python),
                      "-e", str(an_clone)], cwd=an_clone)
    _require(installed.returncode == 0,
             f"editable install into the clean analytics venv failed:"
             f" {installed.stderr}")
    log.write("analytics venv provisioned in the clean checkout (T001"
              " procedure: harness interpreter, editable install)")
    return venv_python


def stage_battery(work_dir: Path, log: StageLog) -> dict:
    """G011 T006: run BOTH repositories' full and package-level checks
    from one-shot clean checkouts of the frozen commits. The checkouts
    are real git checkouts (clone + detach) so the package-acceptance
    chain still finds the historical tags; every command must exit 0 —
    a single failure fails the stage naming it."""
    manifest = load_manifest(work_dir)
    freeze = manifest.get("stages", {}).get("freeze", {})
    if freeze.get("status") != "passed":
        raise StageFailure("battery", "missing-step-evidence",
                           "freeze has not passed; run freeze first")
    binding = manifest["binding"]
    orx_commit = binding["orx"]["commit"]
    an_commit = binding["analytics"]["commit"]

    battery_dir = work_dir / "battery"
    orx_clone = clone_commit(REPO_ROOT, orx_commit, battery_dir / "orx")
    # The joint suite resolves the analytics repo at
    # <orx root>/.release-workspaces/orx-analytics: provision the clean
    # analytics checkout exactly there (the release workspace layout).
    joint_layout = orx_clone / ".release-workspaces"
    joint_layout.mkdir(parents=True, exist_ok=True)
    an_clone = clone_commit(ANALYTICS_REPO, an_commit,
                            joint_layout / "orx-analytics")
    provision_analytics_venv(an_clone, log)
    dogfood = provision_dogfood_snapshot(orx_clone)
    if dogfood:
        log.write(f"dogfood fixture provisioned: {dogfood}"
                  " (sqlite backup snapshot of the release workspace's"
                  " live .orx/state.db; the m12 acceptance measures it;"
                  " gitignored runtime state, never release source)")
    log.write(f"clean checkouts: orx@{orx_commit[:12]}"
              f" analytics@{an_commit[:12]} (git clone, detached HEAD;"
              " no working-tree content)")

    commands = (
        ("orx_pytest",
         "the frozen ORX commit's full test suite",
         ["uv", "run", "pytest", "-q"], orx_clone),
        ("orx_check_release",
         "the frozen ORX commit's package acceptance chain",
         ["uv", "run", "python", "scripts/check_release.py"], orx_clone),
        ("analytics_pytest",
         "the frozen analytics commit's full test suite",
         # module form: the analytics suite imports `scripts.…` from the
         # repo root, which `-m pytest` puts on sys.path
         ["uv", "run", "--extra", "dev", "python", "-m", "pytest", "-q"],
         an_clone),
        ("analytics_swift_test",
         "the frozen analytics commit's Swift test suite",
         ["swift", "test"], an_clone / "apps" / "macos"),
        ("analytics_check_install",
         "the frozen analytics commit's sdist/wheel/install check",
         ["uv", "run", "--extra", "dev", "python",
          "scripts/check_install.py"], an_clone),
    )
    checks: list[dict] = []
    failed: list[str] = []
    for name, what, command, cwd in commands:
        log.write(f"$ ({name}) {' '.join(command)}  # {what}")
        result = _run(command, cwd=cwd, timeout=_BATTERY_TIMEOUT_SEC)
        log.command(command, result, env_note=f"clean checkout ({name})")
        log.write(f"battery {name}: exit {result.returncode} ({what})")
        checks.append({
            "name": name,
            "command": " ".join(command),
            "what": what,
            "exit_code": result.returncode,
            "log": log.rel,
        })
        if result.returncode != 0:
            failed.append(name)

    record_stage(
        manifest, "battery", "failed" if failed else "passed", log.rel,
        checks=checks, failed=failed,
        checkouts={
            "orx": "battery/orx",
            "analytics": "battery/orx/.release-workspaces/orx-analytics",
        },
        fixtures_provisioned=[
            "analytics .venv (T001 procedure)",
            "dogfood .orx/state.db snapshot (sqlite backup)",
        ] if dogfood else ["analytics .venv (T001 procedure)"],
        commits={"orx": orx_commit, "analytics": an_commit},
    )
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    if failed:
        raise StageFailure(
            "battery", "battery-check-failed",
            f"clean-checkout battery failed: {', '.join(failed)}",
            failed=failed)
    return manifest


# ---------------------------------------------------------------------------
# Build: one-shot checkouts of the frozen commits
# ---------------------------------------------------------------------------


def build_orx_wheel(checkout: Path, out_dir: Path) -> Path:
    """uv build inside the archived checkout — never the working tree."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(["uv", "build", "--wheel", "--out-dir", str(out_dir)],
                  cwd=checkout)
    if result.returncode != 0:
        raise JointReleaseError(
            f"uv build failed in {checkout} (exit {result.returncode})"
            f"\n{result.stdout}\n{result.stderr}")
    wheels = sorted(out_dir.glob("orx_agent-*.whl"))
    _require(wheels, f"no orx_agent wheel produced in {out_dir}")
    return wheels[-1]


def build_analytics_dist(checkout: Path, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(["uv", "build", "--out-dir", str(out_dir)], cwd=checkout)
    if result.returncode != 0:
        raise JointReleaseError(
            f"uv build failed for analytics in {checkout}"
            f" (exit {result.returncode})\n{result.stdout}\n{result.stderr}")
    wheel = sorted(out_dir.glob("orx_analytics-*.whl"))
    sdist = sorted(out_dir.glob("orx_analytics-*.tar.gz"))
    _require(wheel and sdist, f"analytics dist incomplete in {out_dir}")
    return {"wheel": wheel[-1], "sdist": sdist[-1]}


def dependency_runtime(wheel: Path, work_dir: Path) -> dict:
    """Install the frozen ORX wheel into a throwaway venv and record the
    exact dependency closure it pulls (the 兼容组合's runtime face)."""
    venv_dir = work_dir / "dep-venv"
    created = _run(["uv", "venv", "--python", sys.executable, str(venv_dir)],
                   cwd=work_dir)
    _require(created.returncode == 0, f"uv venv failed: {created.stderr}")
    python = venv_dir / "bin" / "python"
    installed = _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=work_dir)
    _require(installed.returncode == 0,
             f"wheel install failed: {installed.stderr}")
    code = (
        "import json, importlib.metadata as md\n"
        "deps = {}\n"
        "for req in md.requires('orx-agent') or []:\n"
        "    name = req.split(';')[0].split('>=')[0].split('==')[0]\n"
        "    name = name.split('!=')[0].split('<')[0].split('>')[0].strip()\n"
        "    try:\n"
        "        deps[name] = md.version(name)\n"
        "    except md.PackageNotFoundError:\n"
        "        deps[name] = None\n"
        "print(json.dumps({'python': __import__('sys').version.split()[0],\n"
        "                  'orx_version': md.version('orx-agent'),\n"
        "                  'dependencies': deps}))\n"
    )
    probe = _run([str(python), "-c", code], cwd=work_dir)
    _require(probe.returncode == 0,
             f"dependency probe failed: {probe.stderr}")
    return json.loads(probe.stdout)


def locate_app_archive(work_dir: Path, mode: str) -> Path | None:
    dist = work_dir / "dist"
    if not dist.is_dir():
        return None
    suffix = "zip" if mode == "dev" else "dmg"
    archives = sorted(
        p for p in dist.glob(f"ORXAnalytics-*-{mode}-macos13-arm64.{suffix}")
        if p.is_file())
    return archives[-1] if archives else None


def build_app_archive(work_dir: Path, checkout: Path, mode: str,
                      log: StageLog, decision: Path | None = None) -> Path:
    """Run the FROZEN checkout's own build_app.sh (the app's single build
    entry) and pull the produced archive (+ its build record) into the
    work dir.

    ``checkout`` must be a GIT checkout of the frozen commit
    (``clone_commit``): build_app.sh reads the COMPLETE source commit
    from ``git rev-parse HEAD`` and the channel gate refuses a
    truncated/absent commit, so a .git-less archive checkout cannot
    produce a final app. The pinned-runtime cache is pre-seeded from the
    release clone when present (build_app.sh verifies the tarball's
    sha256 on every use either way). The distribution decision gate is
    honored via ORX_DISTRIBUTION_DECISION when a decision file is given
    (mandatory for the official channel)."""
    script = checkout / "scripts" / "build_app.sh"
    _require(script.is_file(), "build_app.sh missing in the frozen checkout")
    # Pre-seed the pinned python-build-standalone tarball (offline
    # friendly; the script still verifies the sha256 on every use).
    cache_src = ANALYTICS_REPO / "var" / "runtime-cache"
    cache_dst = checkout / "var" / "runtime-cache"
    if cache_src.is_dir():
        cache_dst.mkdir(parents=True, exist_ok=True)
        for tarball in cache_src.glob("*.tar.gz"):
            seeded = cache_dst / tarball.name
            if not seeded.exists():
                shutil.copyfile(tarball, seeded)
                log.write(f"runtime cache pre-seeded: {tarball.name}")
    env = dict(os.environ)
    if decision is not None:
        # absolute: build_app.sh resolves the decision path against ITS
        # checkout cwd, not the harness root
        env["ORX_DISTRIBUTION_DECISION"] = str(Path(decision).resolve())
    log.write(f"building app via frozen checkout: scripts/build_app.sh"
              f" {mode}"
              + (f" --decision {decision}" if decision is not None else ""))
    result = _run(["sh", str(script), mode], cwd=checkout, env=env,
                  timeout=_APP_BUILD_TIMEOUT_SEC)
    log.command(["sh", "scripts/build_app.sh", mode], result,
                env_note="frozen git checkout")
    if result.returncode != 0:
        raise StageFailure(
            "build", "app-build-failed",
            "the frozen checkout's build_app.sh failed",
            output_tail=(result.stdout + result.stderr)[-2000:])
    suffix = "zip" if mode == "dev" else "dmg"
    produced = sorted(
        (checkout / "var" / "dist").glob(
            f"ORXAnalytics-*-{mode}-macos13-arm64.{suffix}"))
    _require(produced, "build_app.sh produced no archive")
    archive = produced[-1]
    dest = work_dir / "dist" / archive.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive, dest)
    record = archive.with_name(archive.name + ".json")
    if record.is_file():
        shutil.copyfile(record, work_dir / "dist" / record.name)
    return dest


def signature_evidence(app_dir: Path) -> dict:
    """Record what codesign actually says about the extracted .app (the
    honest signature evidence — ad-hoc today, Developer ID when the
    credentials exist)."""
    verify = _run(["codesign", "--verify", "--strict", str(app_dir)],
                  cwd=app_dir.parent)
    display = _run(["codesign", "-dv", str(app_dir)], cwd=app_dir.parent)
    text = display.stdout + display.stderr
    authority = re.findall(r"Authority=(.+)", text)
    style = re.findall(r"Signature=(\S+)", text)
    return {
        "codesign_verify_exit": verify.returncode,
        "signature_style": style[0] if style else None,
        "authorities": authority,
        "adhoc": bool(style and style[0] == "adhoc") or not authority,
    }


def stage_build(work_dir: Path, log: StageLog, *, build_app: bool = False,
                app_archive: Path | None = None,
                decision: Path | None = None) -> dict:
    manifest = load_manifest(work_dir)
    freeze = manifest.get("stages", {}).get("freeze", {})
    if freeze.get("status") != "passed":
        raise StageFailure("build", "missing-step-evidence",
                           "freeze has not passed; run freeze first")
    binding = manifest["binding"]
    if decision is None:
        # per-goal default (g011: docs/diagnostics/g011/distribution.json;
        # g012: docs/diagnostics/g012/distribution.json) — resolved only
        # when present, exactly like the previous CLI pre-resolution
        default_decision = goal_config(binding.get("goal_id"))["decision_file"]
        if default_decision.is_file():
            decision = default_decision
    orx_commit = binding["orx"]["commit"]
    an_commit = binding["analytics"]["commit"]
    work_dir.mkdir(parents=True, exist_ok=True)

    orx_checkout = archive_commit(REPO_ROOT, orx_commit,
                                  work_dir / "checkouts" / "orx")
    an_checkout = archive_commit(ANALYTICS_REPO, an_commit,
                                 work_dir / "checkouts" / "analytics")
    log.write(f"one-shot checkouts: orx@{orx_commit[:12]}"
              f" analytics@{an_commit[:12]} (git archive; no working tree)")

    wheel = build_orx_wheel(orx_checkout, work_dir / "dist")
    an_dist = build_analytics_dist(an_checkout, work_dir / "dist" / "analytics")
    wheel_version = re.match(r"orx_agent-([0-9][^-]*)-", wheel.name)
    _require(wheel_version and wheel_version.group(1)
             == binding["orx"]["version"],
             f"built wheel {wheel.name} does not match the frozen version"
             f" {binding['orx']['version']}")
    deps = dependency_runtime(wheel, work_dir)
    log.write(f"orx wheel: {wheel.name} sha256 {sha256_file(wheel)}")
    log.write(f"analytics dist: {[p.name for p in an_dist.values()]}")
    log.write(f"dependency runtime: {json.dumps(deps, sort_keys=True)}")

    artifacts = manifest.setdefault("artifacts", {})
    artifacts["orx_wheel"] = {
        "path": f"dist/{wheel.name}", "sha256": sha256_file(wheel),
        "built_from": orx_commit, "version": binding["orx"]["version"],
        "built_from_archive": True,
    }
    artifacts["analytics_wheel"] = {
        "path": f"dist/analytics/{an_dist['wheel'].name}",
        "sha256": sha256_file(an_dist["wheel"]),
        "built_from": an_commit, "version": binding["analytics"]["version"],
        "built_from_archive": True,
    }
    artifacts["analytics_sdist"] = {
        "path": f"dist/analytics/{an_dist['sdist'].name}",
        "sha256": sha256_file(an_dist["sdist"]),
        "built_from": an_commit, "built_from_archive": True,
    }
    manifest.setdefault("binding", {})["runtime"]["orx_dependencies"] = \
        deps["dependencies"]

    mode = CHANNEL_APP_MODE[binding["channel"]]
    if binding["channel"] == "official" and decision is None:
        raise StageFailure(
            "build", "decision-missing",
            "the official channel requires the pre-fixed distribution"
            " decision (--decision / ORX_DISTRIBUTION_DECISION); it is"
            " never built without one")
    archive_path: Path | None = None
    if app_archive is not None:
        if not Path(app_archive).is_file():
            raise StageFailure("build", "app-artifact-missing",
                               f"app archive not found at {app_archive}")
        dest = work_dir / "dist" / Path(app_archive).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app_archive, dest)
        archive_path = dest
        built_by = "bound-from-path"
    elif build_app:
        # A real GIT checkout of the frozen analytics commit: build_app.sh
        # records the COMPLETE source commit and refuses a .git-less tree.
        git_checkout = clone_commit(ANALYTICS_REPO, an_commit,
                                    work_dir / "checkouts" / "analytics-git")
        archive_path = build_app_archive(work_dir, git_checkout, mode, log,
                                         decision=decision)
        built_by = "build_app.sh"
    else:
        archive_path = locate_app_archive(work_dir, mode)
        built_by = "previously-built" if archive_path else None

    if archive_path is None:
        # Fail closed: the final App artifacts are G011 T006's deliverable.
        # A missing app is NEVER a skip.
        record_stage(manifest, "build", "failed", log.rel,
                     code="app-artifact-missing",
                     wheels_bound=True,
                     message=(
                         "final app archive missing: the joint combination"
                         f" ships the {binding['channel']} channel's app"
                         f" ({mode} flavor); it is built by G011 T006 via the"
                         " frozen checkout's scripts/build_app.sh. Rerun"
                         " build with --build-app, or bind an existing"
                         " archive with --app-archive PATH."))
        save_manifest(work_dir, manifest)
        raise StageFailure(
            "build", "app-artifact-missing",
            "final app archive missing (expected under"
            f" {work_dir / 'dist'}; the app is G011 T006's deliverable —"
            " rerun with --build-app or --app-archive)",
            expected_mode=mode)

    artifacts["app_archive"] = {
        "path": f"dist/{archive_path.name}",
        "sha256": sha256_file(archive_path),
        "built_from": an_commit, "built_by": built_by, "mode": mode,
        "channel": binding["channel"],
    }
    # The per-archive build record written by build_app.sh (hash AFTER the
    # channel gates): bind it by digest when it accompanies the archive.
    build_record = archive_path.with_name(archive_path.name + ".json")
    if build_record.is_file():
        record_dest = work_dir / "dist" / build_record.name
        if not record_dest.is_file():
            shutil.copyfile(build_record, record_dest)
        artifacts["app_archive"]["build_record"] = {
            "path": f"dist/{build_record.name}",
            "sha256": sha256_file(record_dest),
        }
    # Signature evidence straight from the artifact (extract now; exercise
    # re-extracts for its own connection work).
    app_dir = extract_app_archive(archive_path, work_dir / "app")
    sig = signature_evidence(app_dir)
    if binding["channel"] == "official":
        if not sig["authorities"] or \
                not any(a.startswith("Developer ID Application") for a in sig["authorities"]):
            record_stage(manifest, "build", "failed", log.rel,
                         code="signature-evidence-missing", signature=sig)
            save_manifest(work_dir, manifest)
            raise StageFailure(
                "build", "signature-evidence-missing",
                "official channel requires a Developer ID Application"
                " signature; the artifact carries none (baseline: no"
                " Developer ID certificate configured)",
                signature=sig)
    artifacts["app_archive"]["signature"] = sig
    record_stage(manifest, "build", "passed", log.rel,
                 artifacts_bound=sorted(artifacts),
                 signature=sig)
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    return manifest


def extract_app_archive(archive: Path, dest: Path) -> Path:
    """Extract a .app from a zip (dev flavor) or DMG (dist flavor)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        # ditto zips carry AppleDouble metadata entries; ditto -x restores
        # them as extended attributes. Plain unzip materializes them as
        # stray ._* files inside the bundle, which breaks the code-sign
        # seal (codesign: "a sealed resource is missing or invalid").
        result = _run(["ditto", "-x", "-k", str(archive), str(dest)],
                      cwd=dest)
        if result.returncode != 0:
            raise JointReleaseError(
                f"ditto -x -k failed on {archive}: {result.stderr}")
    elif archive.suffix == ".dmg":
        mount = dest / "mount"
        mount.mkdir(exist_ok=True)
        attached = _run(["hdiutil", "attach", "-readonly",
                         "-mountpoint", str(mount), str(archive)],
                        cwd=dest)
        if attached.returncode != 0:
            raise JointReleaseError(
                f"hdiutil attach failed: {attached.stderr}")
        try:
            for app in mount.glob("*.app"):
                shutil.copytree(app, dest / app.name)
        finally:
            _run(["hdiutil", "detach", str(mount)], cwd=dest)
    else:
        raise JointReleaseError(f"unknown app archive flavor: {archive}")
    apps = sorted(dest.glob("*.app"))
    _require(apps, f"no .app bundle inside {archive}")
    return apps[0]


# ---------------------------------------------------------------------------
# Exercise: installed-wheel CLI + the final App's built-in engine
# ---------------------------------------------------------------------------


class EngineClient:
    """Newline-JSON client for the engine socket (protocol v1)."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(path))
        self._file = self._sock.makefile("rwb")

    def request(self, payload: dict) -> dict:
        self._file.write(json.dumps(payload).encode() + b"\n")
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("engine closed the connection")
        return json.loads(line)

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()


def install_orx_wheel(wheel: Path, venv_dir: Path) -> Path:
    # The venv pins THIS harness interpreter: a uv-default python may
    # carry a newer SQLite whose last-close WAL-sidecar cleanup leaves
    # quiescent ORX projects without -wal/-shm, which the analytics
    # read-only open (mode=ro) then cannot read at all. The manifest
    # records the interpreter and its SQLite for exactly this reason.
    created = _run(["uv", "venv", "--python", sys.executable, str(venv_dir)],
                   cwd=venv_dir.parent if venv_dir.parent.exists()
                   else Path("."))
    _require(created.returncode == 0, f"uv venv failed: {created.stderr}")
    python = venv_dir / "bin" / "python"
    installed = _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=venv_dir.parent)
    _require(installed.returncode == 0,
             f"orx wheel install failed: {installed.stderr}")
    return python


EXERCISE_PLAN_TASKS = 2


def _exercise_plan_ir(goal_id: str) -> dict:
    return {
        "goal": goal_id,
        "exploration": {
            "summary": "joint acceptance stand-in exploration",
            "relevant_components": ["src/"],
            "unknowns": [], "assumptions": [], "risks": [],
        },
        "approach": {"summary": "two-task joint acceptance plan",
                     "decisions": []},
        "tasks": [
            {
                "id": "T001",
                "objective": "deliver the joint acceptance marker with"
                             " structured evidence",
                "dependencies": [],
                "scope": {"allowed": ["src/"]},
                "acceptance": ["the joint marker exists",
                               "the joint summary is verified"],
                "verification": ["test -f joint-marker.txt"],
                "routing": {"complexity": "medium",
                            "required_capabilities": ["coding"]},
                "preread": [],
            },
            {
                "id": "T002",
                "objective": "close the joint loop with an independent"
                             " verdict",
                "dependencies": ["T001"],
                "scope": {"allowed": ["src/"]},
                "acceptance": ["the joint summary is verified"],
                "verification": ["test -f joint-summary.txt",
                                 "agent: joint acceptance review"],
                "routing": {"complexity": "medium",
                            "required_capabilities": ["coding"]},
                "preread": [],
            },
        ],
    }


def _evidence_json(marker: str, log_rel: str) -> str:
    return json.dumps({
        "status": "passed",
        "summary": f"joint acceptance stand-in delivery ({marker})",
        "checks": [
            {"command": f"test -f {marker}", "exit_code": 0, "log": log_rel}
        ],
        "artifacts": [marker],
    })


def engine_snapshot_count(analytics_db: Path, project_key: str) -> int:
    if not analytics_db.is_file():
        return 0
    conn = sqlite3.connect(f"file:{analytics_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM orx_snapshots WHERE project_key = ?",
            (project_key,)).fetchone()
        return row[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def launch_bundle_engine(app_dir: Path, data_dir: Path, log: StageLog,
                         env_extra: dict | None = None) -> dict:
    """Launch the app's OWN engine serve entry (the wrapper the app
    launches) on the bundled runtime, on an isolated data dir, and wait
    for the socket. Notifications off; nothing real is reachable. Engine
    stdout/stderr go to files under the work dir (a pipe would fill or
    die with the launching process)."""
    resources = app_dir / "Contents" / "Resources"
    python = resources / "python" / "bin" / "python3"
    serve = resources / "orx_engine_serve.py"
    package = resources / "orxanalytics"
    version_file = resources / "orxanalytics-version.txt"
    for required, what in ((python, "bundled python runtime"),
                           (serve, "the app's engine serve entry"),
                           (package, "the bundled orxanalytics package")):
        if not required.exists():
            raise StageFailure(
                "exercise", "app-bundle-incomplete",
                f"{what} missing inside the app bundle ({required}) —"
                " not a final-app delivery")
    data_dir.mkdir(parents=True, exist_ok=True)
    # The engine's socket is <data_dir>/engine.sock; AF_UNIX paths are
    # capped (~104 bytes on macOS), and deeply nested work dirs (pytest
    # tmp trees) would break the bind. Route to a short temp dir when
    # needed; stop_bundle_engine removes it.
    cleanup_dir: Path | None = None
    orig_data_dir = data_dir
    if len(str(data_dir / "engine.sock")) > 100:
        import tempfile
        data_dir = Path(tempfile.mkdtemp(prefix="jr-engine-"))
        cleanup_dir = data_dir
        log.write(f"engine data dir rerouted to a short path: {data_dir}")
    lock = data_dir / "engine.lock"
    if lock.is_file():
        try:
            holder = json.loads(lock.read_text())
        except (OSError, json.JSONDecodeError):
            holder = {}
        pid = holder.get("pid")
        still_running = False
        if pid:
            probe = _run(["ps", "-p", str(pid)], cwd=data_dir)
            still_running = probe.returncode == 0
        if still_running:
            raise StageFailure(
                "exercise", "engine-already-running",
                f"another engine (pid {pid}) already holds {lock}; a"
                " stale engine from an earlier run must be stopped"
                " before the joint exercise can own its data dir")
    env = isolated_env(data_dir.parent / "engine-home")
    env["ORX_ANALYTICS_HOME"] = str(data_dir)
    if env_extra:
        env.update(env_extra)
    out_fh = open(data_dir.parent / "engine.stdout.log", "w")
    err_fh = open(data_dir.parent / "engine.stderr.log", "w")
    proc = subprocess.Popen(
        [str(python), str(serve), str(data_dir)],
        cwd=data_dir.parent, env=env, stdout=out_fh, stderr=err_fh,
    )
    sock = data_dir / "engine.sock"
    try:
        deadline = time.monotonic() + _ENGINE_START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if sock.exists():
                try:
                    client = EngineClient(sock)
                    hello = client.request({"op": "overview",
                                            "protocol_version": 1})
                    client.close()
                    handle = {"proc": proc, "socket": sock, "hello": hello,
                            "stdout": out_fh, "stderr": err_fh,
                            "data_dir": data_dir, "cleanup_dir": cleanup_dir,
                            "orig_data_dir": orig_data_dir,
                            "bundled_version":
                                version_file.read_text().strip()
                                if version_file.exists() else None}
                    _ACTIVE_ENGINES.append(handle)
                    return handle
                except (ConnectionError, OSError):
                    pass
            if proc.poll() is not None:
                out_fh.flush(); err_fh.flush()
                tail = (data_dir.parent / "engine.stdout.log").read_text()[
                    -2000:] + (data_dir.parent / "engine.stderr.log").read_text()[-2000:]
                raise StageFailure(
                    "exercise", "engine-start-failed",
                    "the app's bundled engine exited before serving",
                    output_tail=tail)
            time.sleep(0.2)
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        out_fh.close()
        err_fh.close()
        raise
    proc.terminate()
    out_fh.close()
    err_fh.close()
    raise StageFailure("exercise", "engine-timeout",
                       "the bundled engine did not bind its socket in time")


def stop_bundle_engine(handle: dict) -> None:
    proc = handle.get("proc")
    for fh in (handle.get("stdout"), handle.get("stderr")):
        try:
            if fh and not fh.closed:
                fh.close()
        except OSError:
            pass
    try:
        _ACTIVE_ENGINES.remove(handle)
    except ValueError:
        pass
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    cleanup_dir = handle.get("cleanup_dir")
    if cleanup_dir:
        # preserve the evidence (analytics.db, engine state) in the
        # work-dir location the caller chose, then drop the short path
        orig = handle.get("orig_data_dir")
        if orig is not None:
            orig.mkdir(parents=True, exist_ok=True)
            for name in ("analytics.db", "engine.state.json"):
                src = cleanup_dir / name
                if src.is_file():
                    shutil.copyfile(src, orig / name)
        shutil.rmtree(cleanup_dir, ignore_errors=True)


# Every engine this process launched (deterministic cleanup even when a
# stage fails mid-lifecycle; an orphaned engine would hold the data-dir
# lock and poison the next run).
_ACTIVE_ENGINES: list[dict] = []


def stop_all_engines() -> None:
    while _ACTIVE_ENGINES:
        stop_bundle_engine(_ACTIVE_ENGINES[-1])


def bundle_engine_refresh(handle: dict, analytics_db: Path,
                          project_key: str, expect_snapshots: int,
                          reason: str = "panel-open") -> dict:
    """Send one refresh.request through the socket and wait until the
    project's snapshot count reaches ``expect_snapshots`` (the engine's
    coalesced collect actually ran)."""
    client = EngineClient(handle["socket"])
    response = client.request({"op": "refresh.request",
                               "protocol_version": 1,
                               "reason": reason,
                               "projects": [project_key]})
    client.close()
    deadline = time.monotonic() + _ENGINE_REFRESH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if engine_snapshot_count(analytics_db, project_key) >= expect_snapshots:
            return {"refresh": response, "snapshots": expect_snapshots}
        time.sleep(0.3)
    raise StageFailure(
        "exercise", "engine-refresh-stalled",
        f"engine did not produce snapshot #{expect_snapshots} for the"
        " isolated project in time",
        refresh=response)


def exercise_driver(work_dir: Path, manifest: dict, log: StageLog,
                    *, app_dir: Path, stand_in: bool) -> dict:
    """The full joint exercise: installed-wheel CLI lifecycle on an
    isolated project + the app-bundle engine connection. ``stand_in``
    only changes HONESTY LABELING (the record says so); the machinery
    run is identical."""
    binding = manifest["binding"]
    wheel_rel = manifest["artifacts"]["orx_wheel"]["path"]
    wheel = work_dir / wheel_rel
    _require(wheel.is_file(), f"bound orx wheel missing: {wheel}")

    python = install_orx_wheel(wheel, work_dir / "exercise-venv")
    orx_bin = python.parent / "orx"
    _require(orx_bin.exists(), "orx console script missing in the venv")

    home = work_dir / "exercise-home"
    env = isolated_env(home)
    proj = (work_dir / "exercise-project").resolve()
    proj.mkdir(parents=True, exist_ok=True)
    # the analytics registry keys projects by the RESOLVED path (on macOS
    # /tmp -> /private/tmp); talk to the engine with the same key.
    project_key = str(proj)
    facts: dict = {"stand_in_app_delivery": stand_in, "project": "isolated"}

    def cli(label: str, args: list[str], expect: int = 0) -> dict:
        proc = _run([str(orx_bin), *args], cwd=proj, env=env)
        log.command([str(orx_bin), *args], proc)
        step = {"step": label, "command": "orx " + " ".join(args),
                "exit_code": proc.returncode}
        if "--json" in args:
            try:
                step["envelope"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                step["envelope"] = None
                step["stdout"] = proc.stdout[-1500:]
        else:
            step["stdout"] = proc.stdout[-1500:]
        if proc.returncode != expect:
            raise StageFailure(
                "exercise", "cli-step-failed",
                f"installed CLI step {label!r} exited {proc.returncode}"
                f" (expected {expect})", step=step)
        return step

    def envelope(step: dict) -> dict:
        env_ = step.get("envelope")
        _require(isinstance(env_, dict),
                 f"step {step['step']} produced no JSON envelope")
        return env_

    installed_version = envelope(cli(
        "version", ["version", "--json"]))["version"]

    # -- goal 1 up to a live claimed executor, then the engine connects ---
    cli("init", ["init", ".", "--json"])
    state_db = proj / ".orx" / "state.db"
    goal1 = envelope(cli("goal-1-new", [
        "goal", "new", "--json",
        "--objective", "Joint acceptance: lifecycle layering with the"
                       " installed wheel and the app engine",
        "--acceptance", "the joint marker exists",
        "--acceptance", "the joint summary is verified",
        "--constraint", "no paid model, no analytics watch: host work"
                        " stays parked; engine reads read-only",
    ]))
    goal_id = goal1["goal"]["id"]
    (proj / "plan-ir.json").write_text(json.dumps(_exercise_plan_ir(goal_id)))
    cli("plan-submit", ["plan", "submit", "--file", "plan-ir.json", "--json"])
    parked = envelope(cli("run-park-t001", ["run", "--json"]))
    _require(any(e.get("task") == "T001"
                 for e in parked.get("host_required", [])),
             "run did not park T001 as host work")
    claim = envelope(cli("claim-t001",
                         ["task", "claim", "T001", "--json",
                          "--session", "sess-joint-exercise"]))
    attempt = claim["attempt"]

    # the FINAL APP's built-in engine, connected for the WHOLE lifecycle:
    # the app's own serve entry on the bundled runtime, isolated data
    # dir, notifications off, only this project registered.
    engine_data = work_dir / "engine-data"
    handle = launch_bundle_engine(app_dir, engine_data, log)
    analytics_db = handle["data_dir"] / "analytics.db"
    # post-stop evidence location (identical unless the socket path had
    # to be rerouted to a short temp dir)
    evidence_db = (handle.get("orig_data_dir") or handle["data_dir"]) \
        / "analytics.db"
    register_env = isolated_env(work_dir / "engine-home")
    # the app runs bundle processes with PYTHONPATH=<Resources>; the
    # bundled package lives beside the engine serve entry.
    register_env["PYTHONPATH"] = str(app_dir / "Contents" / "Resources")
    register = _run(
        [str(app_dir / "Contents" / "Resources" / "python" / "bin" / "python3"),
         "-m", "orxanalytics", "--data-dir", str(handle["data_dir"]),
         "projects", "add", project_key, "--name", "joint-exercise"],
        cwd=work_dir, env=register_env)
    log.command(["<bundled python>", "-m", "orxanalytics", "projects", "add",
                 "<isolated project>"], register)
    _require(register.returncode == 0,
             f"project registration via the bundled runtime failed:"
             f" {register.stderr}")

    # -- pause: gated dispatch, live facts still land ----------------------
    paused = envelope(cli("goal-pause", ["goal", "pause", "--json"]))
    _require(paused["status"] == "paused" and paused["changed"] is True,
             f"pause envelope unexpected: {paused}")
    gated = cli("run-gated-while-paused", ["run", "--json"], expect=1)
    gate_env = envelope(gated)
    _require(gate_env.get("ok") is False and "paused" in json.dumps(gate_env),
             f"pause gate envelope unexpected: {gate_env}")
    beat = envelope(cli("heartbeat-while-paused", [
        "task", "heartbeat", "T001", "--attempt", str(attempt),
        "--phase", "implementing", "--message", "still lands while paused",
        "--json"]))
    _require(beat["sequence"] >= 1, "heartbeat refused during pause")

    # completion DURING the pause: the running executor's late fact lands
    (proj / "joint-marker.txt").write_text("joint acceptance marker\n")
    log_dir = proj / ".orx" / "runs" / "joint" / "check" / "T001"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "01-command.log").write_text("test -f joint-marker.txt\n")
    (proj / "evidence-1.json").write_text(_evidence_json(
        "joint-marker.txt", ".orx/runs/joint/check/T001/01-command.log"))
    completed = envelope(cli(
        "complete-t001-during-pause",
        ["task", "complete", "T001", "--evidence", "evidence-1.json",
         "--attempt", str(attempt), "--json"]))
    _require(completed["status"] == "passed",
             f"completion during pause failed: {completed}")
    status_paused = envelope(cli("status-while-paused", ["status", "--json"]))
    _require(status_paused["goal"]["status"] == "paused",
             "goal left paused after the late completion")
    task_states = {t["id"]: t["status"] for t in status_paused["tasks"]}
    _require(task_states.get("T001") == "passed",
             f"T001 not passed during pause: {task_states}")

    # collect #1 WHILE PAUSED: the engine must show the layered truth —
    # run still executing (active bucket), Goal control state paused.
    # The read-only assertion is scoped to the ENGINE's read windows:
    # the ORX CLI legitimately keeps writing between them.
    sha_before_collect_1 = sha256_file(state_db)
    files_before_collect_1 = sorted(
        p.name for p in (proj / ".orx").iterdir()
        if not re.match(r"state\.db-(wal|shm|journal)", p.name))
    bundle_engine_refresh(handle, analytics_db, project_key, 1)
    overview_paused = EngineClient(handle["socket"]).request(
        {"op": "overview", "protocol_version": 1})
    _require(
        sha256_file(state_db) == sha_before_collect_1
        and sorted(p.name for p in (proj / ".orx").iterdir()
                   if not re.match(r"state\.db-(wal|shm|journal)", p.name))
        == files_before_collect_1,
        "collect #1 (while paused) modified the ORX project source")

    def runs_in(payload: dict, *buckets: str) -> list[dict]:
        return [
            item for bucket in buckets for item in payload.get(bucket, [])
            if item.get("project") == project_key
        ]

    def goal_raw(item: dict) -> str | None:
        status = (item.get("goal") or {}).get("status")
        return status.get("raw") if isinstance(status, dict) else status

    active_paused = runs_in(overview_paused, "active")
    _require(active_paused, "engine saw no active run while the goal was"
             " paused (collect #1)")
    _require(any(goal_raw(i) == "paused" for i in active_paused),
             f"paused control state not mirrored verbatim in the active"
             f" bucket: {[(i.get('run_id'), goal_raw(i)) for i in active_paused]}")
    _require(all(goal_raw(i) != "done" for i in active_paused),
             "a paused goal masqueraded as done in the engine's active view")

    # receipt re-transmission: the SAME delivery, a fresh process — pure
    # read replay of the stored receipt, zero database change
    sha_before_replay = sha256_file(state_db)
    replay = envelope(cli(
        "receipt-retransmit",
        ["task", "complete", "T001", "--evidence", "evidence-1.json",
         "--attempt", str(attempt), "--json"]))
    replay_ok = (
        replay.get("status") == "passed"
        and sha256_file(state_db) == sha_before_replay
    )
    _require(replay_ok,
             f"receipt re-transmission was not a pure replay: {replay}")
    conn = _connect_immutable(state_db)
    receipts = conn.execute(
        "SELECT COUNT(*) FROM completion_receipts").fetchone()[0]
    conn.close()
    _require(receipts == 1,
             f"expected exactly one completion receipt, found {receipts}")

    # -- resume → T002 → independent verdict → goal 1 done ----------------
    resumed = envelope(cli("goal-resume", ["goal", "resume", "--json"]))
    _require(resumed["changed"] is True, "resume did not change anything")
    cli("run-park-t002", ["run", "--json"])
    claim2 = envelope(cli("claim-t002",
                          ["task", "claim", "T002", "--json",
                           "--session", "sess-joint-exercise-2"]))
    (proj / "joint-summary.txt").write_text("joint acceptance summary\n")
    log2 = proj / ".orx" / "runs" / "joint" / "check" / "T002"
    log2.mkdir(parents=True, exist_ok=True)
    (log2 / "01-command.log").write_text("test -f joint-summary.txt\n")
    (proj / "evidence-2.json").write_text(_evidence_json(
        "joint-summary.txt", ".orx/runs/joint/check/T002/01-command.log"))
    envelope(cli("complete-t002",
                 ["task", "complete", "T002", "--evidence", "evidence-2.json",
                  "--attempt", str(claim2["attempt"]), "--json"]))
    verify_out = envelope(cli("verify-dispatch", ["verify", "--json"]))
    agent_entries = [e.get("entry") for e in verify_out.get("agent_required", [])]
    _require("agent: joint acceptance review" in agent_entries,
             f"verify did not report the agent entry: {agent_entries}")
    verdict = envelope(cli(
        "verify-submit", ["verify", "submit", "T002", "--result", "pass",
                          "--entry", "agent: joint acceptance review",
                          "--json"]))
    _require(verdict["status"] == "passed", f"verdict failed: {verdict}")
    final1 = envelope(cli("status-goal-1", ["status", "--json"]))
    _require(final1["goal"]["status"] == "done"
             and final1["run"]["status"] == "done",
             f"goal 1 did not close: {final1['goal']} {final1['run']}")
    facts["goal_1"] = {
        "paused_then_resumed": True,
        "completion_during_pause": True,
        "engine_view_while_paused": {
            "run_active": True,
            "goal_control_raw": "paused",
        },
        "receipt_retransmit_zero_change": True,
        "receipts": receipts,
        "final": {"goal": "done", "run": "done"},
    }

    # -- goal 2: cancel with a live executor, converge, stay queryable ---
    goal2 = envelope(cli("goal-2-new", [
        "goal", "new", "--json",
        "--objective", "Joint acceptance: cancellation with a live executor",
        "--acceptance", "the cancel branch converges",
        "--constraint", "no paid model",
    ]))
    (proj / "plan-2.json").write_text(json.dumps({
        "goal": goal2["goal"]["id"],
        "exploration": {"summary": "cancel branch", "relevant_components": [],
                        "unknowns": [], "assumptions": [], "risks": []},
        "approach": {"summary": "one-task cancel branch", "decisions": []},
        "tasks": [{
            "id": "T001", "objective": "run until cancelled",
            "dependencies": [], "scope": {"allowed": ["src/"]},
            "acceptance": ["the cancel branch converges"],
            "verification": ["true"],
            "routing": {"complexity": "medium",
                        "required_capabilities": ["coding"]},
            "preread": [],
        }],
    }))
    cli("plan-2-submit", ["plan", "submit", "--file", "plan-2.json", "--json"])
    cli("run-2-park", ["run", "--json"])
    envelope(cli("claim-2-t001",
                 ["task", "claim", "T001", "--json",
                  "--session", "sess-joint-cancel"]))
    cancel = envelope(cli("goal-cancel", ["goal", "cancel", "--json"]))
    _require(cancel["status"] == "cancelling",
             f"cancel envelope unexpected: {cancel}")
    _require(cancel.get("unconfirmed"),
             "cancel did not list the live executor as unconfirmed")
    envelope(cli("recover-terminated-executor", [
        "task", "recover", "T001", "--termination-fact",
        "joint acceptance: executor session ended (operator-confirmed)",
        "--json"]))
    converged = envelope(cli("goal-cancel-converge",
                             ["goal", "cancel", "--json"]))
    _require(converged["status"] == "cancelled",
             f"goal 2 did not converge to cancelled: {converged}")
    usage = envelope(cli("usage-still-queryable", ["usage", "--json"]))
    _require(usage.get("ok") is True, "usage not queryable after cancel")
    facts["goal_2"] = {
        "cancelling_then_cancelled": True,
        "unconfirmed_executor_listed": True,
        "recovered_via_termination_fact": True,
        "history_queryable": True,
    }
    facts["installed_orx_version"] = installed_version

    # -- collect #2 after everything: repeated collection is idempotent,
    #    the done run aggregates, the cancelled goal never does ----------
    sha_before_collect_2 = sha256_file(state_db)
    files_before_collect_2 = sorted(
        p.name for p in (proj / ".orx").iterdir()
        if not re.match(r"state\.db-(wal|shm|journal)", p.name))
    bundle_engine_refresh(handle, analytics_db, project_key, 2,
                          reason="background")
    overview_final = EngineClient(handle["socket"]).request(
        {"op": "overview", "protocol_version": 1})
    stop_bundle_engine(handle)

    recent_ours = runs_in(overview_final, "recent_completed")
    _require(any(goal_raw(i) == "done" for i in recent_ours),
             f"engine did not aggregate the completed run:"
             f" {[(i.get('run_id'), goal_raw(i)) for i in recent_ours]}")
    _require(all(goal_raw(i) != "cancelled" for i in recent_ours),
             "a cancelled goal aggregated into recent completions")
    conn = sqlite3.connect(f"file:{evidence_db}?mode=ro", uri=True)
    try:
        non_orx = conn.execute(
            "SELECT COUNT(*) FROM zc_sessions").fetchone()[0] \
            + conn.execute("SELECT COUNT(*) FROM cx_usage").fetchone()[0] \
            + conn.execute("SELECT COUNT(*) FROM cur_composers").fetchone()[0]
        snapshots = conn.execute(
            "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
        goals = {row[0] for row in conn.execute(
            "SELECT DISTINCT status FROM orx_goals")}
    finally:
        conn.close()
    _require(non_orx == 0,
             "engine mirrored real-session (zcode/codex/cursor) sources —"
             " isolation broken")
    _require(snapshots == 2,
             f"expected exactly 2 snapshots after repeated collection,"
             f" found {snapshots}")
    _require({"paused", "done", "cancelled"} <= goals,
             f"engine never mirrored the exercised goal states verbatim:"
             f" {goals}")
    read_only_ok = (
        sha256_file(state_db) == sha_before_collect_2
        and sorted(p.name for p in (proj / ".orx").iterdir()
                   if not re.match(r"state\.db-(wal|shm|journal)", p.name))
        == files_before_collect_2
    )
    _require(read_only_ok,
             "the engine's collection modified the ORX project source"
             " (state.db bytes or the non-sidecar file set changed across"
             " collect #2)")
    facts["engine"] = {
        "connected_via": "app engine serve entry (socket protocol v1)",
        "bundled_analytics_version": handle.get("bundled_version"),
        "collects": 2,
        "snapshots": snapshots,
        "read_only_source": read_only_ok,
        "no_real_session_mirrors": non_orx == 0,
        "cancelled_goal_not_aggregated": True,
    }
    return facts


def assemble_stand_in_app(work_dir: Path, analytics_checkout: Path,
                          version: str) -> Path:
    """A STAND-IN app delivery with the bundle's exact engine layout:
    the frozen analytics package + the app's own orx_engine_serve.py side
    by side, and python/bin/python3 as a wrapper to the dev interpreter.
    Clearly NOT a product delivery: no Swift app, no bundled runtime,
    unsigned. Used by self-test only; recorded as stand_in in evidence."""
    bundle = work_dir / "stand-in-app" / "ORX Analytics (StandIn).app"
    resources = bundle / "Contents" / "Resources"
    if bundle.exists():
        shutil.rmtree(bundle)
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copytree(analytics_checkout / "src" / "orxanalytics",
                    resources / "orxanalytics",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(
        analytics_checkout / "apps" / "macos" / "Resources"
        / "orx_engine_serve.py",
        resources / "orx_engine_serve.py")
    (resources / "orxanalytics-version.txt").write_text(version + "\n")
    python_dir = resources / "python" / "bin"
    python_dir.mkdir(parents=True, exist_ok=True)
    wrapper = python_dir / "python3"
    wrapper.write_text(
        "#!/bin/sh\n# stand-in runtime wrapper (dev interpreter, not the"
        " pinned bundled runtime)\nexec "
        f"{sys.executable} \"$@\"\n")
    wrapper.chmod(0o755)
    return bundle


def stage_exercise(work_dir: Path, log: StageLog, *,
                   stand_in_app_dir: Path | None = None) -> dict:
    manifest = load_manifest(work_dir)
    stages = manifest.get("stages", {})
    for required in ("freeze", "build"):
        record = stages.get(required)
        if not record or record.get("status") != "passed":
            raise StageFailure(
                "exercise", "missing-step-evidence",
                f"stage {required!r} has not passed (record:"
                f" {record}); the joint exercise needs the frozen, built"
                " combination first")
    binding = manifest["binding"]
    app_record = manifest.get("artifacts", {}).get("app_archive")
    if stand_in_app_dir is not None:
        app_dir = Path(stand_in_app_dir)
        _require(app_dir.is_dir(), f"stand-in app dir missing: {app_dir}")
        stand_in = True
    else:
        if not app_record:
            raise StageFailure(
                "exercise", "app-artifact-missing",
                "no final app artifact is bound in the manifest — the"
                " exercise drives the FINAL APP's built-in engine, and"
                " without the app artifact (G011 T006) there is nothing"
                " to connect to; this is a failure, never a skip")
        archive = work_dir / app_record["path"]
        if not archive.is_file():
            raise StageFailure(
                "exercise", "app-artifact-missing",
                f"bound app artifact missing on disk: {archive}")
        app_dir = extract_app_archive(archive, work_dir / "app")
        stand_in = False

    facts = exercise_driver(work_dir, manifest, log, app_dir=app_dir,
                            stand_in=stand_in)
    record_stage(manifest, "exercise", "passed", log.rel,
                 **facts, goal_control_layering=True)
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Stage: verify (fail-closed; nothing is ever skipped-and-passed)
# ---------------------------------------------------------------------------


def verify_artifacts(work_dir: Path, manifest: dict) -> None:
    artifacts = manifest.get("artifacts", {})
    _require(artifacts, "no artifacts bound in the manifest")
    for name, row in sorted(artifacts.items()):
        if not isinstance(row, dict) or not row.get("sha256"):
            raise StageFailure("verify", "artifact-record-invalid",
                               f"artifact {name} has no digest record")
        path = work_dir / row["path"]
        if not path.is_file():
            raise StageFailure("verify", "artifact-missing",
                               f"artifact {name} not found at {row['path']}")
        digest = sha256_file(path)
        if digest != row["sha256"]:
            raise StageFailure(
                "verify", "artifact-hash-mismatch",
                f"artifact {name} changed on disk: recorded"
                f" {row['sha256'][:16]}…, found {digest[:16]}…")
    binding = manifest["binding"]
    wheel_name = Path(artifacts["orx_wheel"]["path"]).name
    m = re.match(r"orx_agent-([0-9][^-]*)-", wheel_name)
    if not m or m.group(1) != binding["orx"]["version"]:
        raise StageFailure(
            "verify", "artifact-identity-mismatch",
            f"orx wheel {wheel_name} does not carry the frozen version"
            f" {binding['orx']['version']} (commit mismatch or rolled-back"
            " source)")
    if artifacts["orx_wheel"]["built_from"] != binding["orx"]["commit"]:
        raise StageFailure(
            "verify", "commit-mismatch",
            "orx wheel was not built from the frozen ORX commit")
    if artifacts["analytics_wheel"]["built_from"] \
            != binding["analytics"]["commit"]:
        raise StageFailure(
            "verify", "commit-mismatch",
            "analytics dist was not built from the frozen analytics commit")
    app = artifacts.get("app_archive")
    if not app:
        raise StageFailure("verify", "app-evidence-missing",
                           "no final app artifact is bound — the joint"
                           " combination ships the App; absence fails")
    if app.get("built_from") != binding["analytics"]["commit"]:
        raise StageFailure("verify", "commit-mismatch",
                           "app artifact not bound to the frozen analytics"
                           " commit")
    if app.get("mode") != CHANNEL_APP_MODE[binding["channel"]]:
        raise StageFailure(
            "verify", "channel-artifact-mismatch",
            f"channel {binding['channel']!r} requires the"
            f" {CHANNEL_APP_MODE[binding['channel']]} flavor app artifact,"
            f" found mode {app.get('mode')!r}")


def stage_verify(work_dir: Path, log: StageLog) -> dict:
    manifest = load_manifest(work_dir)
    binding = manifest.get("binding") or {}
    stages = manifest.get("stages", {})

    for stage in ("freeze", "battery", "build", "exercise"):
        record = stages.get(stage)
        if record is None:
            raise StageFailure(
                "verify", "missing-step-evidence",
                f"stage {stage!r} has no record at all — joint acceptance"
                " cannot pass on missing steps")
        if record.get("status") == "skipped":
            raise StageFailure(
                "verify", "skipped-not-accepted",
                f"stage {stage!r} is marked skipped; a skipped stage is a"
                " failure, never a pass")
        if record.get("status") != "passed":
            raise StageFailure(
                "verify", "missing-step-evidence",
                f"stage {stage!r} status is {record.get('status')!r},"
                " not passed")
        log_path = work_dir / record.get("log", "")
        if not log_path.is_file():
            raise StageFailure(
                "verify", "missing-step-evidence",
                f"stage {stage!r} references log {record.get('log')!r}"
                " which does not exist")

    verify_artifacts(work_dir, manifest)
    log.write("artifacts re-digested and identity-checked against the"
              " frozen binding")

    _, problems = verify_samples(REPO_ROOT, ANALYTICS_REPO,
                                 workroot=work_dir / "verify-samples")
    if problems:
        raise StageFailure("verify", "samples-invalid",
                           "sample provenance failed at verify time",
                           problems=problems)
    log.write("samples re-verified")

    exercise = stages.get("exercise", {})
    engine = exercise.get("engine", {})
    if not engine.get("connected_via"):
        raise StageFailure(
            "verify", "app-evidence-missing",
            "exercise record carries no engine connection evidence")
    if not engine.get("read_only_source"):
        raise StageFailure("verify", "readonly-evidence-missing",
                           "read-only collection evidence missing")

    # GUI evidence: produced by the OPERATOR during the real acceptance
    # (the automated harness must never launch the GUI or pop
    # notifications). Absence fails — never skipped.
    gui_path = work_dir / "gui-evidence.json"
    if not gui_path.is_file():
        raise StageFailure(
            "verify", "gui-evidence-missing",
            f"no GUI evidence at {gui_path.relative_to(REPO_ROOT) if gui_path.is_relative_to(REPO_ROOT) else gui_path}:"
            " the joint acceptance requires an operator-recorded GUI"
            " check (screenshot/launch record); the harness cannot and"
            " must not fabricate or skip it")
    gui = json.loads(gui_path.read_text())
    for field in ("operator", "artifacts", "observed_at"):
        if not gui.get(field):
            raise StageFailure("verify", "gui-evidence-missing",
                               f"GUI evidence field {field!r} missing")
    for ref in gui["artifacts"]:
        ref_path = work_dir / ref if not Path(ref).is_absolute() else Path(ref)
        if not ref_path.exists():
            raise StageFailure("verify", "gui-evidence-missing",
                               f"GUI evidence references missing file {ref}")
    log.write(f"GUI evidence recorded by {gui['operator']}")

    # Signature evidence: recorded from the artifact itself.
    sig = manifest.get("artifacts", {}).get("app_archive", {}).get("signature")
    if not sig:
        raise StageFailure("verify", "signature-evidence-missing",
                           "no signature evidence recorded for the app"
                           " artifact")
    if binding.get("channel") == "official":
        if not sig.get("codesign_verify_exit") == 0 or not any(
                a.startswith("Developer ID Application")
                for a in sig.get("authorities", [])):
            raise StageFailure(
                "verify", "signature-evidence-missing",
                "official channel requires a verified Developer ID"
                " Application signature (notarization evidence on top)")
    else:
        if sig.get("codesign_verify_exit") != 0:
            raise StageFailure(
                "verify", "signature-evidence-missing",
                "internal preview still requires a VERIFIABLE signature"
                " (ad-hoc at minimum; baseline records no Developer ID)")
    log.write(f"signature evidence: style={sig.get('signature_style')}"
              f" authorities={sig.get('authorities')}")

    record_stage(manifest, "verify", "passed", log.rel,
                 gui_evidence={"operator": gui["operator"],
                               "observed_at": gui["observed_at"]},
                 signature=sig, skipped_stages=[])
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    report = work_dir / "verify-report.json"
    report.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# ---------------------------------------------------------------------------
# Stage: candidate — the frozen binding manifest (G011 T006)
# ---------------------------------------------------------------------------


def wheel_metadata(wheel: Path) -> dict:
    """Name/Version from a wheel's *.dist-info/METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        entry = next(name for name in archive.namelist()
                     if name.endswith(".dist-info/METADATA"))
        message = Parser().parsestr(archive.read(entry).decode("utf-8"))
    return {"Name": message["Name"], "Version": message["Version"]}


def sdist_metadata(sdist: Path) -> dict:
    """Name/Version from an sdist's top-level PKG-INFO."""
    with tarfile.open(sdist, "r:gz") as archive:
        entry = next(name for name in archive.getnames()
                     if name.endswith("/PKG-INFO")
                     and name.count("/") == 1)
        message = Parser().parsestr(
            archive.extractfile(entry).read().decode("utf-8"))
    return {"Name": message["Name"], "Version": message["Version"]}


def gather_app_facts(app_dir: Path) -> dict:
    """The app bundle's self-describing facts: Info.plist channel keys,
    the in-app distribution manifest, the packaged analytics version
    file, and the bundled interpreter actually running."""
    resources = app_dir / "Contents" / "Resources"
    info = plistlib.loads((app_dir / "Contents" / "Info.plist").read_bytes())
    in_app = json.loads(
        (resources / "distribution.json").read_text(encoding="utf-8"))
    version_file = resources / "orxanalytics-version.txt"
    python = resources / "python" / "bin" / "python3"
    # -B + PYTHONDONTWRITEBYTECODE: probing the bundled interpreter must
    # never drop __pycache__ into the extracted bundle (it would break
    # the signature seal checked right after)
    probe_env = dict(os.environ)
    probe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = _run(
        [str(python), "-B", "-c",
         "import sys; print(sys.version.split()[0])"],
        cwd=app_dir.parent, env=probe_env, timeout=60)
    return {
        "app_name": app_dir.name,
        "bundle_id": info.get("CFBundleIdentifier"),
        "cf_bundle_version_string": info.get("CFBundleShortVersionString"),
        "cf_bundle_version": info.get("CFBundleVersion"),
        "build_flavor": info.get("ORXAnalyticsBuildFlavor"),
        "channel": info.get("ORXAnalyticsChannel"),
        "audience": info.get("ORXAnalyticsChannelAudience"),
        "source_commit": info.get("ORXAnalyticsSourceCommit"),
        "bundled_python_name": info.get("ORXAnalyticsBundledPython"),
        "in_app_manifest": in_app,
        "orxanalytics_version_txt": version_file.read_text().strip()
        if version_file.is_file() else None,
        "bundled_python_version": probe.stdout.strip()
        if probe.returncode == 0 else None,
        "bundled_python_exit": probe.returncode,
    }


def app_fact_problems(facts: dict, binding: dict) -> list[str]:
    """App-vs-binding consistency (acceptance: package metadata, app
    version, source commit, bundled runtime and signing channel must
    agree with the frozen binding)."""
    analytics = binding["analytics"]
    channel = binding["channel"]
    mode = CHANNEL_APP_MODE[channel]
    pinned = analytics.get("app_pinned_runtime", {})
    expected_python = pinned.get("pbs_python", "").replace("cpython-", "")
    problems: list[str] = []
    if facts.get("cf_bundle_version_string") != analytics["version"]:
        problems.append(
            f"CFBundleShortVersionString {facts.get('cf_bundle_version_string')!r}"
            f" != frozen analytics version {analytics['version']!r}")
    if facts.get("orxanalytics_version_txt") != analytics["version"]:
        problems.append("orxanalytics-version.txt disagrees with the"
                        " frozen analytics version")
    if facts.get("source_commit") != analytics["commit"]:
        problems.append(
            f"Info.plist ORXAnalyticsSourceCommit {facts.get('source_commit')!r}"
            f" != frozen analytics commit {analytics['commit']}")
    in_app = facts.get("in_app_manifest") or {}
    for key, expected in (("version", analytics["version"]),
                          ("source_commit", analytics["commit"]),
                          ("channel", channel), ("mode", mode)):
        if in_app.get(key) != expected:
            problems.append(
                f"in-app distribution.json {key}={in_app.get(key)!r}"
                f" != frozen binding {expected!r}")
    if facts.get("channel") != channel:
        problems.append(
            f"Info.plist channel {facts.get('channel')!r} != {channel!r}")
    if facts.get("build_flavor") != mode:
        problems.append(
            f"build flavor {facts.get('build_flavor')!r} != channel mode"
            f" {mode!r}")
    if pinned and facts.get("bundled_python_name") != pinned.get("pbs_name"):
        problems.append(
            f"ORXAnalyticsBundledPython {facts.get('bundled_python_name')!r}"
            f" != pinned runtime {pinned.get('pbs_name')!r}")
    if facts.get("bundled_python_exit") != 0 or \
            facts.get("bundled_python_version") != expected_python:
        problems.append(
            "the bundled interpreter does not run as the pinned"
            f" {expected_python!r} (exit"
            f" {facts.get('bundled_python_exit')}, reports"
            f" {facts.get('bundled_python_version')!r})")
    if not str(facts.get("cf_bundle_version", "")).startswith(
            f"{analytics['version']}+{mode}+"):
        problems.append(
            f"CFBundleVersion {facts.get('cf_bundle_version')!r} does not"
            f" encode version+flavor+commit ({analytics['version']}+{mode}+…)")
    return problems


def package_metadata(base: Path, artifacts: dict) -> dict:
    return {
        "orx_wheel": wheel_metadata(
            base / artifacts["orx_wheel"]["path"]),
        "analytics_wheel": wheel_metadata(
            base / artifacts["analytics_wheel"]["path"]),
        "analytics_sdist": sdist_metadata(
            base / artifacts["analytics_sdist"]["path"]),
    }


def package_metadata_problems(meta: dict, binding: dict) -> list[str]:
    problems: list[str] = []
    orx_expect = ("orx-agent", binding["orx"]["version"])
    an_expect = ("orx-analytics", binding["analytics"]["version"])
    for key, expect in (("orx_wheel", orx_expect),
                        ("analytics_wheel", an_expect),
                        ("analytics_sdist", an_expect)):
        got = (meta[key]["Name"], meta[key]["Version"])
        if got != expect:
            problems.append(
                f"{key} metadata Name/Version {got} != {expect}")
    return problems


def frozen_commit_problems(binding: dict) -> list[str]:
    """The frozen commits still exist and still read the frozen versions
    (catches post-freeze history rewrites)."""
    problems: list[str] = []
    for repo, which in ((REPO_ROOT, "orx"), (ANALYTICS_REPO, "analytics")):
        commit = binding[which]["commit"]
        version = binding[which]["version"]
        probe = _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                     cwd=repo)
        if probe.returncode != 0:
            problems.append(f"the frozen {which} commit {commit} no longer"
                            " exists in the repository")
            continue
        reader = orx_version_at if which == "orx" else analytics_version_at
        if reader(repo, commit) != version:
            problems.append(
                f"the frozen {which} commit no longer reads version"
                f" {version!r}")
    return problems


def historical_tags_snapshot() -> dict:
    orx_tags = sorted(t for t in
                      _git(REPO_ROOT, "tag", "--list").splitlines() if t)
    an_tags = sorted(t for t in
                     _git(ANALYTICS_REPO, "tag", "--list").splitlines() if t)
    return {
        "orx": orx_tags,
        "analytics": an_tags,
        "required_orx_tags": list(REQUIRED_ORX_TAGS),
        "v0_3_0_commit": _git(REPO_ROOT, "rev-parse", f"{V030_TAG}^{{commit}}"),
        "v0_3_0_expected_commit": V030_COMMIT,
    }


def historical_tag_problems(snapshot: dict) -> list[str]:
    problems: list[str] = []
    missing = [tag for tag in snapshot["required_orx_tags"]
               if tag not in snapshot["orx"]]
    if missing:
        problems.append(f"historical ORX tags missing: {missing}")
    if snapshot.get("v0_3_0_commit") != V030_COMMIT:
        problems.append(
            f"tag {V030_TAG} moved: {snapshot.get('v0_3_0_commit')}"
            f" != {V030_COMMIT}")
    return problems


def freeze_worktree_problems(binding: dict) -> list[str]:
    """The uncommitted-inputs guard: at freeze time the only allowed dirt
    is the baseline-protected user report in the ORX repo (and NOTHING
    tracked-or-untracked-visible in the analytics clone). Builds are git
    archives/clones of the frozen commits, so this records that no
    uncommitted content existed to be excluded in the first place.

    G012 only: untracked GENERATED build output inside the analytics clone
    (``apps/macos/.build`` — ANALYTICS_GENERATED_UNTRACKED) is exempt; it
    can never enter a build and the ORX product input is the pinned
    v0.4.0 tag, not a worktree. Every other uncommitted entry — and the
    entire G011 guard (goal g011 or a legacy binding) — refuses unchanged."""
    problems: list[str] = []
    goal_id = binding.get("goal_id") or "g011"
    orx_entries = set(binding.get("orx", {}).get("worktree", {})
                      .get("entries", []))
    extra = orx_entries - {PROTECTED_REPORT_ENTRY}
    if extra:
        problems.append(
            f"uncommitted ORX worktree content at freeze time beyond the"
            f" protected report: {sorted(extra)}")
    an_entries = binding.get("analytics", {}).get("worktree", {}).get(
        "entries", [])
    if goal_id == "g012":
        an_entries = [entry for entry in an_entries
                      if not analytics_generated_exempt(entry)]
    if an_entries:
        problems.append(
            f"uncommitted analytics worktree content at freeze time:"
            f" {an_entries}")
    return problems


def orx_pin_problems(binding: dict) -> list[str]:
    """G012: the ORX product input is the PUBLISHED v0.4.0 tag — at
    candidate/verify time the pin must still resolve to the published
    commit and stay the bound ORX input. G011 bindings (no pinned_tag)
    are untouched."""
    pinned = binding.get("orx", {}).get("pinned_tag")
    if not pinned:
        return []
    problems: list[str] = []
    live = _git(REPO_ROOT, "rev-parse", f"{pinned['tag']}^{{commit}}")
    if live != pinned.get("published_commit"):
        problems.append(
            f"pinned ORX tag {pinned['tag']} moved: resolves to {live},"
            f" published commit is {pinned.get('published_commit')}")
    if live != binding.get("orx", {}).get("commit"):
        problems.append(
            f"pinned ORX tag {pinned['tag']} resolves to {live} but the"
            f" binding froze {binding.get('orx', {}).get('commit')}")
    return problems


def load_decision_facts(decision_file: Path, channel: str) -> dict:
    """The fixed channel decision, bound by sha256. The candidate channel
    must be explicitly allowed with a non-empty basis; the other
    channel's decision is recorded honestly (never silently flipped)."""
    if not decision_file.is_file():
        raise StageFailure(
            "candidate", "decision-missing",
            f"distribution decision file not found at {decision_file}")
    data = json.loads(decision_file.read_text(encoding="utf-8"))
    entry = (data.get("channels") or {}).get(channel)
    if not isinstance(entry, dict) or entry.get("decision") != "allowed":
        raise StageFailure(
            "candidate", "channel-not-allowed",
            f"the distribution decision does not allow channel"
            f" {channel!r}; an explicit NEW decision entry is required"
            " before a candidate can bind it (no silent downgrade)")
    basis = entry.get("basis") or []
    _require(isinstance(basis, list) and basis,
             f"channel {channel!r} is allowed without a basis list")
    other = "official" if channel == "internal-preview" else "internal-preview"
    other_entry = (data.get("channels") or {}).get(other) or {}
    try:
        rel = str(decision_file.resolve().relative_to(REPO_ROOT))
    except ValueError:
        rel = str(decision_file)
    return {
        "path": rel,
        "sha256": sha256_file(decision_file),
        "channel": channel,
        "decision": entry.get("decision"),
        "basis_count": len(basis),
        "signing": entry.get("signing"),
        "other_channel": {
            "channel": other,
            "decision": other_entry.get("decision"),
            "fail_closed": other_entry.get("fail_closed"),
        },
    }


def candidate_path(work_dir: Path) -> Path:
    return work_dir / "candidate.json"


def candidate_signature_problems(signature: dict, channel: str) -> list[str]:
    problems: list[str] = []
    if channel == "official":
        if signature.get("codesign_verify_exit") != 0 or not any(
                a.startswith("Developer ID Application")
                for a in signature.get("authorities", [])):
            problems.append(
                "the official channel requires a verified Developer ID"
                " Application signature (plus notarization evidence)")
    elif signature.get("codesign_verify_exit") != 0:
        problems.append(
            "the internal-preview channel still requires a VERIFIABLE"
            " signature (ad-hoc at minimum)")
    return problems


def _candidate_app_and_meta(base: Path, doc: dict, log_note: str) -> tuple:
    """Shared by candidate generation and candidate re-verification:
    extract the bound app, gather its facts, verify package metadata and
    the live signature. Raises StageFailure on any disagreement."""
    binding = doc["binding"]
    channel = binding["channel"]
    artifacts = doc["artifacts"]
    try:
        app_dir = extract_app_archive(base / artifacts["app_archive"]["path"],
                                      base / "candidate-app")
    except JointReleaseError as exc:
        raise StageFailure(
            "verify-candidate" if log_note == "verify" else "candidate",
            "app-archive-invalid",
            f"the bound app archive could not be extracted: {exc}")
    facts = gather_app_facts(app_dir)
    problems = app_fact_problems(facts, binding)
    if problems:
        raise StageFailure(
            "verify-candidate" if log_note == "verify" else "candidate",
            "app-metadata-mismatch",
            "the app artifact disagrees with the frozen binding",
            problems=problems)
    meta = package_metadata(base, artifacts)
    problems = package_metadata_problems(meta, binding)
    if problems:
        raise StageFailure(
            "verify-candidate" if log_note == "verify" else "candidate",
            "wheel-metadata-mismatch",
            "Python distribution metadata disagrees with the frozen"
            " versions", problems=problems)
    signature = signature_evidence(app_dir)
    problems = candidate_signature_problems(signature, channel)
    if problems:
        raise StageFailure(
            "verify-candidate" if log_note == "verify" else "candidate",
            "signature-evidence-missing",
            "channel signature requirements not met", problems=problems)
    return facts, meta, signature


def candidate_document(binding: dict, decision: dict, snapshot: dict,
                       battery_checks: list, missing: list[str],
                       battery_record: dict, artifacts: dict, facts: dict,
                       meta: dict) -> dict:
    """The frozen candidate document (schema 1). The goal label comes from
    the binding: a G012 candidate is labeled G012 (and never carries a
    G011 task id or note); a legacy binding without goal_id stays G011."""
    goal_id = binding.get("goal_id") or "g011"
    config = goal_config(goal_id)
    return {
        "schema": 1,
        "phase": "candidate",
        "goal": config["label"],
        "goal_id": goal_id,
        "task": config["candidate_task"],
        "generated_at": now_iso(),
        "published": False,
        "published_note": config["publish_note"],
        "freeze_commits": {
            "orx": binding["orx"]["commit"],
            "analytics": binding["analytics"]["commit"],
        },
        "versions": {
            "orx": binding["orx"]["version"],
            "analytics": binding["analytics"]["version"],
        },
        "channel": binding["channel"],
        "decision": decision,
        "historical_tags": snapshot,
        "protected_report": {
            "path": "docs/m1.2-report.md",
            "disposition": (
                "EXCLUDED from the release: the worktree modification is"
                " the only dirt allowed at freeze time and never enters"
                " any build — releases materialize the file from its"
                " committed state (git archive/clone of the freeze"
                f" commit); never staged, never committed by"
                f" {config['label']}"),
        },
        "binding": binding,
        "battery": {
            "checks": battery_checks,
            "all_passed": not missing,
            "checkouts": battery_record.get("checkouts", {}),
            "log": battery_record.get("log"),
        },
        "artifacts": artifacts,
        "app": facts,
        "package_metadata": meta,
        "invalidation": (
            "the candidate archive SHA256s are frozen AFTER the channel's"
            " final gates (internal-preview: verified ad-hoc signature;"
            " official: Developer ID + notarization + stapling). Any"
            " later source, signature or byte change invalidates the"
            " candidate and requires a rebuild plus joint re-acceptance"),
    }


def stage_candidate(work_dir: Path, log: StageLog,
                    decision_file: Path | None = None) -> dict:
    """Write candidate.json: the frozen candidate binding both repos'
    freeze commits, the channel, the historical-tag snapshot, the
    clean-checkout battery evidence, the artifact digests (frozen AFTER
    the channel's final gates) and the app/package consistency facts."""
    manifest = load_manifest(work_dir)
    stages = manifest.get("stages", {})
    for required in ("freeze", "battery", "build"):
        record = stages.get(required)
        if not record or record.get("status") != "passed":
            raise StageFailure(
                "candidate", "missing-step-evidence",
                f"stage {required!r} has not passed; the candidate binds"
                " the frozen, clean-checkout-checked, built combination")
    binding = manifest["binding"]
    channel = binding["channel"]
    if decision_file is None:
        # per-goal default (g011: docs/diagnostics/g011/distribution.json;
        # g012: docs/diagnostics/g012/distribution.json)
        decision_file = goal_config(binding.get("goal_id"))["decision_file"]
    decision_file = Path(decision_file)
    decision = load_decision_facts(decision_file, channel)
    log.write(f"decision: {channel} allowed by {decision['path']}"
              f" (sha256 {decision['sha256'][:16]}…)")

    # artifacts re-digested + identity-checked against the frozen binding
    verify_artifacts(work_dir, manifest)
    problems = frozen_commit_problems(binding)
    if problems:
        raise StageFailure("candidate", "commit-mismatch",
                           "the frozen commits changed under the candidate",
                           problems=problems)
    problems = orx_pin_problems(binding)
    if problems:
        raise StageFailure(
            "candidate", "orx-pin-moved",
            "the pinned ORX product input (published v0.4.0) drifted"
            " under the candidate", problems=problems)
    problems = freeze_worktree_problems(binding)
    if problems:
        raise StageFailure(
            "candidate", "uncommitted-inputs-at-freeze",
            "uncommitted content was present at freeze time beyond the"
            " baseline-protected user report", problems=problems)

    battery_record = stages["battery"]
    battery_checks = battery_record.get("checks", [])
    passed = {c["name"] for c in battery_checks if c.get("exit_code") == 0}
    missing = [name for name in REQUIRED_BATTERY_CHECKS
               if name not in passed]
    if missing:
        raise StageFailure(
            "candidate", "battery-evidence-missing",
            f"clean-checkout battery checks missing or failed: {missing}")

    _, problems = verify_samples(REPO_ROOT, ANALYTICS_REPO,
                                 workroot=work_dir / "candidate-samples")
    if problems:
        raise StageFailure("candidate", "samples-invalid",
                           "sample provenance failed at candidate time",
                           problems=problems)

    facts, meta, signature = _candidate_app_and_meta(work_dir, manifest,
                                                     "candidate")
    artifacts = manifest["artifacts"]
    artifacts["app_archive"]["signature"] = signature
    log.write(f"app facts: version {facts['cf_bundle_version_string']}"
              f" commit {facts['source_commit'][:12]} channel"
              f" {facts['channel']} runtime {facts['bundled_python_name']}"
              f" (runs {facts['bundled_python_version']})")
    log.write(f"signature: style={signature.get('signature_style')}"
              f" verify_exit={signature.get('codesign_verify_exit')}")

    snapshot = historical_tags_snapshot()
    problems = historical_tag_problems(snapshot)
    if problems:
        raise StageFailure("candidate", "historical-tag-moved",
                           "historical tags drifted", problems=problems)

    app_record = artifacts["app_archive"]
    candidate = candidate_document(
        binding, decision, snapshot, battery_checks, missing,
        battery_record, artifacts, facts, meta)
    path = candidate_path(work_dir)
    path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n")
    log.write(f"candidate written: {path.name}"
              f" (orx {candidate['versions']['orx']}"
              f" @ {candidate['freeze_commits']['orx'][:12]},"
              f" analytics {candidate['versions']['analytics']}"
              f" @ {candidate['freeze_commits']['analytics'][:12]},"
              f" channel {channel}, published=false)")
    record_stage(manifest, "candidate", "passed", log.rel,
                 candidate="candidate.json", channel=channel,
                 published=False)
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    return candidate


def verify_candidate(candidate_file: Path) -> dict:
    """Re-verify a FROZEN CANDIDATE read-only (the prescribed T006
    check). Loads candidate.json, re-digests every artifact, re-checks
    identity/metadata/app facts/signature/battery evidence/decision
    sha/historical tags/sample provenance. The operator GUI acceptance
    (T007) and the published channel root (T008) are NOT part of this
    phase — their absence is not judged here and never a pass claim."""
    candidate_file = Path(candidate_file).resolve()
    if not candidate_file.is_file():
        raise StageFailure(
            "verify-candidate", "manifest-missing",
            f"candidate manifest not found at {candidate_file}")
    doc = json.loads(candidate_file.read_text(encoding="utf-8"))
    if doc.get("phase") != "candidate" or doc.get("schema") != 1 \
            or not isinstance(doc.get("binding"), dict) \
            or not isinstance(doc.get("artifacts"), dict):
        raise StageFailure(
            "verify-candidate", "candidate-shape-invalid",
            "the candidate manifest is not a schema-1 candidate document")
    artifacts = doc["artifacts"]
    for key in ("orx_wheel", "analytics_wheel", "analytics_sdist",
                "app_archive"):
        if not isinstance(artifacts.get(key), dict) \
                or not artifacts[key].get("sha256"):
            raise StageFailure(
                "verify-candidate", "candidate-shape-invalid",
                f"artifact {key!r} has no digest record")
    binding = doc["binding"]
    # the goal label must agree with the binding (a G012 candidate is
    # never verified under a G011 label, or vice versa)
    try:
        expected_label = goal_config(binding.get("goal_id"))["label"]
    except JointReleaseError:
        expected_label = None
    if doc.get("goal") != expected_label:
        raise StageFailure(
            "verify-candidate", "candidate-shape-invalid",
            f"the candidate goal label {doc.get('goal')!r} disagrees with"
            f" its binding goal {binding.get('goal_id')!r}")
    base = candidate_file.parent

    # digests + identity against the frozen binding (fail-closed codes
    # shared with the acceptance verify: artifact-hash-mismatch,
    # artifact-identity-mismatch, commit-mismatch, channel-artifact-…)
    verify_artifacts(base, doc)

    problems = frozen_commit_problems(binding)
    if problems:
        raise StageFailure("verify-candidate", "commit-mismatch",
                           "the frozen commits are no longer valid",
                           problems=problems)
    problems = orx_pin_problems(binding)
    if problems:
        raise StageFailure("verify-candidate", "orx-pin-moved",
                           "the pinned ORX product input (published v0.4.0)"
                           " drifted under the candidate", problems=problems)

    battery = doc.get("battery") or {}
    checks = {c.get("name"): c for c in battery.get("checks", [])}
    for name in REQUIRED_BATTERY_CHECKS:
        entry = checks.get(name)
        if not entry or entry.get("exit_code") != 0:
            raise StageFailure(
                "verify-candidate", "battery-evidence-missing",
                f"clean-checkout battery check {name!r} is missing or"
                " failed in the candidate manifest")
        log_path = base / str(entry.get("log", ""))
        if not log_path.is_file():
            raise StageFailure(
                "verify-candidate", "missing-step-evidence",
                f"battery check {name!r} references log"
                f" {entry.get('log')!r} which does not exist")

    facts, meta, signature = _candidate_app_and_meta(base, doc, "verify")

    build_record = artifacts["app_archive"].get("build_record")
    if build_record:
        record_path = base / build_record["path"]
        if not record_path.is_file() or \
                sha256_file(record_path) != build_record["sha256"]:
            raise StageFailure(
                "verify-candidate", "artifact-hash-mismatch",
                "the app build record changed on disk")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("channel") != binding["channel"] \
                or record.get("source_commit") != binding["analytics"]["commit"] \
                or record.get("version") != binding["analytics"]["version"] \
                or record.get("sha256") != artifacts["app_archive"]["sha256"]:
            raise StageFailure(
                "verify-candidate", "artifact-identity-mismatch",
                "the app build record disagrees with the frozen binding"
                " or the archive digest")

    decision = doc.get("decision") or {}
    decision_path = REPO_ROOT / str(decision.get("path", ""))
    if decision.get("path") and (
            not decision_path.is_file()
            or sha256_file(decision_path) != decision.get("sha256")):
        raise StageFailure(
            "verify-candidate", "decision-drift",
            f"the distribution decision {decision.get('path')!r} changed"
            " after the candidate was frozen; a new decision requires a"
            " rebuild and joint re-acceptance")

    snapshot = historical_tags_snapshot()
    problems = historical_tag_problems(snapshot)
    recorded = doc.get("historical_tags") or {}
    if recorded.get("orx") != snapshot["orx"] or \
            recorded.get("analytics") != snapshot["analytics"]:
        problems.append(
            "the live tag snapshot differs from the candidate's frozen"
            " snapshot (tags were added or removed after the freeze)")
    if problems:
        raise StageFailure("verify-candidate", "historical-tag-moved",
                           "historical tags drifted", problems=problems)

    _, problems = verify_samples(REPO_ROOT, ANALYTICS_REPO,
                                 workroot=base / "candidate-verify-samples")
    if problems:
        raise StageFailure("verify-candidate", "samples-invalid",
                           "sample provenance failed at candidate"
                           " verification", problems=problems)

    return {
        "phase": "candidate",
        "verified_at": now_iso(),
        "published": doc.get("published", False),
        "channel": binding["channel"],
        "freeze_commits": {
            "orx": binding["orx"]["commit"],
            "analytics": binding["analytics"]["commit"],
        },
        "versions": {
            "orx": binding["orx"]["version"],
            "analytics": binding["analytics"]["version"],
        },
        "artifacts": {name: row["sha256"]
                      for name, row in sorted(artifacts.items())
                      if isinstance(row, dict) and row.get("sha256")},
        "battery": {
            name: checks[name]["exit_code"]
            for name in REQUIRED_BATTERY_CHECKS if name in checks},
        "app": {
            "version": facts.get("cf_bundle_version_string"),
            "source_commit": facts.get("source_commit"),
            "channel": facts.get("channel"),
            "bundled_python": facts.get("bundled_python_name"),
            "bundled_python_version": facts.get("bundled_python_version"),
        },
        "signature": {
            "style": signature.get("signature_style"),
            "codesign_verify_exit": signature.get("codesign_verify_exit"),
            "authorities": signature.get("authorities"),
        },
        "package_metadata": meta,
        "historical_tags_unchanged": True,
        "samples_verified": True,
    }


# ---------------------------------------------------------------------------
# Stage: verify-remote (the consumer / distribution side)
# ---------------------------------------------------------------------------


def stage_verify_remote(work_dir: Path, log: StageLog,
                        channel_root: Path | None) -> dict:
    manifest = load_manifest(work_dir)
    stage_verify_quiet(manifest, work_dir)
    binding = manifest["binding"]
    channel = binding["channel"]
    root = Path(channel_root) if channel_root else \
        work_dir / "channels" / channel
    if not root.is_dir():
        raise StageFailure(
            "verify-remote", "channel-root-missing",
            f"published channel root not found at {root} — G011 T008"
            " publishes the jointly accepted artifacts there; until then"
            " the consumer-side check fails closed")
    remote_manifest = root / "manifest.json"
    if not remote_manifest.is_file():
        raise StageFailure("verify-remote", "channel-root-missing",
                           f"no manifest.json under {root}")
    remote = json.loads(remote_manifest.read_text())
    if remote.get("binding") != binding:
        raise StageFailure(
            "verify-remote", "remote-binding-mismatch",
            "the published manifest does not match the local frozen"
            " binding (commits/versions/channel drifted)")
    for name, row in manifest["artifacts"].items():
        published = root / row["path"]
        if not published.is_file():
            raise StageFailure("verify-remote", "channel-root-missing",
                               f"artifact {name} not published at"
                               f" {row['path']}")
        if sha256_file(published) != row["sha256"]:
            raise StageFailure(
                "verify-remote", "remote-hash-mismatch",
                f"published artifact {name} is not byte-identical to the"
                " accepted one")
    # channel separation: the internal preview root must never carry the
    # official DMG; the official root must never carry dev zips —
    # anywhere under the published root.
    wrong = {
        "internal-preview": list(root.rglob("*.dmg")),
        "official": list(root.rglob("*-dev-*.zip")),
    }[channel]
    if wrong:
        raise StageFailure(
            "verify-remote", "channel-separation-violation",
            f"{channel} channel root carries foreign-flavor artifacts:"
            f" {[p.name for p in wrong]}")
    log.write(f"channel {channel} verified against {root}")
    record_stage(manifest, "verify-remote", "passed", log.rel,
                 channel_root=str(root), channel=channel,
                 published_artifacts=sorted(manifest["artifacts"]))
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    return manifest


def stage_verify_quiet(manifest: dict, work_dir: Path) -> None:
    """verify-remote requires the LOCAL acceptance to have passed first
    (verify record green) — you cannot publish-check what never passed."""
    record = manifest.get("stages", {}).get("verify")
    if not record or record.get("status") != "passed":
        raise StageFailure(
            "verify-remote", "missing-step-evidence",
            "the local verify stage has not passed; the consumer-side"
            " check only applies to an accepted combination")


# ---------------------------------------------------------------------------
# Stage: self-test — harness machinery with stand-ins, NO product claim
# ---------------------------------------------------------------------------


def probe_fail_closed(work_dir: Path) -> dict:
    """The fail-closed behaviors, exercised for real on scratch state:
    build/exercise/verify refuse missing app / step / GUI evidence and
    reject skipped records."""
    probe: dict = {}
    scratch = work_dir / "failclosed-probe"
    if scratch.exists():
        shutil.rmtree(scratch)
    log = StageLog(scratch, "probe")
    stage_freeze(scratch, "internal-preview", log)

    # exercise before build: the step evidence is missing
    try:
        stage_exercise(scratch, log)
        raise AssertionError("exercise passed before build")
    except StageFailure as failure:
        _require(failure.code == "missing-step-evidence",
                 f"exercise fail-closed code {failure.code!r}")
        probe["exercise_before_build"] = failure.code

    # build without the final app artifact (today's honest state: the
    # app is G011 T006's deliverable) fails closed naming it
    try:
        stage_build(scratch, log)
        raise AssertionError("build passed without the app artifact")
    except StageFailure as failure:
        _require(failure.code == "app-artifact-missing",
                 f"build fail-closed code {failure.code!r}")
        probe["build_without_app"] = failure.code

    # even with a 'passed' build record, exercise refuses an unbound app
    manifest = load_manifest(scratch)
    (scratch / "logs").mkdir(exist_ok=True)
    (scratch / "logs" / "02-build.log").write_text("stand-in build log\n")
    record_stage(manifest, "build", "passed", "logs/02-build.log")
    save_manifest(scratch, manifest)
    try:
        stage_exercise(scratch, log)
        raise AssertionError("exercise passed without an app artifact")
    except StageFailure as failure:
        _require(failure.code == "app-artifact-missing",
                 f"exercise fail-closed code {failure.code!r}")
        probe["exercise_without_app"] = failure.code

    # verify without the full step chain
    try:
        stage_verify(scratch, log)
        raise AssertionError("verify passed without build/exercise")
    except StageFailure as failure:
        _require(failure.code == "missing-step-evidence",
                 f"verify fail-closed code {failure.code!r}")
        probe["verify_without_steps"] = failure.code

    # a skipped record can never pass verify (the other steps are green,
    # so the skip itself is the only thing left to reject)
    manifest = load_manifest(scratch)
    (scratch / "logs").mkdir(exist_ok=True)
    for name in ("03-battery.log", "04-exercise.log"):
        log_file = scratch / "logs" / name
        if not log_file.exists():
            log_file.write_text("stand-in log\n")
    record_stage(manifest, "battery", "passed", "logs/03-battery.log")
    record_stage(manifest, "exercise", "passed", "logs/04-exercise.log")
    record_stage(manifest, "build", "skipped", "logs/xx.log")
    save_manifest(scratch, manifest)
    try:
        stage_verify(scratch, log)
        raise AssertionError("verify accepted a skipped stage")
    except StageFailure as failure:
        _require(failure.code == "skipped-not-accepted",
                 f"verify skip-rejection code {failure.code!r}")
        probe["verify_rejects_skipped"] = failure.code
    log.close()
    return probe


def probe_rejections(work_dir: Path, manifest: dict) -> dict:
    """The tamper rejections on a full-looking scratch state: hash drift,
    identity (commit/version) mismatch, missing log evidence."""
    probe: dict = {}
    scratch = work_dir / "rejections-probe"
    if scratch.exists():
        shutil.rmtree(scratch)
    (scratch / "dist").mkdir(parents=True)
    fake_wheel = scratch / "dist" / \
        f"orx_agent-{manifest['binding']['orx']['version']}-py3-none-any.whl"
    fake_wheel.write_bytes(b"stand-in wheel bytes\n")
    fake_app = scratch / "dist" / \
        "ORXAnalytics-0.0.0-dev-macos13-arm64.zip"
    fake_app.write_bytes(b"stand-in app bytes\n")
    binding = json.loads(json.dumps(manifest["binding"]))  # deep copy
    scratch_manifest = {
        "binding": binding,
        "artifacts": {
            "orx_wheel": {"path": f"dist/{fake_wheel.name}",
                          "sha256": sha256_file(fake_wheel),
                          "built_from": binding["orx"]["commit"],
                          "version": binding["orx"]["version"]},
            "analytics_wheel": {
                "path": f"dist/{fake_wheel.name}",  # stand-in slot
                "sha256": sha256_file(fake_wheel),
                "built_from": binding["analytics"]["commit"]},
            "app_archive": {
                "path": f"dist/{fake_app.name}",
                "sha256": sha256_file(fake_app),
                "built_from": binding["analytics"]["commit"],
                "mode": "dev",
                "signature": {"codesign_verify_exit": 0,
                              "signature_style": "adhoc",
                              "authorities": [], "adhoc": True}},
        },
        "stages": {
            "freeze": {"status": "passed", "log": "logs/01-freeze.log"},
            "battery": {"status": "passed", "log": "logs/02-battery.log"},
            "build": {"status": "passed", "log": "logs/03-build.log"},
            "exercise": {
                "status": "passed", "log": "logs/04-exercise.log",
                "engine": {"connected_via": "stand-in",
                           "read_only_source": True}},
        },
    }
    logs = scratch / "logs"
    logs.mkdir()
    for name in ("01-freeze.log", "02-battery.log", "03-build.log",
                 "04-exercise.log"):
        (logs / name).write_text("stand-in stage log\n")
    (scratch / "gui-evidence.json").write_text(json.dumps({
        "operator": "self-test stand-in",
        "artifacts": ["logs/04-exercise.log"],
        "observed_at": now_iso(),
    }))
    save_manifest(scratch, scratch_manifest)

    def expect(code: str, mutate) -> None:
        """Mutate (manifest and/or files), expect the named rejection,
        then restore EVERYTHING (manifest bytes and moved files)."""
        current = load_manifest(scratch)
        restore_manifest = json.loads(json.dumps(current))
        gui = scratch / "gui-evidence.json"
        gui_backup = gui.read_bytes() if gui.is_file() else None
        moved: list[tuple[Path, Path]] = []
        try:
            if code == "gui-evidence-missing":
                aside = scratch / "gui-evidence.aside"
                gui.rename(aside)
                moved.append((aside, gui))
            else:
                mutate(current)
                save_manifest(scratch, current)
            try:
                stage_verify(scratch, StageLog(scratch, "probe"))
                raise AssertionError(
                    f"verify accepted tampered state ({code})")
            except StageFailure as failure:
                _require(failure.code == code,
                         f"expected {code}, got {failure.code}")
        finally:
            save_manifest(scratch, restore_manifest)
            for aside, target in moved:
                if aside.exists():
                    aside.rename(target)
            if gui_backup is not None and gui.is_file():
                gui.write_bytes(gui_backup)
        probe[code] = True

    expect("artifact-hash-mismatch", lambda m: m["artifacts"]["orx_wheel"]
           .update(sha256="0" * 64))
    expect("artifact-identity-mismatch", lambda m: m["binding"]["orx"]
           .update(version="9.9.9"))  # the bound wheel is not this version
    expect("commit-mismatch", lambda m: m["artifacts"]["analytics_wheel"]
           .update(built_from="f" * 40))
    expect("channel-artifact-mismatch", lambda m: m["artifacts"]
           ["app_archive"].update(mode="dist"))
    expect("missing-step-evidence", lambda m: m["stages"].pop("exercise"))
    expect("gui-evidence-missing", lambda m: None)
    expect("signature-evidence-missing", lambda m: m["artifacts"]
           ["app_archive"].update(signature=None))
    return probe


def stage_self_test(work_dir: Path, log: StageLog) -> dict:
    """Exercise the harness machinery END TO END with a STAND-IN app
    delivery. Real frozen commits, real wheels, the app's own engine
    serve entry — but a dev runtime instead of the bundled one, no .app
    bundle, no GUI, no signature. The report does NOT claim product
    acceptance, and every real-run requirement stays unmet here.

    The caller (main) cleans the work dir BEFORE opening the stage log:
    this function must never delete the directory its own log lives in.
    """
    report: dict = {"product_acceptance_claimed": False}

    manifest = stage_freeze(work_dir, "internal-preview", log)
    report["freeze"] = "passed"

    # The ORX wheel IS real here: built from the frozen commit's one-shot
    # archive, installed into a throwaway venv, driven as the installed
    # CLI. Only the APP delivery is a stand-in.
    orx_commit = manifest["binding"]["orx"]["commit"]
    orx_checkout = archive_commit(REPO_ROOT, orx_commit,
                                  work_dir / "checkouts" / "orx")
    wheel = build_orx_wheel(orx_checkout, work_dir / "dist")
    wheel_version = re.match(r"orx_agent-([0-9][^-]*)-", wheel.name).group(1)
    _require(wheel_version == manifest["binding"]["orx"]["version"],
             "self-test wheel version does not match the frozen commit")
    manifest.setdefault("artifacts", {})["orx_wheel"] = {
        "path": f"dist/{wheel.name}", "sha256": sha256_file(wheel),
        "built_from": orx_commit, "version": wheel_version,
        "built_from_archive": True,
    }
    save_manifest(work_dir, manifest)
    log.write(f"self-test built the frozen ORX wheel: {wheel.name}")

    an_commit = manifest["binding"]["analytics"]["commit"]
    an_checkout = archive_commit(ANALYTICS_REPO, an_commit,
                                 work_dir / "checkouts" / "analytics")
    stand_in_app = assemble_stand_in_app(
        work_dir, an_checkout, manifest["binding"]["analytics"]["version"])
    log.write("stand-in app delivery assembled (dev runtime; NOT a product"
              " delivery; unsigned; no Swift app)")

    facts = exercise_driver(work_dir, manifest, log, app_dir=stand_in_app,
                            stand_in=True)
    record_stage(manifest, "build", "passed", log.rel,
                 stand_in=True,
                 note="self-test: the ORX wheel is the real frozen build;"
                      " the analytics dist and the final app artifact are"
                      " NOT bound here (the real build stage does that)")
    record_stage(manifest, "exercise", "passed", log.rel,
                 **facts, stand_in=True)
    manifest["selftest"] = {
        "stand_in_app_delivery": True,
        "claims_product_acceptance": False,
        "banner": "SELF-TEST ONLY: stand-in app delivery; the final App"
                  " artifacts (G011 T006) and the operator GUI/signature"
                  " evidence are intentionally absent and stay unmet",
    }
    save_manifest(work_dir, manifest)

    upgrade = current_analytics_upgrades_store_v2(
        ANALYTICS_REPO,
        REPO_ROOT / STORE_V2_SAMPLE_DIR / "analytics.db",
        work_dir / "upgrade-probe")
    refusal = old_analytics_refuses_newer_orx(REPO_ROOT, ANALYTICS_REPO,
                                              work_dir / "refusal-probe")
    log.write(f"store v2→current upgrade chain: {json.dumps(upgrade, sort_keys=True)}")
    log.write(f"old-code rollback refusal: {refusal['refusal']}")

    probe = probe_fail_closed(work_dir)
    rejections = probe_rejections(work_dir, manifest)
    remote = None
    try:
        stage_verify_remote(work_dir, log, None)
        raise AssertionError("verify-remote passed before verify")
    except StageFailure as failure:
        # correct order: the consumer-side check refuses BEFORE even
        # looking for the channel root, because the local acceptance
        # (verify) has not passed in the self-test (no final app). The
        # channel-root-missing branch is covered by the pytest suite.
        _require(failure.code == "missing-step-evidence",
                 f"verify-remote fail-closed code {failure.code!r}")
        remote = failure.code

    report.update({
        "stand_in_exercise": "passed",
        "store_v2_upgrade_chain": upgrade,
        "old_code_rollback_refusal": refusal,
        "fail_closed_probes": probe,
        "tamper_rejections": sorted(rejections),
        "verify_remote_without_channel": remote,
    })
    (work_dir / "selftest-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n")
    record_stage(manifest, "self-test", "passed", log.rel,
                 stand_in=True, product_acceptance_claimed=False)
    save_manifest(work_dir, manifest)
    evidence_index(work_dir, manifest)
    log.write("SELF-TEST PASSED — harness machinery only; this is NOT the"
              " product acceptance run")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Joint frozen-build + acceptance driver (G011 combo"
                    " ORX + orx-analytics; G012 analytics-only 0.4.3 via"
                    " --goal-id g012)")
    # --work-dir is accepted before AND after the stage name (the
    # prescribed check uses `self-test --work-dir <dir>`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--work-dir", type=Path, default=None,
                        help="working directory (default:"
                             f" {DEFAULT_WORK_DIR}; g012: dist/g012)")
    common.add_argument("--goal-id", choices=tuple(GOAL_CONFIGS),
                        default=None,
                        help="goal profile: g011 (default — the joint"
                             " 0.4.0+0.4.2 combo, ORX input = repo HEAD)"
                             " or g012 (analytics-only 0.4.3 with the ORX"
                             " product input pinned to the published"
                             " v0.4.0 tag; default work dir dist/g012,"
                             " decision docs/diagnostics/g012/"
                             "distribution.json)")
    sub = parser.add_subparsers(dest="stage", required=True)
    sub.add_parser("self-test", parents=[common])
    sub.add_parser("freeze", parents=[common])
    sub.add_parser("battery", parents=[common])
    build_p = sub.add_parser("build", parents=[common])
    build_p.add_argument("--build-app", action="store_true",
                         help="build the final app via the frozen checkout's"
                              " build_app.sh (G011 T006 path)")
    build_p.add_argument("--app-archive", type=Path, default=None,
                         help="bind an existing final app archive")
    build_p.add_argument("--decision", type=Path, default=None,
                         help="distribution decision file honored via"
                              " ORX_DISTRIBUTION_DECISION (required for the"
                              " official channel; default:"
                              " docs/diagnostics/g011/distribution.json)")
    candidate_p = sub.add_parser("candidate", parents=[common])
    candidate_p.add_argument("--decision", type=Path, default=None,
                             help="distribution decision file (default:"
                                  " docs/diagnostics/g011/distribution.json)")
    exercise_p = sub.add_parser("exercise", parents=[common])
    exercise_p.add_argument("--stand-in-app-dir", type=Path, default=None,
                            help=argparse.SUPPRESS)  # self-test internals
    verify_p = sub.add_parser("verify", parents=[common])
    verify_p.add_argument("--manifest", type=Path, default=None,
                          help="a frozen candidate manifest to re-verify"
                               " (used with --phase candidate)")
    verify_p.add_argument("--phase", choices=("candidate",), default=None,
                          help="verification phase: 'candidate' re-verifies"
                               " a frozen candidate.json read-only (T006);"
                               " the default work-dir verify is the full"
                               " acceptance (T007)")
    remote_p = sub.add_parser("verify-remote", parents=[common])
    remote_p.add_argument("--channel-root", type=Path, default=None,
                          help="published channel root to verify"
                               " (default: <work>/channels/<channel>)")
    gen_p = sub.add_parser("generate-samples", parents=[common])
    gen_p.add_argument("--dest", type=Path,
                       default=REPO_ROOT / JOINT_FIXTURE_DIR)
    args = parser.parse_args(argv)

    # The candidate phase re-verifies a frozen candidate manifest
    # read-only; it shares nothing with the work-dir acceptance verify.
    if args.stage == "verify" and (
            args.phase is not None or args.manifest is not None):
        if args.phase != "candidate" or args.manifest is None:
            parser.error("--manifest PATH goes together with"
                         " --phase candidate; the full acceptance verify"
                         " uses --work-dir")
        try:
            summary = verify_candidate(args.manifest)
        except StageFailure as failure:
            print(json.dumps({
                "stage": failure.stage, "code": failure.code,
                "message": failure.message, "detail": failure.detail,
            }, indent=2), file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    work_dir = args.work_dir or goal_config(args.goal_id)["default_work_dir"]
    if not work_dir.is_absolute():
        work_dir = REPO_ROOT / work_dir
    if args.stage == "self-test":
        # the self-test is deterministic from a clean work dir — clean it
        # BEFORE the stage log is opened inside it
        if work_dir.exists():
            shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.stage == "generate-samples":
        dest = args.dest if args.dest.is_absolute() else REPO_ROOT / args.dest
        log = StageLog(work_dir, "generate-samples")
        try:
            result = generate_store_v2_sample(
                dest / STORE_V2_SAMPLE_DIR.name, REPO_ROOT, ANALYTICS_REPO,
                work_dir / "generate-work", log)
            (dest / "samples.json").write_text(json.dumps({
                "orx_old": {
                    "path": str(V030_FIXTURE_DIR),
                    "produced_by": f"real {V030_TAG} code"
                                   f" (commit {V030_COMMIT})",
                    "see": f"{V030_FIXTURE_DIR}/PROVENANCE.md"},
                "analytics_store_v2": {
                    "path": str(STORE_V2_SAMPLE_DIR),
                    "produced_by": f"real analytics commit"
                                   f" {ANALYTICS_STORE_V2_COMMIT}"
                                   f" (version {ANALYTICS_STORE_V2_VERSION},"
                                   f" store v2)",
                    "see": f"{STORE_V2_SAMPLE_DIR}/PROVENANCE.md",
                    "sha256": result["sha256"]},
            }, indent=2, sort_keys=True) + "\n")
            print(json.dumps(result, indent=2, sort_keys=True))
            log.write(f"samples generated into {dest}")
            log.close()
            return 0
        except (JointReleaseError, StageFailure) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            log.write(f"FAILED: {exc}")
            log.close()
            return 1

    log = StageLog(work_dir, args.stage)
    try:
        try:
            if args.stage == "self-test":
                report = stage_self_test(work_dir, log)
                print(json.dumps(report, indent=2, sort_keys=True))
            elif args.stage == "freeze":
                manifest = load_manifest(work_dir) \
                    if manifest_path(work_dir).is_file() else None
                previous = (manifest or {}).get("binding", {})
                channel = previous.get("channel", "internal-preview")
                # goal continuity: an existing g012 work dir stays g012
                # unless --goal-id explicitly selects another profile
                goal_id = args.goal_id or previous.get("goal_id") or "g011"
                stage_freeze(work_dir, channel, log, goal_id=goal_id)
            elif args.stage == "battery":
                stage_battery(work_dir, log)
            elif args.stage == "build":
                # the per-goal decision default (g011/g012
                # distribution.json) is resolved inside the stage from
                # the manifest's binding goal
                stage_build(work_dir, log, build_app=args.build_app,
                            app_archive=args.app_archive,
                            decision=args.decision)
            elif args.stage == "candidate":
                stage_candidate(work_dir, log, decision_file=args.decision)
            elif args.stage == "exercise":
                stage_exercise(work_dir, log,
                               stand_in_app_dir=args.stand_in_app_dir)
            elif args.stage == "verify":
                stage_verify(work_dir, log)
            elif args.stage == "verify-remote":
                stage_verify_remote(work_dir, log, args.channel_root)
        finally:
            stop_all_engines()  # never leak an engine across stages/runs
        log.close()
        return 0
    except StageFailure as failure:
        # Record the honest failure in the manifest, then exit non-zero.
        try:
            manifest = load_manifest(work_dir)
            record_stage(manifest, failure.stage, "failed", log.rel,
                         code=failure.code, message=failure.message,
                         **failure.detail)
            save_manifest(work_dir, manifest)
            evidence_index(work_dir, manifest)
        except (StageFailure, JointReleaseError, json.JSONDecodeError,
                OSError):
            pass
        print(json.dumps({
            "stage": failure.stage, "code": failure.code,
            "message": failure.message, "detail": failure.detail,
        }, indent=2), file=sys.stderr)
        log.write(f"STAGE FAILED [{failure.code}]: {failure.message}")
        log.close()
        return 1
    except JointReleaseError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        log.write(f"HARNESS ERROR: {exc}")
        log.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
