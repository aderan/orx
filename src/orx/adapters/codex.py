"""Codex adapter (probed against codex 0.160.0 on 2026-10-03).

- Required in `codex exec --help`: --json, -m, -C, --output-last-message.
  Anything missing -> the attempt fails with capability_mismatch.
- Effort: map quick->low, standard->medium, deep->high, max->max, then check
  the mapped level against `supported_reasoning_levels` for the configured
  model in `codex debug models` (local catalog, no completion). Supported ->
  pass `-c model_reasoning_effort=<level>`; unsupported or model unknown ->
  pass nothing and record provider_default.
- Sandbox (per-role contract, docs/pbv-mapping.md §4.5): worker launches run
  `-s workspace-write`; planner and verifier launches run `-s read-only`
  (planning and verification read the tree, they must not mutate it). The
  enforced mode is declared on Launch.sandbox ("workspace_write" /
  "read_only"). Never --dangerously-bypass...; --ephemeral so ORX runs do
  not depend on Codex session files; prompt is the positional argument;
  planner adds `--output-schema <file>`.
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
    MISS_HARNESS_OMITTED,
    Capture,
    EffortOutcome,
    Launch,
    ProbeReport,
    captured_stdout,
    miss_without_usage,
    native_usage,
    opaque_session_id,
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

    def _base_argv(self, *, root: Path, profile, prompt: str, scratch: Path,
                   sandbox: str = "workspace-write") -> tuple[list[str], EffortOutcome, Path]:
        effort_args, effort = self._effort_flag(profile)
        scratch.mkdir(parents=True, exist_ok=True)
        last_message = scratch / "last-message.txt"
        argv = [
            "codex", "exec",
            "--json",
            "-m", profile.model,
            "-C", str(root),
            "-s", sandbox,
            "--ephemeral",
            "--output-last-message", str(last_message),
            *effort_args,
            prompt,
        ]
        return argv, effort, last_message

    def _launch(self, *, root, scratch, profile, prompt, timeout, sandbox: str) -> Launch:
        argv, effort, last_message = self._base_argv(
            root=root, profile=profile, prompt=prompt, scratch=scratch, sandbox=sandbox)
        return Launch(argv=argv, cwd=root, timeout=timeout,
                      last_message_path=last_message, planned_effort=effort,
                      label=f"codex:{profile.name}",
                      # codex exec reads non-TTY stdin as additional prompt
                      # input (observed in a real run, 2026-10-03); never let
                      # it inherit the controller's stdin.
                      stdin_text="",
                      # The CLI flag value is the isolation claim
                      # (read-only -> read_only; docs/pbv-mapping.md §4.5).
                      sandbox=sandbox.replace("-", "_"))

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        # Workers write code: they keep workspace-write (§4.5).
        return self._launch(root=root, scratch=scratch, profile=profile,
                            prompt=prompt, timeout=timeout,
                            sandbox="workspace-write")

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None) -> Launch:
        # Planning reads the tree and must not mutate it (§4.5).
        launch = self._launch(root=root, scratch=scratch, profile=profile,
                              prompt=prompt, timeout=timeout, sandbox="read-only")
        if schema_path is not None:
            # Insert before the positional prompt.
            argv = launch.argv[:-1] + ["--output-schema", str(schema_path), launch.argv[-1]]
            launch = replace(launch, argv=argv)
        return launch

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        # Verification inspects the work; it must not change it (§4.5).
        return self._launch(root=root, scratch=scratch, profile=profile,
                            prompt=prompt, timeout=timeout, sandbox="read-only")

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        planned = launch.planned_effort or EffortOutcome(requested="medium")
        for line in captured_stdout(run_result).splitlines():
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

    def interpret_capture(self, launch: Launch, run_result) -> Capture:
        """Last ``turn.completed`` usage object only — earlier turns are not
        summed. ``thread.started.thread_id`` is the session when it is an
        opaque string. A numeric id, a different field, or no id is NULL."""
        text = captured_stdout(run_result)
        session: str | None = None
        session_locked = False
        best: dict | None = None
        broken_turn = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{") or not stripped.endswith("}"):
                if "turn.completed" in stripped and stripped.startswith("{"):
                    broken_turn = True
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                if "turn.completed" in stripped:
                    broken_turn = True
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started" and not session_locked:
                session_locked = True
                session = opaque_session_id(event.get("thread_id"))
            if event.get("type") != "turn.completed" or "usage" not in event:
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                best = usage  # last usage object; earlier turns are not added in
                broken_turn = False
            else:
                best = None
                broken_turn = True
        if best is not None:
            observation, reason = native_usage(
                best, input_key="input_tokens", output_key="output_tokens",
                cached_key="cached_input_tokens",
            )
            if observation is not None:
                return Capture(usage=observation, session_ref=session)
            if reason == MISS_HARNESS_OMITTED:
                reason = miss_without_usage(run_result, text)
            return Capture(session_ref=session, miss_reason=reason)
        return Capture(
            session_ref=session,
            miss_reason=miss_without_usage(run_result, text, malformed=broken_turn),
        )

    def usage_observation(self, launch: Launch, run_result) -> dict | None:
        """turn.completed.usage from the JSONL stream. None when nothing
        storable was emitted. The last usage object wins; turns are not summed."""
        return self.interpret_capture(launch, run_result).usage

    def extract_text(self, launch: Launch, run_result) -> str:
        if launch.last_message_path and launch.last_message_path.exists():
            return launch.last_message_path.read_text()
        return captured_stdout(run_result)


def reset_caches() -> None:
    """Test hook: forget cached probes and catalog reads."""
    global _catalog_cache
    _probe_cache.clear()
    _catalog_cache = None
