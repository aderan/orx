"""Codex adapter (probed against codex 0.160.0 on 2026-10-03).

- Required in `codex exec --help`: --json, -m, -C, --output-last-message.
  Anything missing -> the attempt fails with capability_mismatch.
- Model resolution (G008 T003): :func:`resolve_codex_model` consumes the
  profile's :class:`~orx.models.ModelSpec` plus the shared refreshable
  catalog (:func:`orx.model_catalog.launch_catalog`, fed by
  `codex debug models` — local, free, no completion) and produces the
  exact `-m` slug and, when the catalog's ``supported_reasoning_levels``
  contain the mapped effort, the ``-c model_reasoning_effort=<level>``
  flag (source=requested_validated). A bare-string profile is the id
  form of the same spec and keeps the legacy contract verbatim: unknown
  slug or unsupported level -> pass nothing, record provider_default.
  A *semantic* declaration is strict: an unknown model, an unsupported
  effort level, or an explicit speed/context/capability dimension (codex
  exec has no launch channel for them) is a ModelResolutionError naming
  the requested dimensions, the catalog state, and the refresh command —
  never a silent substitution.
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

from orx import model_catalog
from orx import runtime
from orx.adapters.base import (
    DELIVERY_CHANNEL_MESSAGE,
    EFFORT_MAP,
    EFFORT_PROVIDER_DEFAULT,
    EFFORT_SOURCE_REQUESTED_VALIDATED,
    EFFORT_SOURCE_REPORTED,
    MISS_HARNESS_OMITTED,
    Capture,
    EffortOutcome,
    Launch,
    ModelResolutionError,
    ProbeReport,
    captured_stdout,
    catalog_state,
    effective_model_spec,
    identity_token,
    identity_tokens,
    miss_without_usage,
    native_usage,
    opaque_session_id,
    refresh_hint,
    requested_dims,
)
from orx.models import ModelSpec, ResolvedModel
from orx.records import ORXError

REQUIRED_FLAGS = ("--json", "-m", "-C", "--output-last-message")

_probe_cache: dict[str, ProbeReport] = {}

# The effort dimension's launch channel: a -c config key that never
# appears in --help; the catalog's own effort levels are the signal.
EFFORT_FLAG_KEY = "model_reasoning_effort"


def _help_text() -> str:
    result = runtime.run_argv(["codex", "exec", "--help"], cwd=Path.cwd(), timeout=20)
    if result.exit_code != 0 and not result.stdout:
        raise ORXError(f"codex probe failed: exit {result.exit_code}")
    return result.stdout + "\n" + result.stderr


def _resolution_error(detail: str, spec: ModelSpec, catalog) -> ModelResolutionError:
    return ModelResolutionError(
        f"codex model resolution failed: {detail};"
        f" requested: {requested_dims(spec)};"
        f" {catalog_state(catalog, 'codex')}; {refresh_hint('codex')}"
    )


def resolve_codex_model(spec: ModelSpec, *, catalog,
                        strict: bool) -> ResolvedModel:
    """Resolve one model declaration against the local codex catalog.

    ``strict`` is true for semantic (model-table) declarations and false
    for bare legacy strings. Legacy keeps today's contract exactly: an
    unknown model or an unsupported effort level passes no effort flag
    and records provider_default. Strict raises
    :class:`ModelResolutionError` for an unknown model, an effort level
    the catalog does not list, or an explicit speed/context/capability
    dimension — codex exec has no channel for those, and a semantic
    request is never silently narrowed.
    """
    token = identity_token(spec)
    entry = None
    if catalog is not None:
        for candidate in identity_tokens(spec):
            for row in catalog.models:
                if row.slug == candidate:
                    entry = row
                    token = candidate
                    break
            if entry is not None:
                break
    generation = catalog.generation if catalog is not None else None
    if entry is None:
        if strict:
            raise _resolution_error(
                f"model {token!r} is not in the local codex catalog",
                spec, catalog,
            )
        return ResolvedModel(
            spec=spec, cli_slug=token, strategy="bare_unlisted",
            bracket_params=(), catalog_generation=generation,
        )
    if strict:
        if spec.speed is not None:
            raise _resolution_error(
                "codex exec has no launch channel for the speed dimension"
                " (speed tiers are catalog metadata only)", spec, catalog,
            )
        if spec.context_tokens is not None:
            raise _resolution_error(
                "codex exec has no launch channel for the context dimension"
                " (context windows are catalog metadata only)", spec, catalog,
            )
        if spec.capabilities:
            raise _resolution_error(
                "capability requirements cannot be verified against the"
                " codex model catalog", spec, catalog,
            )
    mapped = EFFORT_MAP.get(spec.effort) if spec.effort else None
    if mapped is not None and mapped in entry.efforts:
        return ResolvedModel(
            spec=spec, cli_slug=entry.slug, strategy="catalog_levels",
            bracket_params=((EFFORT_FLAG_KEY, mapped),),
            catalog_generation=generation,
        )
    if entry.efforts and mapped is not None and strict:
        raise _resolution_error(
            f"model {token!r} supports reasoning levels {list(entry.efforts)};"
            " the requested effort is not among them", spec, catalog,
        )
    # No reasoning levels for this model (the dimension does not exist),
    # no effort requested, or the legacy form's tolerated pass-through.
    return ResolvedModel(
        spec=spec, cli_slug=entry.slug, strategy="catalog_slug",
        bracket_params=(), catalog_generation=generation,
    )


def _effort_flag(resolved: ResolvedModel, requested: str) -> tuple[list[str], EffortOutcome]:
    level = dict(resolved.bracket_params).get(EFFORT_FLAG_KEY)
    if level is not None:
        return (
            ["-c", f"{EFFORT_FLAG_KEY}={level}"],
            EffortOutcome(
                requested=requested, actual=level,
                source=EFFORT_SOURCE_REQUESTED_VALIDATED,
            ),
        )
    return [], EffortOutcome(requested=requested)


def permission_argv_axes(profile, role: str = "worker") -> dict:
    """The permission-relevant argv axes :class:`CodexAdapter` will pass
    for this profile and role (G009 T007 fingerprint mirror).

    The adapter is the source of truth; tests pin this mirror to the real
    launch argv. Sandbox is an explicit argv fact for codex (-s per role);
    the bypass flag is NEVER passed (force/yolo stays its own axis, value
    false as an argv fact); workspace trust is not an argv axis at all —
    it lives in the CLI's per-project config."""
    sandbox = "workspace-write" if role == "worker" else "read-only"
    return {
        "trust": None,
        "force_yolo": False,
        "sandbox": sandbox,
    }


class CodexAdapter:
    harness = "codex"
    # codex exec's exit code is transport; the worker's delivery is the
    # structured document in the final message (--output-last-message).
    delivery_channel = DELIVERY_CHANNEL_MESSAGE

    def response_error(self, launch: Launch, run_result) -> str | None:
        """The event stream's own error fact — distinct from the process
        exit and from the worker's delivery. An ``error`` event in the
        JSONL stream says the execution errored harness-side; a passed
        delivery in the final message alongside it is a contradiction."""
        for line in captured_stdout(run_result).splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "error":
                message = event.get("message")
                detail = (
                    f": {message}" if isinstance(message, str) and message else ""
                )
                return f"codex event stream reports an error event{detail}"
        return None

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

    def _base_argv(self, *, root: Path, profile, prompt: str, scratch: Path,
                   sandbox: str = "workspace-write") -> tuple[list[str], EffortOutcome, Path, ResolvedModel]:
        catalog = model_catalog.launch_catalog("codex")
        resolved = resolve_codex_model(
            effective_model_spec(profile), catalog=catalog,
            strict=profile.model_spec is not None,
        )
        effort_args, effort = _effort_flag(resolved, profile.effort.value)
        scratch.mkdir(parents=True, exist_ok=True)
        last_message = scratch / "last-message.txt"
        argv = [
            "codex", "exec",
            "--json",
            "-m", resolved.cli_slug,
            "-C", str(root),
            "-s", sandbox,
            "--ephemeral",
            "--output-last-message", str(last_message),
            *effort_args,
            prompt,
        ]
        return argv, effort, last_message, resolved

    def _launch(self, *, root, scratch, profile, prompt, timeout, sandbox: str) -> Launch:
        argv, effort, last_message, resolved = self._base_argv(
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
                      sandbox=sandbox.replace("-", "_"),
                      model_resolution=resolved.to_dict())

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
    """Test hook: forget cached probes and the shared model catalog."""
    _probe_cache.clear()
    model_catalog.invalidate()
