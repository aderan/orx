"""Claude Code's non-interactive stream and native structured output."""

from __future__ import annotations

import json

from orx.verify import VERDICT_SCHEMA

from .base import (
    Capture, DELIVERY_CHANNEL_MESSAGE, EffortOutcome, Launch, ModelResolutionError,
    captured_stdout, effective_model_spec, native_model_spec, identity_token, miss_without_usage, native_usage,
)
from .headless import events, probe_headless, terminal_result


class ClaudeAdapter:
    harness = "claude"
    delivery_channel = DELIVERY_CHANNEL_MESSAGE

    def probe(self):
        return probe_headless(self.harness)

    def _launch(self, *, root, profile, prompt, timeout, schema=None):
        spec = effective_model_spec(profile)
        if spec.speed is not None or spec.context_tokens is not None:
            raise ModelResolutionError("Claude Code expects an explicit model id for speed/context variants")
        model = identity_token(spec)
        effort = native_model_spec(profile).effort
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ModelResolutionError(f"Claude Code native effort {effort!r} unsupported")
        argv = [
            "claude", "--print", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions", "--model", model, "--effort", effort,
        ]
        if schema is not None:
            argv += ["--json-schema", schema]
        return Launch(
            argv=[*argv, "--", prompt], cwd=root, timeout=timeout, stdin_text="",
            label=f"claude:{profile.name}", planned_effort=EffortOutcome(profile.effort.value, actual=""),
            model_resolution={"spec": spec.to_dict(), "cli_slug": model,
                              "strategy": "native_flag", "requested_effort": profile.effort.value,
                              "native_effort": effort},
        )

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None):
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout,
                            schema=schema_path.read_text() if schema_path else None)

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout):
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout):
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout,
                            schema=json.dumps(VERDICT_SCHEMA))

    def extract_text(self, launch, run_result):
        result = terminal_result(captured_stdout(run_result))
        if not result or result.get("subtype") != "success" or result.get("is_error"):
            return ""
        output = result.get("structured_output")
        if isinstance(output, dict):
            return json.dumps(output)
        text = result.get("result")
        return text if isinstance(text, str) else ""

    def extract_verdict_text(self, launch, run_result):
        result = terminal_result(captured_stdout(run_result))
        if not result or result.get("subtype") != "success" or result.get("is_error"):
            return ""
        output = result.get("structured_output")
        return json.dumps(output) if isinstance(output, dict) else ""

    def effort_outcome(self, launch, run_result):
        # The CLI accepts the flag. The result doesn't attest upstream effort.
        return launch.planned_effort

    def interpret_capture(self, launch, run_result):
        stream = captured_stdout(run_result)
        result = terminal_result(stream)
        if not result:
            initialized = next((row for row in events(stream)
                                if row.get("type") == "system" and row.get("subtype") == "init"), {})
            session = initialized.get("session_id")
            return Capture(
                session_ref=session if isinstance(session, str) else None,
                miss_reason=miss_without_usage(run_result, stream, malformed=bool(stream) and not initialized),
            )
        session = result.get("session_id")
        models = result.get("modelUsage")
        model = next(iter(models)) if isinstance(models, dict) and len(models) == 1 else None
        usage, miss = native_usage(
            result.get("usage", {}), input_key="input_tokens",
            output_key="output_tokens", cached_key="cache_read_input_tokens",
        )
        # Claude also reports cache creation separately. Keep the raw read
        # count and avoid deriving totals under a different cache convention.
        return Capture(usage, session if isinstance(session, str) else None, miss, model)
