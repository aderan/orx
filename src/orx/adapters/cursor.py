"""Cursor Agent adapter (probed against `agent` 2026.10.01 on 2026-10-03;
effort handling corrected after the first real logged-in run, same day).

- Binary `agent` (never `cursor`, which is the editor shim).
- Required in `agent --help`: --print, --output-format, --workspace, --trust,
  --model. Missing -> capability_mismatch.
- Args: `agent --print --output-format json --workspace <root> --trust
  --model <id> <prompt positional>`. Prompt-as-positional was re-verified by
  the first real logged-in run (2026-10-03); `prompt_transport` stays
  shell-only.
- Effort (real run finding: this CLI rejects bracket overrides on listed
  slugs — "Cannot use this model: gpt-5.3-codex[effort=high]" — every listed
  model bakes effort into the slug, e.g. gpt-5.3-codex-high):
  1. configured model contains `[...]` -> pass verbatim, provider_default.
  2. model listed in `agent --list-models` (local, free) and
     `<model>-<mapped>` also listed -> rewrite to that slug
     (source=requested_validated).
  3. model listed but no `-<mapped>` variant -> bare slug, provider_default.
  4. model NOT listed (a parameterized family per --help) and help mentions
     `effort=` -> `--model '<id>[effort=<mapped>]'`, validated only when the
     process succeeded and the output echoes the bracketed string.
- Never --api-key. --force/--yolo only when the profile sets force = true.
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
    EffortOutcome,
    Launch,
    ProbeReport,
)
from orx.records import ORXError

REQUIRED_FLAGS = ("--print", "--output-format", "--workspace", "--trust", "--model")

_probe_cache: dict[str, ProbeReport] = {}
_models_cache: set[str] | None = None


def _listed_models() -> set[str]:
    """Slugs from `agent --list-models` (local, free). Empty set when the
    catalog cannot be read (then bracket handling falls back to help-text
    detection). Lines look like 'auto - Auto (current, default)'; the slug is
    the first token, ASCII-normalized (the listing contains stray zero-width
    spaces and a 'Available models' header)."""
    global _models_cache
    if _models_cache is None:
        try:
            result = runtime.run_argv(["agent", "--list-models"], cwd=Path.cwd(), timeout=30)
        except ORXError:
            _models_cache = set()
            return _models_cache
        slugs: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts or parts[0] in ("Available", "-"):
                continue
            slug = parts[0].encode("ascii", "ignore").decode()
            if slug:
                slugs.add(slug)
        _models_cache = slugs
    return _models_cache


class CursorAdapter:
    harness = "cursor"

    def probe(self) -> ProbeReport:
        cached = _probe_cache.get("cursor")
        if cached is not None:
            return cached
        if not shutil.which("agent"):
            report = ProbeReport(ok=False, detail="agent not found on PATH", missing=("binary",))
        else:
            try:
                result = runtime.run_argv(["agent", "--help"], cwd=Path.cwd(), timeout=20)
            except ORXError as exc:
                report = ProbeReport(ok=False, detail=str(exc), missing=("help",))
            else:
                text = result.stdout + "\n" + result.stderr
                missing = tuple(f for f in REQUIRED_FLAGS if f not in text)
                report = ProbeReport(
                    ok=not missing,
                    detail="flags present" if not missing else f"missing {list(missing)}",
                    missing=missing,
                )
        _probe_cache["cursor"] = report
        return report

    def _model_arg(self, profile, help_text: str) -> tuple[str, EffortOutcome]:
        requested = profile.effort.value
        if "[" in profile.model:
            # User-supplied bracket: pass verbatim, never rewrite.
            return profile.model, EffortOutcome(requested=requested)
        mapped = EFFORT_MAP.get(requested)
        listed = _listed_models()
        if mapped:
            variant = f"{profile.model}-{mapped}"
            base_listed = profile.model in listed
            if variant in listed:
                # Effort is baked into the slug on this CLI; pick the variant
                # (works whether the base slug itself is listed or the family
                # only exposes suffixed variants, e.g. claude-opus-5-5-high).
                return variant, EffortOutcome(
                    requested=requested, actual=mapped,
                    source=EFFORT_SOURCE_REQUESTED_VALIDATED,
                )
            if base_listed:
                return profile.model, EffortOutcome(requested=requested)
            if "effort=" in help_text:
                # Unlisted, no catalog variant: a parameterized family per
                # --help (e.g. 'claude-opus-4-8[context=1m,effort=high]').
                bracketed = f"{profile.model}[effort={mapped}]"
                return bracketed, EffortOutcome(
                    requested=requested, actual=mapped,
                    source=EFFORT_SOURCE_REQUESTED_VALIDATED,
                )
        return profile.model, EffortOutcome(requested=requested)

    def _launch(self, *, root: Path, profile, prompt: str, timeout: int) -> Launch:
        help_result = runtime.run_argv(["agent", "--help"], cwd=Path.cwd(), timeout=20)
        help_text = help_result.stdout + "\n" + help_result.stderr
        model_arg, effort = self._model_arg(profile, help_text)
        argv = [
            "agent", "--print", "--output-format", "json",
            "--workspace", str(root), "--trust",
            "--model", model_arg,
        ]
        if profile.force:
            argv.append("--force")
        argv.append(prompt)
        # Empty stdin on purpose: the prompt is positional, and a CLI agent
        # left with an inherited non-TTY stdin may try to read more input
        # (Codex does; observed in a real run, 2026-10-03).
        return Launch(argv=argv, cwd=root, timeout=timeout,
                      planned_effort=effort, label=f"cursor:{profile.name}",
                      stdin_text="")

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None) -> Launch:
        # Cursor has no output-schema flag; the schema travels inside the prompt.
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        planned = launch.planned_effort or EffortOutcome(requested="standard")
        if planned.source == EFFORT_SOURCE_REQUESTED_VALIDATED:
            bracketed = next(
                (a for a in launch.argv if "[" in a and "effort=" in a), None
            )
            if bracketed is not None:
                # Bracket form: believe it only when the CLI echoed it back.
                if run_result.exit_code == 0 and bracketed in (run_result.stdout + run_result.stderr):
                    return planned
            elif run_result.exit_code == 0:
                # Catalog-confirmed slug variant (e.g. gpt-5.3-codex-high):
                # the slug itself is the confirmation.
                return planned
        return EffortOutcome(requested=planned.requested, actual=EFFORT_PROVIDER_DEFAULT, source=None)

    def usage_observation(self, launch: Launch, run_result) -> dict | None:
        """The --print JSON envelope's usage block (camelCase keys,
        verified in a real run 2026-10-03)."""
        text = run_result.stdout.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return None
        return {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "cached_input_tokens": usage.get("cacheReadTokens"),
            "source": "native_cli",
            "accuracy": "exact",
        }

    def extract_text(self, launch: Launch, run_result) -> str:
        text = run_result.stdout.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return run_result.stdout
        if isinstance(data, dict):
            for key in ("result", "text", "message", "content", "response", "output"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list):
                    parts = [
                        item.get("text", "") for item in value
                        if isinstance(item, dict) and isinstance(item.get("text"), str)
                    ]
                    if parts:
                        return "\n".join(parts)
        return run_result.stdout


def reset_caches() -> None:
    """Test hook: forget cached probes and the model listing."""
    global _models_cache
    _probe_cache.clear()
    _models_cache = None
