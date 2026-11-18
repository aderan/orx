"""Headless protocol fixtures taken from local CLI observations with IDs redacted."""

import json
from pathlib import Path

import pytest

from orx import adapters, verify
from orx.config import load_profiles
from orx.runtime import CommandResult


FIXTURES = Path(__file__).parent / "fixtures" / "stage2-cli"


def make_launch(tmp_path, harness, model, effort):
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(f"""schema_version = 1
[profiles.verifier]
driver = "cli"
harness = "{harness}"
model = "{model}"
class = "strong"
effort = "{effort}"
capabilities = ["coding"]
""")
    adapter = adapters.get_adapter(harness)
    launch = adapter.build_verifier_launch(
        root=tmp_path, scratch=tmp_path, profile=load_profiles(profiles)["verifier"],
        prompt="check", timeout=5,
    )
    return adapter, launch


def output(stream):
    return CommandResult(command="fixture", exit_code=0, stdout=stream,
                         stderr="ORX_VERDICT=fail", duration_sec=0.01, timed_out=False)


def test_claude_verifier_reads_native_schema_result(tmp_path):
    model = "zcode/GLM-5.3[1m]"
    adapter, launch = make_launch(tmp_path, "claude", model, "low")
    assert json.loads(launch.argv[launch.argv.index("--json-schema") + 1]) == verify.VERDICT_SCHEMA
    assert launch.argv[launch.argv.index("--model") + 1] == model
    assert launch.argv[launch.argv.index("--effort") + 1] == "low"

    run = output((FIXTURES / "claude-verifier.jsonl").read_text())
    assert verify.parse_agent_verdict(adapter.extract_verdict_text(launch, run)) == (
        "pass", "protocol smoke",
    )
    capture = adapter.interpret_capture(launch, run)
    assert capture.actual_model == model
    assert capture.session_ref == "claude_fixture"
    assert capture.usage["input_tokens"] == 14218
    assert not adapter.effort_outcome(launch, run).actual


def test_zcode_selects_model_per_launch_and_keeps_observed_facts(tmp_path):
    model = "account:bigmodel-individual-coding-plan/GLM-5.3-Flash"
    adapter, launch = make_launch(tmp_path, "zcode", model, "low")
    selection = json.loads(Path(launch.argv[1].split("=", 1)[1]).read_text())
    assert selection["config"]["defaultModelSelection"] == {
        "providerId": "account:bigmodel-individual-coding-plan", "modelId": "GLM-5.3-Flash",
        "options": {"reasoningLevel": "low"},
    }

    run = output((FIXTURES / "zcode-flash.jsonl").read_text())
    assert adapter.extract_text(launch, run) == "ORX_STREAM"
    observed = adapter.interpret_capture(launch, run)
    assert observed.actual_model == model
    assert observed.session_ref == "sess_fixture"
    assert observed.usage["source"] == "native_cli"
    assert not adapter.effort_outcome(launch, run).actual


def test_zcode_rejects_result_from_a_registry_fallback(tmp_path):
    model = "account:bigmodel-individual-coding-plan/GLM-5.3"
    adapter, launch = make_launch(tmp_path, "zcode", model, "high")
    run = output((FIXTURES / "zcode-flash.jsonl").read_text())
    assert adapter.extract_text(launch, run) == ""
    assert adapter.interpret_capture(launch, run).actual_model.endswith("/GLM-5.3-Flash")


@pytest.mark.parametrize("message", [
    '{"verdict":"pass","reason":null}',
    '{"verdict":"fail","verdict":"pass","reason":"conflict"}',
    'ORX_VERDICT=pass\nORX_VERDICT=fail',
    'ORX_VERDICT=fail',
])
def test_ambiguous_or_incomplete_verdicts_are_rejected(message):
    assert verify.parse_agent_verdict(message) is None


def test_explicit_structured_rejection_is_a_verdict():
    assert verify.parse_agent_verdict('{"verdict":"fail","reason":"missing check"}') == (
        "fail", "missing check",
    )
