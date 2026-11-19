"""ZCode headless CLI with a per-launch provider selection file."""

from __future__ import annotations

import json
import tempfile

from .base import (
    Capture, DELIVERY_CHANNEL_MESSAGE, EffortOutcome, Launch, ModelResolutionError,
    captured_stdout, effective_model_spec, native_model_spec, identity_token, miss_without_usage, native_usage,
)
from .headless import events, probe_headless, terminal_result


def _model_requests(stream, result):
    """Read completed main-session provider requests for this exact turn."""
    for event in events(stream):
        if (event.get("type") != "session.updated"
                or event.get("sessionId") != result.get("sessionId")
                or event.get("turnId") != result.get("turnId")):
            continue
        payload = event.get("payload")
        if (isinstance(payload, dict) and payload.get("type") == "model_request_completed"
                and payload.get("modelRequestSessionType") == "main"):
            yield payload


def _reported_model(requests):
    models = {
        f"{item['providerId']}/{item['modelId']}" for item in requests
        if isinstance(item.get("providerId"), str) and isinstance(item.get("modelId"), str)
    }
    return next(iter(models)) if len(models) == 1 else None


class ZCodeAdapter:
    harness = "zcode"
    delivery_channel = DELIVERY_CHANNEL_MESSAGE

    def probe(self):
        return probe_headless(self.harness)

    def _launch(self, *, root, scratch, profile, prompt, timeout):
        spec = effective_model_spec(profile)
        model = identity_token(spec)
        if spec.model_id is None and spec.provider:
            model = f"{spec.provider}/{model}"
        provider, separator, model_id = model.partition("/")
        if not separator or not provider or not model_id or spec.speed is not None or spec.context_tokens is not None:
            raise ModelResolutionError("ZCode requires an explicit provider/model id")
        effort = native_model_spec(profile).effort
        if (profile.tier is not None or profile.native_effort is not None) and effort not in ("low", "high", "max"):
            raise ModelResolutionError(f"ZCode native effort {effort!r} unsupported (low | high | max)")
        selection = {"providerId": provider, "modelId": model_id,
                     "options": {"reasoningLevel": effort}}
        scratch.mkdir(parents=True, exist_ok=True)
        # Allocate one file per launch. A verifier retry or concurrent probe
        # cannot overwrite the selection of an in-flight process.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="zcode-provider-", suffix=".json", dir=scratch, delete=False,
        ) as file:
            json.dump({"schemaVersion": 1, "config": {
                "providerConfigRules": {"providerRules": []},
                "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
                "defaultModelSelection": selection,
            }}, file)
            config_path = file.name
        return Launch(
            argv=["env", f"ZCODE_PERSONAL_PROVIDER_CONFIG_FILE={config_path}", "zcode",
                  "--cwd", str(root), "--mode", "yolo", "--output-format", "stream-json",
                  "--prompt", prompt],
            cwd=root, timeout=timeout, stdin_text="", label=f"zcode:{profile.name}",
            planned_effort=EffortOutcome(profile.effort.value, actual=""),
            model_resolution={"spec": spec.to_dict(), "cli_slug": model,
                              "strategy": "isolated_provider_selection",
                              "requested_effort": profile.effort.value, "native_effort": effort},
        )

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None):
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt,
                            timeout=timeout)

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout):
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt,
                            timeout=timeout)

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout):
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt,
                            timeout=timeout)

    def extract_text(self, launch, run_result):
        stream = captured_stdout(run_result)
        result = terminal_result(stream)
        projection = result.get("projection") if result else None
        if not isinstance(projection, dict) or projection.get("status") != "idle":
            return ""
        selection = launch.model_resolution
        for item in _model_requests(stream, result):
            if item.get("modelId") and item.get("providerId"):
                reported = f"{item['providerId']}/{item['modelId']}"
                if reported != selection["cli_slug"]:
                    return ""
            call = item.get("modelCall")
            reasoning = call.get("reasoning") if isinstance(call, dict) else None
            requested = reasoning.get("requestedLevel") if isinstance(reasoning, dict) else None
            if requested not in (None, "", "unknown", selection["native_effort"]):
                return ""
        text = result.get("response")
        return text if isinstance(text, str) else ""

    def effort_outcome(self, launch, run_result):
        stream = captured_stdout(run_result)
        result = terminal_result(stream) or {}
        levels = set()
        for item in _model_requests(stream, result):
            call = item.get("modelCall")
            reasoning = call.get("reasoning") if isinstance(call, dict) else None
            level = reasoning.get("effectiveLevel") if isinstance(reasoning, dict) else None
            if isinstance(level, str) and level not in ("", "unknown"):
                levels.add(level)
        if len(levels) == 1:
            return EffortOutcome(launch.planned_effort.requested, next(iter(levels)), "reported")
        return launch.planned_effort

    def interpret_capture(self, launch, run_result):
        stream = captured_stdout(run_result)
        result = terminal_result(stream)
        if not result:
            started = next((row for row in events(stream) if row.get("type") == "turn.started"), {})
            session = started.get("sessionId")
            model = _reported_model(_model_requests(stream, started)) if session else None
            return Capture(
                session_ref=session if isinstance(session, str) else None,
                miss_reason=miss_without_usage(run_result, stream, malformed=bool(stream) and not started),
                actual_model=model,
            )
        session = result.get("sessionId")
        usage, miss = native_usage(
            result.get("usage", {}), input_key="inputTokens",
            output_key="outputTokens", cached_key="cacheReadTokens",
        )
        model = _reported_model(_model_requests(stream, result))
        return Capture(usage, session if isinstance(session, str) else None, miss, model)
