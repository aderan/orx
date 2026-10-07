"""Cursor Agent adapter (probed against `agent` 2026.10.01 on 2026-10-03;
effort handling corrected after the first real logged-in run, same day).

- Binary `agent` (never `cursor`, which is the editor shim).
- Required in `agent --help`: --print, --output-format, --workspace, --trust,
  --model. Missing -> capability_mismatch.
- Args: `agent --print --output-format json --workspace <root> --trust
  --model <id> <prompt positional>`. Prompt-as-positional was re-verified by
  the first real logged-in run (2026-10-03); `prompt_transport` stays
  shell-only.
- Model resolution (G008 T003): one resolver (:func:`resolve_cursor_model`)
  consumes the profile's :class:`~orx.models.ModelSpec` plus the shared
  refreshable catalog (:func:`orx.model_catalog.launch_catalog`) and produces
  the exact ``--model`` argument. A bare-string profile is the id form of the
  same spec (opaque, never parsed), so legacy slugs ride the same ladder:
  1. user-supplied bracket (``[`` in the id) -> pass verbatim, never rewrite.
  2. the family is enumerated in the catalog -> pick the entry matching the
     requested effort variant (families that only expose suffixed variants,
     e.g. claude-opus-5-5-high, resolve the same way; ``-fast`` entries
     satisfy an explicit fast speed). The slug itself is the effort
     confirmation (source=requested_validated).
  2b. version policy across catalog evolution (G008 T004): a *fixed*
     family+version request that no enumerated family spells (after the
     dash/dot twins and the CLI's cursor- prefix) fails explicitly when the
     catalog enumerates the same family stem at a different version — never
     a bracket guess, never the other version. A *floating* declaration
     (no version, or ``version = "latest"``) resolves against whichever
     version of the stem the catalog enumerates; more than one distinct
     version is an ambiguity diagnostic, never a guess.
  3. the family is NOT enumerated (a parameterized family per --help) and
     bracket syntax is evidenced -> synthesize ``<id>[context=…,effort=…,
     fast=…]`` from the requested dimensions, validated only when the
     process succeeded and the output echoes the bracketed string.
  4. unenumerated with no bracket evidence: an id keeps the bare passthrough
     of the legacy behavior; a semantic family declaration is a resolution
     failure (diagnostic, never a silent substitution).
  A known enumerated family never masquerades as bracket support: a missing
  variant of an enumerated family is a diagnostic, not a bracket guess (this
  CLI rejects bracket overrides on listed slugs — "Cannot use this model:
  gpt-5.3-codex[effort=high]", real run 2026-10-03).
- Never --api-key. --force/--yolo only when the profile sets force = true.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from orx import model_catalog
from orx import runtime
from orx.adapters.base import (
    EFFORT_MAP,
    EFFORT_PROVIDER_DEFAULT,
    EFFORT_SOURCE_REQUESTED_VALIDATED,
    MISS_HARNESS_OMITTED,
    Capture,
    EffortOutcome,
    Launch,
    ModelResolutionError,
    ProbeReport,
    captured_stderr,
    captured_stdout,
    catalog_state,
    effective_model_spec,
    identity_token,
    identity_tokens,
    launch_failed,
    miss_without_usage,
    native_usage,
    opaque_session_id,
    refresh_hint,
    requested_dims,
)
from orx.models import ModelSpec, ResolvedModel
from orx.records import ORXError

REQUIRED_FLAGS = ("--print", "--output-format", "--workspace", "--trust", "--model")

_probe_cache: dict[str, ProbeReport] = {}

# Bracket parameters inside a user-supplied model string, for the record.
_BRACKET_PARAM_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,\]]+)")


def _resolution_error(detail: str, spec: ModelSpec, catalog) -> ModelResolutionError:
    return ModelResolutionError(
        f"cursor model resolution failed: {detail};"
        f" requested: {requested_dims(spec)};"
        f" {catalog_state(catalog, 'cursor')}; {refresh_hint('cursor')}"
    )


def _matching_entries(catalog, token: str) -> list:
    """Catalog entries a token names: the exact slug, or the decoded family."""
    if catalog is None:
        return []
    return [
        entry for entry in catalog.models
        if entry.slug == token or (entry.family or entry.slug) == token
    ]


# The CLI's own provider prefix on some families (cursor-grok-4.6-*): CLI
# spelling, not family identity — stripped before comparing family stems.
_CURSOR_PREFIX = "cursor-"


def _stem_families(catalog, stem: str) -> dict[str, list]:
    """Enumerated families equivalent to ``stem`` modulo the cursor- prefix
    and a trailing version tail, as ``{base family: [entries]}``.

    Catalog evolution shows up here: the same family stem (``grok``) is
    enumerated at a moving version (cursor-grok-4.6-*, then grok-4.7-*).
    A floating declaration may name whichever version the catalog has; a
    fixed one conflicts with every other version. Prefixed and unprefixed
    spellings of the same base are one family (equivalent targets), so they
    share one bucket and never count as ambiguity against each other.
    """
    out: dict[str, list] = {}
    if catalog is None or not stem:
        return out
    for entry in catalog.models:
        family = entry.family or entry.slug
        base = (
            family[len(_CURSOR_PREFIX):]
            if family.startswith(_CURSOR_PREFIX) else family
        )
        if base == stem or base.startswith(stem + "-"):
            out.setdefault(base, []).append(entry)
    return out


def _render_context(tokens: int) -> str:
    """Bracket spelling of a context size (the CLI's own examples use 1m)."""
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}m"
    if tokens % 1_000 == 0:
        return f"{tokens // 1_000}k"
    return str(tokens)


def _bracket_keys(catalog, help_text: str) -> set[str]:
    """Parameter names the evidenced bracket syntax accepts: union of the
    catalog's parameterized evidence (parsed by the probe from --help and
    the listing) and a fresh parse of the help text this launch fetched."""
    keys: set[str] = set()
    if (
        catalog is not None
        and catalog.parameterized is not None
        and catalog.parameterized.available
    ):
        keys.update(catalog.parameterized.param_keys)
    if help_text:
        evidence = model_catalog.parse_parameterized_syntax(
            {"agent --help": help_text}
        )
        if evidence.available:
            keys.update(evidence.param_keys)
    return keys


def _resolve_enumerated(spec: ModelSpec, mapped: str | None,
                        entries: list, catalog) -> ResolvedModel:
    """Pick the catalog entry satisfying the requested dimensions.

    Explicit context or capability dimensions have no channel on an
    enumerated slug (this CLI rejects bracket overrides on listed models),
    a declared speed must be honored, and a family with effort variants
    must offer the requested level — a missing variant of a known family
    is a diagnostic, never a bracket guess and never a silent level
    change. A family with no effort variants at all (auto, composer-2.5)
    has no effort dimension to drop: the honest outcome is
    provider_default.
    """
    if spec.context_tokens is not None:
        raise _resolution_error(
            "the context dimension cannot be expressed for an enumerated"
            " model (this CLI rejects bracket overrides on listed slugs)",
            spec, catalog,
        )
    if spec.capabilities:
        raise _resolution_error(
            "capability requirements cannot be verified against the cursor"
            " model catalog", spec, catalog,
        )
    has_effort_variants = any(entry.effort is not None for entry in entries)
    pool = entries
    if has_effort_variants:
        pool = [entry for entry in entries if entry.effort == mapped]
        if mapped is None:
            # No effort requested: the bare member of the family, else any.
            pool = [entry for entry in entries if entry.effort is None] or entries
    if not pool:
        available = sorted(
            {entry.effort for entry in entries if entry.effort is not None}
        )
        raise _resolution_error(
            f"the family is enumerated with effort variants {available or ['(none)']}"
            " and the requested effort is not among them; ORX will not"
            " silently pick another level or disguise the missing variant"
            " as bracket support",
            spec, catalog,
        )
    if spec.speed is not None:
        sped = [entry for entry in pool if spec.speed in entry.speeds]
        if not sped:
            raise _resolution_error(
                f"no enumerated variant of this family offers speed"
                f" {spec.speed!r} for the requested effort", spec, catalog,
            )
        pool = sped
    else:
        # Undeclared speed resolves to the standard variant when one exists.
        standard = [entry for entry in pool if "standard" in entry.speeds]
        pool = standard or pool
    entry = pool[0]
    if has_effort_variants and entry.effort is not None:
        return ResolvedModel(
            spec=spec, cli_slug=entry.slug, strategy="catalog_variant",
            bracket_params=(), catalog_generation=(
                catalog.generation if catalog is not None else None
            ),
        )
    return ResolvedModel(
        spec=spec, cli_slug=entry.slug, strategy="bare_enumerated",
        bracket_params=(), catalog_generation=(
            catalog.generation if catalog is not None else None
        ),
    )


def _resolve_parameterized(spec: ModelSpec, mapped: str | None, token: str,
                           help_text: str, catalog) -> ResolvedModel:
    """The bracket channel for families the catalog does not enumerate.

    Parameters are composed from the requested dimensions in the CLI's own
    example order (context, effort, fast) and only for keys the evidenced
    syntax accepts. Without bracket evidence an id keeps the legacy bare
    passthrough; a semantic family declaration fails loudly.
    """
    generation = catalog.generation if catalog is not None else None
    if spec.capabilities:
        raise _resolution_error(
            "capability requirements cannot be verified against the cursor"
            " model catalog", spec, catalog,
        )
    params: list[tuple[str, str]] = []
    if spec.context_tokens is not None:
        params.append(("context", _render_context(spec.context_tokens)))
    if mapped is not None:
        params.append(("effort", mapped))
    if spec.speed == "fast":
        params.append(("fast", "true"))
    elif spec.speed == "standard":
        params.append(("fast", "false"))
    if not params:
        if spec.model_id is not None:
            return ResolvedModel(
                spec=spec, cli_slug=token, strategy="bare_unlisted",
                bracket_params=(), catalog_generation=generation,
            )
        raise _resolution_error(
            "the family is not enumerated in the catalog and no requested"
            " dimension uses the parameter channel; ORX cannot confirm the"
            " CLI supports it", spec, catalog,
        )
    keys = _bracket_keys(catalog, help_text)
    if not keys:
        if (
            spec.model_id is not None
            and spec.speed is None
            and spec.context_tokens is None
        ):
            # Legacy shape: unlisted slug, only the effort dimension, no
            # bracket evidence in --help — pass the bare slug and let the
            # CLI judge (recorded as provider_default, never validated).
            return ResolvedModel(
                spec=spec, cli_slug=token, strategy="bare_unlisted",
                bracket_params=(), catalog_generation=generation,
            )
        raise _resolution_error(
            "no parameterized bracket syntax is evidenced by --help or the"
            " catalog, so the requested dimensions cannot be encoded",
            spec, catalog,
        )
    unsupported = [key for key, _ in params if key not in keys]
    if unsupported:
        raise _resolution_error(
            f"bracket evidence supports only {sorted(keys)}; cannot encode"
            f" {unsupported}", spec, catalog,
        )
    slug = token + "[" + ",".join(f"{key}={value}" for key, value in params) + "]"
    return ResolvedModel(
        spec=spec, cli_slug=slug, strategy="bracket_synthesis",
        bracket_params=tuple(params), catalog_generation=generation,
    )


def resolve_cursor_model(spec: ModelSpec, *, help_text: str,
                         catalog) -> ResolvedModel:
    """Resolve one semantic model declaration against the cursor catalog.

    The single resolution path for every role (worker/planner/verifier)
    and both declaration forms (semantic family table, bare id string).
    Raises :class:`ModelResolutionError` — naming the requested
    dimensions, the catalog state, and the refresh command — whenever the
    request cannot be satisfied; never substitutes another model and
    never drops an explicitly declared dimension.
    """
    token = identity_token(spec)
    if "[" in token:
        # User-supplied bracket: pass verbatim, never rewrite. The spec
        # keeps the string opaque; record its parameters for the trail.
        inside = token[token.index("[") + 1:token.rindex("]")]
        params = tuple(_BRACKET_PARAM_RE.findall(inside))
        return ResolvedModel(
            spec=spec, cli_slug=token, strategy="verbatim_bracket",
            bracket_params=params, catalog_generation=None,
        )
    mapped = EFFORT_MAP.get(spec.effort) if spec.effort else None
    # Candidate spellings: the declared token, its dash-normalized twin,
    # and (family form only) the CLI's own cursor- prefixed spellings —
    # exact matches always win in this order.
    candidates = identity_tokens(spec)
    if spec.model_id is None:
        candidates += [
            f"cursor-{candidate}" for candidate in identity_tokens(spec)
            if f"cursor-{candidate}" not in candidates
        ]
    entries: list = []
    for candidate in candidates:
        entries = _matching_entries(catalog, candidate)
        if entries:
            token = candidate
            break
    if entries:
        return _resolve_enumerated(spec, mapped, entries, catalog)
    if spec.model_id is None:
        # Version policy across catalog evolution (family-form only; an id
        # is opaque and keeps the legacy ladder):
        # * fixed version, stem enumerated elsewhere -> explicit failure
        #   naming the offered versions (never the other fixed version,
        #   never a bracket guess on a family the catalog clearly
        #   enumerates);
        # * floating version, exactly one stem family -> resolve against
        #   it (the floating policy is what tracks a version bump);
        # * floating version, several stem families -> ambiguity
        #   diagnostic naming them (never a guess).
        versions = _stem_families(catalog, spec.family or "")
        if versions:
            offered = sorted(versions)
            if spec.version is not None:
                raise _resolution_error(
                    f"family {spec.family!r} is not enumerated at the"
                    f" requested version {spec.version!r}; the catalog"
                    f" enumerates {offered}; ORX will not silently select"
                    " another fixed version (pin the offered version, or"
                    ' declare model.version = "latest" to float with the'
                    " catalog)", spec, catalog,
                )
            if len(versions) > 1:
                raise _resolution_error(
                    f"the floating family request is ambiguous: the catalog"
                    f" enumerates {offered}; ORX will not guess among"
                    " versions (pin one)", spec, catalog,
                )
            return _resolve_enumerated(
                spec, mapped, next(iter(versions.values())), catalog)
    return _resolve_parameterized(spec, mapped, token, help_text, catalog)


def planned_effort(resolved: ResolvedModel, requested: str) -> EffortOutcome:
    """The EffortOutcome a resolution plans for, before the run decides.

    Catalog-variant slugs and synthesized brackets carry the mapped effort
    as requested_validated — the former confirmed by the slug itself once
    the run succeeds, the latter only by an echoed bracket in successful
    output (:meth:`CursorAdapter.effort_outcome`). Everything else stays
    provider_default.
    """
    bracket_effort = dict(resolved.bracket_params).get("effort")
    if resolved.strategy == "catalog_variant":
        mapped = EFFORT_MAP.get(resolved.spec.effort) if resolved.spec.effort else None
        if mapped is not None:
            return EffortOutcome(
                requested=requested, actual=mapped,
                source=EFFORT_SOURCE_REQUESTED_VALIDATED,
            )
    if resolved.strategy == "bracket_synthesis" and bracket_effort is not None:
        return EffortOutcome(
            requested=requested, actual=bracket_effort,
            source=EFFORT_SOURCE_REQUESTED_VALIDATED,
        )
    return EffortOutcome(requested=requested)


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

    def _launch(self, *, root: Path, profile, prompt: str, timeout: int) -> Launch:
        help_result = runtime.run_argv(["agent", "--help"], cwd=Path.cwd(), timeout=20)
        help_text = help_result.stdout + "\n" + help_result.stderr
        catalog = model_catalog.launch_catalog("cursor")
        spec = effective_model_spec(profile)
        resolved = resolve_cursor_model(spec, help_text=help_text, catalog=catalog)
        argv = [
            "agent", "--print", "--output-format", "json",
            "--workspace", str(root), "--trust",
            "--model", resolved.cli_slug,
        ]
        if profile.force:
            argv.append("--force")
        argv.append(prompt)
        # Empty stdin on purpose: the prompt is positional, and a CLI agent
        # left with an inherited non-TTY stdin may try to read more input
        # (Codex does; observed in a real run, 2026-10-03).
        return Launch(
            argv=argv, cwd=root, timeout=timeout,
            planned_effort=planned_effort(resolved, profile.effort.value),
            label=f"cursor:{profile.name}",
            stdin_text="",
            model_resolution=resolved.to_dict(),
        )

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None) -> Launch:
        # Cursor has no output-schema flag; the schema travels inside the prompt.
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, profile=profile, prompt=prompt, timeout=timeout)

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        planned = launch.planned_effort or EffortOutcome(requested="medium")
        if planned.source == EFFORT_SOURCE_REQUESTED_VALIDATED:
            bracketed = next(
                (a for a in launch.argv if "[" in a and "effort=" in a), None
            )
            echoed = captured_stdout(run_result) + captured_stderr(run_result)
            if bracketed is not None:
                # Bracket form: believe it only when the CLI echoed it back.
                if run_result.exit_code == 0 and bracketed in echoed:
                    return planned
            elif run_result.exit_code == 0:
                # Catalog-confirmed slug variant (e.g. gpt-5.3-codex-high):
                # the slug itself is the confirmation.
                return planned
        return EffortOutcome(requested=planned.requested, actual=EFFORT_PROVIDER_DEFAULT, source=None)

    def interpret_capture(self, launch: Launch, run_result) -> Capture:
        """Envelope ``usage`` (camelCase, values kept as reported — cached
        may exceed input) and ``session_id``. ``request_id`` is not a session.
        Partial token sets stay ``unknown``; missing fields stay NULL."""
        text = captured_stdout(run_result)
        stripped = text.strip()
        try:
            data = json.loads(stripped) if stripped else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            malformed = bool(stripped) and stripped[0] in "{[" and not launch_failed(run_result)
            return Capture(miss_reason=miss_without_usage(
                run_result, text, malformed=malformed,
            ))
        session = opaque_session_id(data.get("session_id"))
        if "usage" not in data:
            return Capture(session_ref=session, miss_reason=miss_without_usage(run_result, text))
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return Capture(
                session_ref=session,
                miss_reason=miss_without_usage(run_result, text, malformed=True),
            )
        observation, reason = native_usage(
            usage, input_key="inputTokens", output_key="outputTokens",
            cached_key="cacheReadTokens",
        )
        if observation is not None:
            return Capture(usage=observation, session_ref=session)
        if reason == MISS_HARNESS_OMITTED:
            reason = miss_without_usage(run_result, text)
        return Capture(session_ref=session, miss_reason=reason)

    def usage_observation(self, launch: Launch, run_result) -> dict | None:
        """The --print JSON envelope's usage block (camelCase keys,
        verified in a real run 2026-10-03)."""
        return self.interpret_capture(launch, run_result).usage

    def extract_text(self, launch: Launch, run_result) -> str:
        text = captured_stdout(run_result).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return captured_stdout(run_result)
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
        return captured_stdout(run_result)


def reset_caches() -> None:
    """Test hook: forget cached probes and the shared model catalog."""
    _probe_cache.clear()
    model_catalog.invalidate()
