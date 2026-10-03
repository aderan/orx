"""Adapter tests against fake executables on PATH. No network, no real
codex/agent process, no paid model."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orx import adapters, dispatch
from orx.adapters import codex as codex_adapter
from orx.adapters import cursor as cursor_adapter
from orx.adapters.base import EFFORT_MAP
from orx.records import ORXError

from conftest import HOST_PROFILES_TOML, ir_for, make_project, task_spec

FAKE_PLAN = json.dumps({
    "goal": "G001",
    "exploration": {"summary": "fake exploration", "relevant_components": ["."],
                     "unknowns": [], "assumptions": [], "risks": []},
    "approach": {"summary": "fake approach", "decisions": []},
    "tasks": [
        {"id": "T001", "objective": "fake task", "dependencies": [],
         "scope": {"allowed": ["src/"]}, "acceptance": ["a1"],
         "verification": [], "routing": {"complexity": "low"}},
    ],
})


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bindir(tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    previous = os.environ["PATH"]
    os.environ["PATH"] = f"{directory}:{previous}"
    codex_adapter.reset_caches()
    cursor_adapter.reset_caches()
    yield directory
    os.environ["PATH"] = previous
    codex_adapter.reset_caches()
    cursor_adapter.reset_caches()


def make_cli_project(tmp_path, profiles_extra="", config_replacements=(),
                     monkeypatch=None):
    config = """\
schema_version = 1

[controller]
profile = "shell-planner"

[plan]
depth = "auto"
allow_class_downgrade = false

[plan.light]
profiles = ["shell-planner"]

[plan.standard]
profiles = ["shell-planner"]

[plan.deep]
profiles = ["shell-planner"]

[worker]
profiles = ["shell-worker"]

[verify]
profiles = ["shell-verifier"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""
    for old, new in config_replacements:
        config = config.replace(old, new)
    profiles = """\
schema_version = 1

[profiles.shell-planner]
driver = "cli"
harness = "shell"
executable = "fake-planner"
prompt_transport = "stdin"
model = "fake"
class = "strong"
effort = "deep"
capabilities = ["coding"]

[profiles.shell-worker]
driver = "cli"
harness = "shell"
executable = "fake-worker"
prompt_transport = "stdin"
model = "fake"
class = "strong"
effort = "deep"
capabilities = ["coding"]

[profiles.shell-verifier]
driver = "cli"
harness = "shell"
executable = "fake-verifier"
prompt_transport = "stdin"
model = "fake"
class = "strong"
effort = "standard"
capabilities = ["coding"]
""" + profiles_extra
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path, config_toml=config, profiles_toml=profiles)


# ---------------------------------------------------------------------------
# Shell adapter transports


def test_shell_transport_stdin(tmp_path, bindir, monkeypatch):
    seen = tmp_path / "prompt-seen.txt"
    make_bin(bindir, "fake-worker",
             f'cat > "{seen}"\necho ORX_ACTUAL_EFFORT=high\n')
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "passed"
        assert "do the thing" in seen.read_text()  # prompt arrived on stdin
        # effort reported by the child is recorded
        assert result["started"][0]["actual_effort"] == "high"
        assert result["started"][0]["effort_source"] == "reported"
    finally:
        project.close()


def test_shell_transport_argument_slot(tmp_path, bindir, monkeypatch):
    seen = tmp_path / "prompt-seen.txt"
    make_bin(bindir, "fake-worker", f'echo "$1" > "{seen}"\n')
    project = make_cli_project(
        tmp_path,
        profiles_extra="""
[profiles.shell-worker-arg]
driver = "cli"
harness = "shell"
executable = "fake-worker"
args = ["{prompt}"]
prompt_transport = "argument"
model = "fake"
class = "strong"
effort = "quick"
capabilities = ["coding"]
""",
        config_replacements=(('profiles = ["shell-worker"]',
                              'profiles = ["shell-worker-arg"]',),),
        monkeypatch=monkeypatch,
    )
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", objective="PORTABLE OBJECTIVE",
                                                             acceptance=["a1"])]))
        dispatch.run_slice(project)
        assert "PORTABLE OBJECTIVE" in seen.read_text()
    finally:
        project.close()


def test_shell_transport_file_slot(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-worker", "PROMPT_FILE=$2; shift 2; cat \"$PROMPT_FILE\"\n")
    project = make_cli_project(
        tmp_path,
        profiles_extra="""
[profiles.shell-worker-file]
driver = "cli"
harness = "shell"
executable = "fake-worker"
args = ["--prompt-file", "{prompt_file}"]
prompt_transport = "file"
model = "fake"
class = "strong"
effort = "quick"
capabilities = ["coding"]
""",
        config_replacements=(('profiles = ["shell-worker"]',
                              'profiles = ["shell-worker-file"]',),),
        monkeypatch=monkeypatch,
    )
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "passed"
    finally:
        project.close()


def test_shell_worker_failure_fails_task_with_reason(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-worker", "echo 'blocked: no such module' >&2\nexit 3\n")
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["failed"][0]["task"] == "T001"
        assert "exit 3" in result["failed"][0]["reason"]
        task = dispatch.task_list(project)[0]
        assert task["status"] == "failed"
        assert "blocked" in task["failure_reason"]
    finally:
        project.close()


def test_serial_cap_defers_second_cli_task(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-worker", "exit 0\n")
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"]),
            task_spec("T002", acceptance=["a1"]),
        ]))
        first = dispatch.run_slice(project)
        assert [s["task"] for s in first["started"]] == ["T001"]
        assert [d["task"] for d in first["deferred"]] == ["T002"]
        statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
        assert statuses == {"T001": "passed", "T002": "runnable"}
        second = dispatch.run_slice(project)
        assert [s["task"] for s in second["started"]] == ["T002"]
        assert dispatch.status_data(project)["run"]["status"] == "done"
    finally:
        project.close()


def test_shell_planner_completes_plan_end_to_end(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-planner", f"cat > /dev/null\ncat <<'PLAN'\n{FAKE_PLAN}\nPLAN\n")
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        result = dispatch.plan_route(project)
        assert result["mode"] == "completed"
        assert result["revision"] == 1
        assert result["tasks"] == 1
        assert result["profile"] == "shell-planner"
        statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
        assert statuses == {"T001": "runnable"}
        # the revision records the profile that actually planned it
        goal_row = project.store.goal_active()
        revision = project.store.revision_active(
            project.store.run_for_goal(goal_row.id).id)
        assert revision.planner_profile == "shell-planner"
        # planner attempt recorded with real effort observability
        decisions = project.store.routing_decisions_all()
        assert decisions[-1].role == "planner" and decisions[-1].attempt_id
    finally:
        project.close()


def test_cli_verifier_verdict_contract(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-worker", "exit 0\n")
    make_bin(bindir, "fake-verifier", "echo ORX_VERDICT=pass\n")
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"],
                      verification=["agent: the work is honest"]),
        ]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "verifying"
        verified = dispatch.verify_dispatch(project)
        assert verified["launched"][0]["verdict"] == "pass"
        assert dispatch.status_data(project)["run"]["status"] == "done"
    finally:
        project.close()


def test_cli_verifier_without_verdict_line_fails(tmp_path, bindir, monkeypatch):
    make_bin(bindir, "fake-worker", "exit 0\n")
    make_bin(bindir, "fake-verifier", "echo 'looks fine to me'\n")
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], verification=["agent: check it"]),
        ]))
        dispatch.run_slice(project)
        verified = dispatch.verify_dispatch(project)
        assert verified["launched"][0]["verdict"] == "fail"
        assert dispatch.task_list(project)[0]["status"] == "failed"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Codex adapter (fake `codex` binary)


CODEX_HELP = """usage: codex exec [OPTIONS] PROMPT
  --json            -m, --model <MODEL>    -C, --cd <DIR>
  --output-last-message <FILE>   --output-schema <FILE>
  -s, --sandbox <SANDBOX_MODE>   --ephemeral
"""

CODEX_MODELS = json.dumps({
    "models": [
        {"slug": "fake-codex-model",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "limited-model",
         "supported_reasoning_levels": [{"effort": "low"}]},
    ]
})


# Log the launch flags without the (multiline) trailing prompt argument.
LOG_ARGS = '''n=$#
i=0
line=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -lt "$n" ]; then line="$line $a"; fi
done
echo "$line" >> CALLS
'''


def install_fake_codex(bindir: Path, plan: str = FAKE_PLAN, effort_jsonl: str = "",
                       models_json: str | None = None) -> Path:
    calls = bindir / "codex-calls.txt"
    effort_block = f"cat <<'E'\n{effort_jsonl}\nE\n" if effort_jsonl else ""
    models = models_json if models_json is not None else CODEX_MODELS
    script = f"""#!/bin/sh
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  cat <<'H'
{CODEX_HELP}
H
  exit 0
fi
if [ "$1" = "debug" ]; then
  cat <<'M'
{models}
M
  exit 0
fi
{LOG_ARGS.replace("CALLS", str(calls))}
prev=""
out=""
for arg in "$@"; do
  if [ "$prev" = "--output-last-message" ]; then out="$arg"; fi
  prev="$arg"
done
{effort_block}if [ -n "$out" ]; then cat <<'P' > "$out"
{plan}
P
fi
exit 0
"""
    path = bindir / "codex"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return calls


def make_codex_project(tmp_path, model="fake-codex-model", monkeypatch=None, worker=True):
    extra = f"""
[profiles.codex-x]
driver = "cli"
harness = "codex"
model = "{model}"
class = "frontier"
effort = "deep"
capabilities = ["coding"]
"""
    replacements = []
    if worker:
        replacements.append(('profiles = ["shell-worker"]', 'profiles = ["codex-x"]'))
    else:
        replacements.append(('profiles = ["shell-planner"]', 'profiles = ["codex-x"]'))
    return make_cli_project(tmp_path, profiles_extra=extra,
                            config_replacements=tuple(replacements),
                            monkeypatch=monkeypatch)


def test_codex_probe_and_argv_shape(tmp_path, bindir, monkeypatch):
    calls = install_fake_codex(bindir)
    adapter = adapters.get_adapter("codex")
    report = adapter.probe()
    assert report.ok, report.detail

    project = make_codex_project(tmp_path, monkeypatch=monkeypatch)
    try:
        from orx.config import Profile
        from orx.records import Driver, Effort, Harness, ModelClass
        profile = Profile(
            name="codex-x", driver=Driver.CLI, harness=Harness.CODEX,
            model="fake-codex-model", model_class=ModelClass.FRONTIER,
            effort=Effort.DEEP, capabilities=("coding",),
        )
        scratch = tmp_path / "scratch"
        launch = adapter.build_worker_launch(
            root=tmp_path, scratch=scratch, profile=profile,
            prompt="DO THE WORK", timeout=30)
        argv = launch.argv
        assert argv[0:2] == ["codex", "exec"]
        assert "--json" in argv
        assert argv[argv.index("-m") + 1] == "fake-codex-model"
        assert argv[argv.index("-C") + 1] == str(tmp_path)
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert "--ephemeral" in argv
        assert argv[-1] == "DO THE WORK"  # prompt positional
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        # deep -> high is supported by fake-codex-model: effort flag present
        effort_index = argv.index("-c")
        assert argv[effort_index + 1] == "model_reasoning_effort=high"
    finally:
        project.close()


def test_codex_effort_flag_omitted_when_unsupported(tmp_path, bindir, monkeypatch):
    install_fake_codex(bindir)
    adapter = adapters.get_adapter("codex")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="codex-limited", driver=Driver.CLI, harness=Harness.CODEX,
        model="limited-model", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),  # deep->high not supported
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s2", profile=profile,
        prompt="x", timeout=5)
    assert "-c" not in launch.argv
    assert launch.planned_effort.actual == "provider_default"

    unknown = Profile(
        name="codex-unknown", driver=Driver.CLI, harness=Harness.CODEX,
        model="not-in-catalog", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),
    )
    launch2 = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s3", profile=unknown,
        prompt="x", timeout=5)
    assert "-c" not in launch2.argv


def test_codex_planner_completed_via_last_message(tmp_path, bindir, monkeypatch):
    calls = install_fake_codex(bindir)
    project = make_codex_project(tmp_path, worker=False, monkeypatch=monkeypatch)
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        result = dispatch.plan_route(project)
        assert result["mode"] == "completed"
        assert result["revision"] == 1
        argv_used = calls.read_text().splitlines()[-1].split()
        assert "--output-schema" in argv_used
        assert argv_used[argv_used.index("--output-schema") + 1].endswith("plan-schema.json")
        assert "--output-last-message" in argv_used
        # The schema file must be the strict rewrite (real Codex rejects
        # pydantic's plain output: additionalProperties/required everywhere).
        schema_file = Path(argv_used[argv_used.index("--output-schema") + 1])
        strict = json.loads(schema_file.read_text())
        assert strict["additionalProperties"] is False
        assert sorted(strict["required"]) == sorted(strict["properties"].keys())
        assert strict["$defs"]["PlanTask"]["additionalProperties"] is False
        assert "objective" in strict["$defs"]["PlanTask"]["required"]
    finally:
        project.close()


def test_cli_launches_never_inherit_stdin(tmp_path, bindir, monkeypatch):
    """codex exec (and CLI agents generally) read non-TTY stdin as extra
    prompt input; ORX launches must hand them an explicitly empty stdin."""
    install_fake_codex(bindir)
    adapter = adapters.get_adapter("codex")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="codex-x", driver=Driver.CLI, harness=Harness.CODEX,
        model="fake-codex-model", model_class=ModelClass.FRONTIER,
        effort=Effort.STANDARD, capabilities=("coding",),
    )
    worker = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s1", profile=profile,
        prompt="x", timeout=5)
    assert worker.stdin_text == ""

    make_bin(bindir, "agent", 'echo "--print --output-format --workspace --trust --model effort="; exit 0\n')
    cursor_adapter.reset_caches()
    cursor = adapters.get_adapter("cursor")
    assert cursor.probe().ok
    cursor_profile = Profile(
        name="cursor-x", driver=Driver.CLI, harness=Harness.CURSOR,
        model="fake-cursor", model_class=ModelClass.FRONTIER,
        effort=Effort.STANDARD, capabilities=("coding",),
    )
    launch = cursor.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s2", profile=cursor_profile,
        prompt="x", timeout=5)
    assert launch.stdin_text == ""


def test_codex_catalog_larger_than_stream_limit_still_validated(tmp_path, bindir, monkeypatch):
    """Real `codex debug models` emits >256 KiB (0.160); the default stream
    truncation used to corrupt the JSON and silently degrade every model to
    provider_default. The catalog read must pass a larger limit."""
    big = json.dumps({
        "models": [
            {"slug": "fake-codex-model",
             "description": "pad" * 150000,  # ~600 KiB > STREAM_LIMIT
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        ]
    })
    assert len(big) > 256 * 1024
    install_fake_codex(bindir, models_json=big)
    adapter = adapters.get_adapter("codex")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="codex-x", driver=Driver.CLI, harness=Harness.CODEX,
        model="fake-codex-model", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile,
        prompt="x", timeout=5)
    assert launch.argv[launch.argv.index("-c") + 1] == "model_reasoning_effort=high"
    assert launch.planned_effort.source == "requested_validated"


def test_codex_reported_effort_in_jsonl_wins(tmp_path, bindir, monkeypatch):
    install_fake_codex(bindir, effort_jsonl='{"type":"settings","model_reasoning_effort":"medium"}')
    project = make_codex_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        started = result["started"][0]
        assert started["actual_effort"] == "medium"
        assert started["effort_source"] == "reported"
    finally:
        project.close()


def test_codex_capability_mismatch_fails_attempt(tmp_path, bindir, monkeypatch):
    # Help is missing --output-last-message.
    make_bin(bindir, "codex", 'if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then echo "--json  -m  -C"; exit 0; fi\nexit 1\n')
    project = make_codex_project(tmp_path, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["failed"][0]["task"] == "T001"
        assert "capability_mismatch" in result["failed"][0]["reason"]
        assert dispatch.task_list(project)[0]["status"] == "failed"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Cursor adapter (fake `agent` binary)


AGENT_HELP = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
  example: agent --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'
"""


def install_fake_agent(bindir: Path, plan_in_result: bool = True,
                       models: str | None = None) -> Path:
    calls = bindir / "agent-calls.txt"
    models_block = ""
    if models is not None:
        models_block = f"""if [ "$1" = "--list-models" ]; then
  cat <<'L'
{models}
L
  exit 0
fi
"""
    script = f"""#!/bin/sh
if [ "$1" = "--help" ]; then
  cat <<'H'
{AGENT_HELP}
H
  exit 0
fi
{models_block}{LOG_ARGS.replace("CALLS", str(calls))}
if [ "$1" = "-p" ] || [ "$1" = "--print" ]; then
  cat <<'J'
{{"result": {json.dumps(plan_in_result and FAKE_PLAN or "worker done")}, "model": "fake-cursor[effort=high]"}}
J
  exit 0
fi
exit 0
"""
    path = bindir / "agent"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return calls


def make_cursor_project(tmp_path, model="fake-cursor", force=False, monkeypatch=None,
                        planner=True):
    extra = f"""
[profiles.cursor-x]
driver = "cli"
harness = "cursor"
model = "{model}"
class = "frontier"
effort = "deep"
capabilities = ["coding"]
force = {"true" if force else "false"}
"""
    replacements = (('profiles = ["shell-planner"]', 'profiles = ["cursor-x"]'),) if planner \
        else (('profiles = ["shell-worker"]', 'profiles = ["cursor-x"]'),)
    return make_cli_project(tmp_path, profiles_extra=extra,
                            config_replacements=replacements, monkeypatch=monkeypatch)


def test_cursor_probe_and_model_bracket(tmp_path, bindir, monkeypatch):
    calls = install_fake_agent(bindir)
    adapter = adapters.get_adapter("cursor")
    assert adapter.probe().ok
    project = make_cursor_project(tmp_path, monkeypatch=monkeypatch)
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        result = dispatch.plan_route(project)
        assert result["mode"] == "completed"
        argv_used = calls.read_text().splitlines()[-1].split()
        # $@ excludes the binary name itself; the flags are all present
        assert "--print" in argv_used and "--output-format" in argv_used
        assert argv_used[argv_used.index("--workspace") + 1] == str(tmp_path)
        assert "--trust" in argv_used
        assert argv_used[argv_used.index("--model") + 1] == "fake-cursor[effort=high]"
        assert "--force" not in argv_used and "--api-key" not in argv_used
        assert result["revision"] == 1
    finally:
        project.close()


def test_cursor_force_flag_from_profile(tmp_path, bindir, monkeypatch):
    calls = install_fake_agent(bindir, plan_in_result=False)
    project = make_cursor_project(tmp_path, force=True, planner=False,
                                  monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "passed"
        argv_used = calls.read_text().splitlines()[-1].split()
        assert "--force" in argv_used
    finally:
        project.close()


def test_cursor_bracketed_model_passed_verbatim(tmp_path, bindir, monkeypatch):
    calls = install_fake_agent(bindir)
    model = "fake-cursor[effort=max]"
    project = make_cursor_project(tmp_path, model=model, monkeypatch=monkeypatch)
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        result = dispatch.plan_route(project)
        assert result["mode"] == "completed"
        argv_used = calls.read_text().splitlines()[-1].split()
        assert argv_used[argv_used.index("--model") + 1] == model
    finally:
        project.close()


def test_cursor_worker_effort_validated_only_when_output_confirms(tmp_path, bindir, monkeypatch):
    # The fake echoes fake-cursor[effort=high] in its JSON, so the bracket
    # ORX added is confirmed and recorded as requested_validated.
    install_fake_agent(bindir, plan_in_result=False)
    project = make_cursor_project(tmp_path, planner=False, monkeypatch=monkeypatch)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["actual_effort"] == "high"
        assert result["started"][0]["effort_source"] == "requested_validated"
    finally:
        project.close()


def test_effort_map_shared_definition():
    assert EFFORT_MAP == {"quick": "low", "standard": "medium", "deep": "high", "max": "max"}


def test_codex_real_160_stream_reports_no_effort(tmp_path, bindir, monkeypatch):
    """Confirmation against the captured real run (2026-10-03, codex-cli
    0.160.0): the exec JSONL event stream (thread.started / item.* /
    turn.started / turn.completed) carries NO effort field — not
    model_reasoning_effort, reasoning_effort, or effort, flat or nested.
    The catalog-validated -c value (requested_validated) is therefore the
    recorded outcome; the tolerant scan stays as forward-proofing only."""
    fixture = Path(__file__).parent / "fixtures" / "codex-0.160-worker-exec.jsonl"
    stream = fixture.read_text()
    for line in stream.splitlines():
        event = json.loads(line)
        for key in ("model_reasoning_effort", "reasoning_effort", "effort"):
            assert key not in event
            for nested in ("config", "model"):
                if isinstance(event.get(nested), dict):
                    assert key not in event[nested]

    install_fake_codex(bindir)
    adapter = adapters.get_adapter("codex")
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="codex-x", driver=Driver.CLI, harness=Harness.CODEX,
        model="fake-codex-model", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile,
        prompt="x", timeout=5)
    assert launch.planned_effort.source == "requested_validated"

    class FakeResult:
        exit_code = 0
        stdout = stream
        stderr = ""

    outcome = adapter.effort_outcome(launch, FakeResult())
    assert outcome.actual == "high"
    assert outcome.source == "requested_validated"


def test_cursor_listed_slug_variant_replaces_bracket(tmp_path, bindir, monkeypatch):
    """Real-CLI behavior (2026-10-03): listed models bake effort into the slug
    and REJECT bracket overrides ('Cannot use this model:
    gpt-5.3-codex[effort=high]'). When '<model>-<mapped>' is listed, the
    adapter must rewrite to that slug instead of bracketing."""
    listing = "\n".join([
        "Available models",
        "",
        "fake-cursor - Fake Cursor",
        "fake-cursor-high - Fake Cursor High",
    ])
    install_fake_agent(bindir, plan_in_result=False, models=listing)
    adapter = adapters.get_adapter("cursor")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="cursor-x", driver=Driver.CLI, harness=Harness.CURSOR,
        model="fake-cursor", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),  # deep -> high
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile,
        prompt="x", timeout=5)
    model_arg = launch.argv[launch.argv.index("--model") + 1]
    assert model_arg == "fake-cursor-high"
    assert "[" not in model_arg
    assert launch.planned_effort.actual == "high"
    assert launch.planned_effort.source == "requested_validated"

    class FakeResult:
        exit_code = 0
        stdout = '{"result": "worker done"}'
        stderr = ""

    # The slug itself is the confirmation; no output echo required.
    outcome = adapter.effort_outcome(launch, FakeResult())
    assert (outcome.actual, outcome.source) == ("high", "requested_validated")


def test_cursor_listed_slug_without_variant_stays_bare(tmp_path, bindir, monkeypatch):
    listing = "\n".join([
        "Available models",
        "",
        "fake-cursor - Fake Cursor",
    ])
    install_fake_agent(bindir, plan_in_result=False, models=listing)
    adapter = adapters.get_adapter("cursor")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="cursor-x", driver=Driver.CLI, harness=Harness.CURSOR,
        model="fake-cursor", model_class=ModelClass.FRONTIER,
        effort=Effort.STANDARD, capabilities=("coding",),  # medium variant absent
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile,
        prompt="x", timeout=5)
    model_arg = launch.argv[launch.argv.index("--model") + 1]
    assert model_arg == "fake-cursor"  # bare slug, no bracket guess
    assert launch.planned_effort.source is None


def test_cursor_planner_with_narrative_wrapped_plan(tmp_path, bindir, monkeypatch):
    """Real Cursor runs (2026-10-03) wrap the Plan IR JSON in narrative text
    inside the result payload even when the prompt says JSON only; the
    planner flow must recover the embedded document."""
    wrapped = "I looked at the repo. Here is the plan:\n" + FAKE_PLAN + "\nDone."
    listing = "Available models\n\nfake-cursor - Fake Cursor\nfake-cursor-high - Fake Cursor High"
    install_fake_agent(bindir, plan_in_result=False, models=listing)
    # The fake emits a fixed result payload; rewrite it to wrap the plan.
    agent_path = bindir / "agent"
    agent_path.write_text(agent_path.read_text().replace(
        '"worker done"', json.dumps(wrapped)))
    project = make_cursor_project(tmp_path, monkeypatch=monkeypatch)
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        result = dispatch.plan_route(project)
        assert result["mode"] == "completed"
        assert result["revision"] == 1
    finally:
        project.close()


def test_cursor_suffixed_only_family_rewrites_variant(tmp_path, bindir, monkeypatch):
    """Families like claude-opus-5-5 expose NO bare slug — only
    claude-opus-5-5-low/-medium/-high/... The adapter must still rewrite to
    the catalog-confirmed variant when the base itself is unlisted."""
    listing = "\n".join([
        "Available models",
        "",
        "claude-opus-5-5-low - Opus Low",
        "claude-opus-5-5-medium - Opus",
        "claude-opus-5-5-high - Opus High",
        "claude-opus-5-5-max - Opus Max",
    ])
    install_fake_agent(bindir, plan_in_result=False, models=listing)
    adapter = adapters.get_adapter("cursor")
    assert adapter.probe().ok
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass
    profile = Profile(
        name="cursor-frontier", driver=Driver.CLI, harness=Harness.CURSOR,
        model="claude-opus-5-5", model_class=ModelClass.FRONTIER,
        effort=Effort.DEEP, capabilities=("coding",),  # deep -> high
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile,
        prompt="x", timeout=5)
    model_arg = launch.argv[launch.argv.index("--model") + 1]
    assert model_arg == "claude-opus-5-5-high"
    assert launch.planned_effort.source == "requested_validated"


def test_cli_verifier_reads_verdict_from_json_envelope(tmp_path, bindir, monkeypatch):
    """Real Cursor verifiers (2026-10-03) print ORX_VERDICT inside the
    JSON result envelope; newlines are escaped \\n there, so scanning raw
    stdout line-by-line never sees the marker. The verdict must be read from
    the adapter's extracted message text."""
    make_bin(bindir, "fake-worker", "exit 0\n")
    payload = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "I checked the acceptance criteria.\nORX_VERDICT=pass\n",
    })
    make_bin(bindir, "agent", f"""if [ "$1" = "--help" ]; then
  cat <<'H'
{AGENT_HELP}
H
  exit 0
fi
echo {json.dumps(payload)}
""")
    cursor_adapter.reset_caches()
    project = make_cli_project(
        tmp_path,
        profiles_extra="""
[profiles.cursor-verifier]
driver = "cli"
harness = "cursor"
model = "fake-cursor"
class = "frontier"
effort = "standard"
capabilities = ["coding"]
""",
        config_replacements=(('profiles = ["shell-verifier"]', 'profiles = ["cursor-verifier"]'),),
        monkeypatch=monkeypatch,
    )
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], verification=["agent: check it"]),
        ]))
        dispatch.run_slice(project)
        verified = dispatch.verify_dispatch(project)
        assert verified["launched"][0]["verdict"] == "pass"
        assert dispatch.status_data(project)["run"]["status"] == "done"
    finally:
        project.close()
