"""Codex adapter (probed against codex 0.160.0 on 2026-10-03).

- Required in `codex exec --help`: --json, -m, -C, --output-last-message.
  Anything missing -> the attempt fails with capability_mismatch.
- Effort: map quick->low, standard->medium, deep->high, max->max, then check
  the mapped level against `supported_reasoning_levels` for the configured
  model in `codex debug models` (local catalog, no completion). Supported ->
  pass `-c model_reasoning_effort=<level>`; unsupported or model unknown ->
  pass nothing and record provider_default.
- Sandbox: `-s workspace-write`; never --dangerously-bypass...; --ephemeral so
  ORX runs do not depend on Codex session files; prompt is the positional
  argument; planner adds `--output-schema <file>`.
- actual_effort: the effort reported in the JSONL event stream if present
  (source=reported); otherwise the catalog-validated value ORX passed
  (source=requested_validated); otherwise provider_default. CONFIRMED against
  a real run (2026-10-03, codex-cli 0.160.0, fixture
  tests/fixtures/codex-0.160-worker-exec.jsonl): the exec event stream
  (thread.started / item.* / turn.started / turn.completed) reports NO effort
  field, so requested_validated is the strongest source on 0.160; the
  tolerant scan (model_reasoning_effort / reasoning_effort / effort, flat or
  nested) stays as forward-proofing for future codex versions.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

from orx import runtime
from orx.adapters.base import (
    EFFORT_MAP,
    EFFORT_PROVIDER_DEFAULT,
    EFFORT_SOURCE_REQUESTED_VALIDATED,
    EFFORT_SOURCE_REPORTED,
    EffortOutcome,
    Launch,
    ProbeReport,
)
from orx.records import ORXError

REQUIRED_FLAGS = ("--json", "-m", "-C", "--output-last-message")

_probe_cache: dict[str, ProbeReport] = {}
_catalog_cache: list | None = None


def _help_text() -> str:
    result = runtime.run_argv(["codex", "exec", "--help"], cwd=Path.cwd(), timeout=20)
    if result.exit_code != 0 and not result.stdout:
        raise ORXError(f"codex probe failed: exit {result.exit_code}")
    return result.stdout + "\n" + result.stderr


class CodexAdapter:
    harness = "codex"

    def probe(self) -> ProbeReport:
        cached = _probe_cache.get("codex")
        if cached is not None:
            return cached
        if not shutil.which("codex"):
            report = ProbeReport(ok=False, detail="codex not found on PATH", missing=("binary",))
        else:
            try:
                text = _help_text()
            except ORXError as exc:
                report = ProbeReport(ok=False, detail=str(exc), missing=("help",))
            else:
                missing = tuple(f for f in REQUIRED_FLAGS if f not in text)
                report = ProbeReport(
                    ok=not missing,
                    detail="flags present" if not missing else f"missing {list(missing)}",
                    missing=missing,
                )
        _probe_cache["codex"] = report
        return report

    def _supported_levels(self, model: str) -> set[str] | None:
        """Levels from the local catalog; None when the model is unknown or
        the catalog cannot be read. The catalog exceeds the default 256 KiB
        stream limit (verified in a real run, 2026-10-03: truncation silently
        corrupted the JSON and degraded every model to provider_default), so
        this read passes a larger limit."""
        global _catalog_cache
        if _catalog_cache is None:
            result = runtime.run_argv(
                ["codex", "debug", "models"], cwd=Path.cwd(), timeout=30,
                stream_limit=4 * 1024 * 1024,
            )
            try:
                _catalog_cache = json.loads(result.stdout).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                _catalog_cache = []
        for entry in _catalog_cache:
            if entry.get("slug") == model:
                return {level.get("effort") for level in entry.get("supported_reasoning_levels", [])}
        return None

    def _effort_flag(self, profile) -> tuple[list[str], EffortOutcome]:
        requested = profile.effort.value
        mapped = EFFORT_MAP.get(requested)
        if mapped is None:
            return [], EffortOutcome(requested=requested)
        levels = self._supported_levels(profile.model)
        if levels and mapped in levels:
            return (
                ["-c", f"model_reasoning_effort={mapped}"],
                EffortOutcome(
                    requested=requested,
                    actual=mapped,
                    source=EFFORT_SOURCE_REQUESTED_VALIDATED,
                ),
            )
        return [], EffortOutcome(requested=requested)

    def _base_argv(self, *, root: Path, profile, prompt: str, scratch: Path) -> tuple[list[str], EffortOutcome, Path]:
        effort_args, effort = self._effort_flag(profile)
        scratch.mkdir(parents=True, exist_ok=True)
        last_message = scratch / "last-message.txt"
        argv = [
            "codex", "exec",
            "--json",
            "-m", profile.model,
            "-C", str(root),
            "-s", "workspace-write",
            "--ephemeral",
            "--output-last-message", str(last_message),
            *effort_args,
            prompt,
        ]
        return argv, effort, last_message

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        argv, effort, last_message = self._base_argv(root=root, profile=profile, prompt=prompt, scratch=scratch)
        return Launch(argv=argv, cwd=root, timeout=timeout,
                      last_message_path=last_message, planned_effort=effort,
                      label=f"codex:{profile.name}",
                      # codex exec reads non-TTY stdin as additional prompt
                      # input (observed in a real run, 2026-10-03); never let
                      # it inherit the controller's stdin.
                      stdin_text="")

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None) -> Launch:
        launch = self.build_worker_launch(root=root, scratch=scratch, profile=profile,
                                          prompt=prompt, timeout=timeout)
        if schema_path is not None:
            # Insert before the positional prompt.
            argv = launch.argv[:-1] + ["--output-schema", str(schema_path), launch.argv[-1]]
            launch = replace(launch, argv=argv)
        return launch

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self.build_worker_launch(root=root, scratch=scratch, profile=profile,
                                        prompt=prompt, timeout=timeout)

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        planned = launch.planned_effort or EffortOutcome(requested="medium")
        for line in run_result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("model_reasoning_effort", "reasoning_effort", "effort"):
                value = event.get(key) if isinstance(event, dict) else None
                if isinstance(value, str) and value:
                    return EffortOutcome(
                        requested=planned.requested, actual=value,
                        source=EFFORT_SOURCE_REPORTED,
                    )
                if isinstance(event, dict):
                    config = event.get("config") or event.get("model")
                    if isinstance(config, dict):
                        nested = config.get(key)
                        if isinstance(nested, str) and nested:
                            return EffortOutcome(
                                requested=planned.requested, actual=nested,
                                source=EFFORT_SOURCE_REPORTED,
                            )
        if planned.source == EFFORT_SOURCE_REQUESTED_VALIDATED:
            return planned
        return EffortOutcome(requested=planned.requested, actual=EFFORT_PROVIDER_DEFAULT, source=None)

    def usage_observation(self, launch: Launch, run_result) -> dict | None:
        """turn.completed.usage from the JSONL stream (native, exact). None
        when the stream carries no usage (truncation, older CLI)."""
        best: dict | None = None
        for line in run_result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{") or "turn.completed" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                best = usage  # keep the LAST turn.completed
        if not best:
            return None
        return {
            "input_tokens": best.get("input_tokens"),
            "output_tokens": best.get("output_tokens"),
            "cached_input_tokens": best.get("cached_input_tokens"),
            "source": "native_cli",
            "accuracy": "exact",
        }

    def extract_text(self, launch: Launch, run_result) -> str:
        if launch.last_message_path and launch.last_message_path.exists():
            return launch.last_message_path.read_text()
        return run_result.stdout


def reset_caches() -> None:
    """Test hook: forget cached probes and catalog reads."""
    global _catalog_cache
    _probe_cache.clear()
    _catalog_cache = None
