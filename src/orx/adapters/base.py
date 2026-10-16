"""Shared adapter vocabulary.

Adapters translate a :class:`Launch` record into argv plus result
interpretation. They never call ``subprocess`` themselves — :mod:`orx.runtime`
is the only process wrapper. One shared effort map, one ``EffortOutcome``
record; no per-adapter copies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from orx.models import ModelSpec
from orx.records import ORXError

# ORX effort -> provider reasoning level (codex model_reasoning_effort,
# cursor model bracket effort=). Defined once; adapters reference this.
# The vocabularies currently coincide; the map stays as the seam so a
# provider that diverges is a one-line change. Adapters validate the mapped
# level against the provider catalog and fall back to provider_default.
EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

EFFORT_PROVIDER_DEFAULT = "provider_default"
EFFORT_SOURCE_REPORTED = "reported"
EFFORT_SOURCE_REQUESTED_VALIDATED = "requested_validated"


class ModelResolutionError(ORXError):
    """A semantic model request cannot be satisfied by the local catalog.

    Raised by adapter resolvers (G008 T003) instead of silently
    substituting another model or dropping an explicitly requested
    dimension. The message always names: the requested identity and
    dimensions, the catalog state it was resolved against, and the
    refresh command that would change that state. Callers record it as
    the attempt failure reason — nothing was launched, so no completion
    or pass result may be written for it.
    """


def effective_model_spec(profile) -> ModelSpec:
    """The spec a resolver consumes for this profile (G008 T003).

    A semantic model table is taken as declared. A bare model string is
    the *id form* of the same contract: a self-contained, opaque id that
    carries the profile effort (the single authoritative source) and is
    never parsed — the legacy behavior expressed through the same
    :class:`~orx.models.ModelSpec` surface, so both forms ride one
    resolution path per adapter.
    """
    if profile.model_spec is not None:
        return profile.model_spec
    return ModelSpec(model_id=profile.model, effort=profile.effort.value)


def identity_token(spec: ModelSpec) -> str:
    """The primary catalog token a declaration names: the id verbatim, or
    the family with the version attached as declared."""
    if spec.model_id is not None:
        return spec.model_id
    token = spec.family or ""
    if spec.version:
        token = f"{token}-{spec.version}"
    return token


def identity_tokens(spec: ModelSpec) -> list[str]:
    """Every catalog spelling a family+version declaration may take.

    The CLIs are not consistent about version spelling: grok-4.7 and
    composer-2.5 keep the dot, claude-opus-5-5 spells 5.5 with a dash.
    Resolution therefore tries the declared spelling first, then the
    dash-normalized one; the declared spelling is always the primary (it
    is what a synthesized parameterized form uses). An id is opaque and
    yields itself only.
    """
    primary = identity_token(spec)
    if spec.model_id is not None or not spec.version:
        return [primary]
    tokens = [primary]
    dashed = f"{spec.family}-{spec.version.replace('.', '-')}"
    if dashed != primary:
        tokens.append(dashed)
    return tokens


def requested_dims(spec: ModelSpec) -> str:
    """The requested identity + dimensions, spelled for diagnostics."""
    dims: list[str] = []
    if spec.model_id is not None:
        dims.append(f"id={spec.model_id}")
    else:
        ident = spec.family or ""
        if spec.version:
            ident = f"{ident}@{spec.version}"
        if spec.provider:
            ident = f"{spec.provider}/{ident}"
        dims.append(f"family={ident}")
    if spec.effort:
        dims.append(f"effort={spec.effort}")
    if spec.speed:
        dims.append(f"speed={spec.speed}")
    if spec.context_tokens is not None:
        dims.append(f"context={spec.context_tokens}")
    if spec.capabilities:
        dims.append("capabilities=" + ",".join(spec.capabilities))
    return " ".join(dims)


def catalog_state(catalog, harness: str) -> str:
    """The catalog a resolution was attempted against, spelled for
    diagnostics: model count, generation stamp, and the family roster."""
    from orx import model_catalog

    if catalog is None:
        return (
            "catalog: none available locally (no persisted snapshot and the"
            " local models probe returned nothing usable)"
        )
    families = list(catalog.families)
    shown = ", ".join(families[:8]) + (", ..." if len(families) > 8 else "")
    if catalog.generation == model_catalog.LAUNCH_DISCOVERY_GENERATION:
        stamp = "in-process discovery"
    else:
        stamp = f"generation {catalog.generation} from {catalog.generated_at or '?'}"
    return (
        f"catalog: {len(catalog.models)} models ({stamp}),"
        f" families: {shown or '(none)'}"
    )


def refresh_hint(harness: str) -> str:
    """The one command that changes the catalog state a diagnostic names."""
    return (
        f"refresh the catalog with `orx agent probe {harness}`,"
        f" inspect it with `orx agent info {harness}`"
    )


@dataclass(frozen=True)
class EffortOutcome:
    requested: str
    actual: str = EFFORT_PROVIDER_DEFAULT
    source: str | None = None  # None (host) | provider_default | requested_validated | reported


@dataclass(frozen=True)
class ProbeReport:
    ok: bool
    detail: str
    missing: tuple[str, ...] = ()


# Why a finished CLI attempt has no usable usage observation. Tokens are
# never invented to fill one of these in.
MISS_ADAPTER_UNSUPPORTED = "adapter_unsupported"  # no usage hook (unsupported capture)
MISS_HARNESS_OMITTED = "harness_omitted"  # hook ran; the stream had no usage event
MISS_MALFORMED = "malformed_output"
MISS_TRUNCATED = "truncated"
MISS_EXECUTION_FAILURE = "execution_failure"
# G009 T010: a host (zcode) attempt whose usage could not be observed
# because no harness session is bound to it — the 21/26 incident gap.
MISS_HOST_SESSION_UNBOUND = "host_session_unbound"
# G009 closeout auto-backfill: stated failures of the zcode-side read at
# attempt closure — the subtree already belongs to another attempt's book,
# or the aggregate could not be read back. Never zero-filled.
MISS_ZCODE_SUBTREE_CONFLICT = "zcode_subtree_conflict"
MISS_ZCODE_UNREADABLE = "zcode_unreadable"
# G010 T001: closure-time reads that must not write a wrong book. A request
# row still mid-flight (status outside the terminal vocabulary) makes every
# sum provisional, so nothing is recorded and the unsettled state is stated
# until a later read finds every row terminal.
MISS_ZCODE_USAGE_UNSETTLED = "zcode_usage_unsettled"
# G010 T001: the attempt already carries a zcode_db observation and a fresh
# read returned different numbers — the stored row stays authoritative and
# no second row is written; a human reconciles from both stated numbers.
MISS_ZCODE_DUPLICATE_BACKFILL = "zcode_duplicate_backfill"


# ---------------------------------------------------------------------------
# G009 T010: one normalized usage vocabulary across providers.
#
# The incident audit (docs/cursor-system-analysis-2026-10-07.md) fixed two
# DIFFERENT provider cache vocabularies that must never be summed blindly:
#
# - cache SUBSET (zcode, codex): the reported input already contains the
#   cache-read prefix. Normalized processing is input + output — adding
#   cache on top would count the same tokens twice. Uncached input is
#   input - cache_read.
# - cache SEPARATE (cursor): input / cacheRead / output are disjoint
#   buckets; a cacheRead far larger than input is the normal shape
#   (a131: 46730/2341/166272). Processing is input + cache_read + output
#   and the input bucket is uncached by definition.
#
# Normalized processing is a VOLUME measure for comparing work, never a
# billing figure: model mix, cache pricing, plan windows, and provider
# weighting are all outside this database. Nothing here converts tokens
# to money, and nothing here is stored — raw provider fields are the
# stored truth; this derivation lives at the presentation layer only.

USAGE_CACHE_SUBSET = "cache_subset"
USAGE_CACHE_SEPARATE = "cache_separate"

HARNESS_CACHE_SEMANTICS: dict[str, str | None] = {
    "zcode": USAGE_CACHE_SUBSET,
    "codex": USAGE_CACHE_SUBSET,
    "cursor": USAGE_CACHE_SEPARATE,
    "shell": None,  # a plain executable reports no model tokens at all
}

NORMALIZED_NOT_BILLING_NOTE = (
    "normalized processing is a per-provider volume measure"
    " (raw fields kept verbatim); it is not billing"
)


def normalized_usage(harness: str | None, input_tokens: int | None,
                     output_tokens: int | None,
                     cached_input_tokens: int | None) -> dict:
    """Raw provider fields plus the derived processing/uncached numbers.

    The raw fields are returned verbatim. The derived fields follow the
    harness's cache semantics above; a field the observation did not
    carry stays None (unknown), never zero, and a derivation whose
    operands are missing is itself None. An harness with no declared
    semantics makes no claim at all (all derived fields None).
    """
    semantics = HARNESS_CACHE_SEMANTICS.get(harness or "")
    processing: int | None = None
    uncached: int | None = None
    if semantics == USAGE_CACHE_SUBSET:
        if input_tokens is not None and output_tokens is not None:
            processing = input_tokens + output_tokens
        if input_tokens is not None and cached_input_tokens is not None:
            difference = input_tokens - cached_input_tokens
            # cached > input contradicts the subset semantics: unknown,
            # never negative, never clipped into a plausible number.
            uncached = difference if difference >= 0 else None
    elif semantics == USAGE_CACHE_SEPARATE:
        uncached = input_tokens
        if (input_tokens is not None and output_tokens is not None
                and cached_input_tokens is not None):
            processing = input_tokens + cached_input_tokens + output_tokens
    return {
        "harness": harness,
        "cache_semantics": semantics,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "normalized_processing_tokens": processing,
        "uncached_input_tokens": uncached,
    }

_TRUNCATION_SUFFIX = re.compile(r"\.\.\. \[truncated at \d+ bytes\]\s*$")


def captured_stdout(run_result) -> str:
    """Text adapters parse. The launch log is a redacted prefix; this is the
    full redacted stream when the runtime kept one, otherwise the logged
    stdout. An empty ``stdout_full`` means the field was not provided."""
    full = getattr(run_result, "stdout_full", None)
    if isinstance(full, str) and full:
        return full
    return getattr(run_result, "stdout", "") or ""


def captured_stderr(run_result) -> str:
    full = getattr(run_result, "stderr_full", None)
    if isinstance(full, str) and full:
        return full
    return getattr(run_result, "stderr", "") or ""


def capture_was_truncated(text: str) -> bool:
    """True when the text being parsed ends in runtime's own truncation
    marker. A mention of truncation earlier in a transcript is not a cut."""
    return _TRUNCATION_SUFFIX.search(text) is not None


def launch_failed(run_result) -> bool:
    if getattr(run_result, "timed_out", False):
        return True
    return getattr(run_result, "exit_code", 0) != 0


def opaque_session_id(value) -> str | None:
    """A harness-emitted session token. Anything else is NULL — never cleaned
    up into an id ORX invented."""
    if not isinstance(value, str) or not value:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


def _token_field(payload: dict, key: str) -> tuple[int | None, bool, bool]:
    """Return (value, present, malformed). Missing stays None. Zero stays
    zero. Booleans, floats, strings, and negatives are malformed — they are
    not coerced and not replaced with zero."""
    if key not in payload:
        return None, False, False
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, True, True
    return value, True, False


def native_usage(payload: dict, *, input_key: str, output_key: str, cached_key: str
                 ) -> tuple[dict | None, str | None]:
    """One harness usage object → a storable observation, or a miss reason.

    ``accuracy=exact`` only when input, output, and cached are all present
    integers. A missing field is NULL, never zero, and the row is ``unknown``
    rather than exact. A present but unusable field drops the whole object
    (``malformed_output``) so the valid siblings cannot look complete.
    """
    if not isinstance(payload, dict):
        return None, MISS_MALFORMED
    parsed = [_token_field(payload, key) for key in (input_key, output_key, cached_key)]
    if any(item[2] for item in parsed):
        return None, MISS_MALFORMED
    if not any(item[1] for item in parsed):
        return None, MISS_HARNESS_OMITTED
    input_tokens, output_tokens, cached_input_tokens = (item[0] for item in parsed)
    complete = all(value is not None for value in (input_tokens, output_tokens, cached_input_tokens))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "source": "native_cli",
        "accuracy": "exact" if complete else "unknown",
    }, None


def miss_without_usage(run_result, text: str, *, malformed: bool = False) -> str:
    """Why a parsed stream produced no storable observation."""
    if capture_was_truncated(text):
        return MISS_TRUNCATED
    if malformed:
        return MISS_MALFORMED
    if launch_failed(run_result):
        return MISS_EXECUTION_FAILURE
    return MISS_HARNESS_OMITTED


@dataclass(frozen=True)
class Capture:
    """Usage and session read from one launch. ``session_ref`` is NULL when
    the harness did not emit a recognized id. ``miss_reason`` is set only
    when ``usage`` is absent."""

    usage: dict | None = None
    session_ref: str | None = None
    miss_reason: str | None = None


# G009 T002: how a harness's WORKER communicates its final delivery. The
# process exit code is ALWAYS transport (the harness controls it; a blocked
# run may still exit 0 — Cursor incident 2026-10-07). This names the
# machine channel dispatch interprets on top of the transport fact:
DELIVERY_CHANNEL_MESSAGE = "message"
#   the worker's final message text carries the structured delivery
#   document (extract_cli_delivery below); cursor and codex. Adapters
#   declaring this channel should also implement
#   ``response_error(launch, run_result) -> str | None`` — the harness
#   response-level error fact (a cursor error envelope, a codex error
#   event), which contradicts a passed delivery.
DELIVERY_CHANNEL_EXIT = "exit"
#   the exit code IS the documented delivery channel; the final-message
#   protocol is not applied (generic executables, scripts — the shell
#   adapter's standing contract "Exit code is the result").


# ---------------------------------------------------------------------------
# CLI final-message delivery extraction (G009 T002).
#
# A CLI worker's process exit code is transport, never a task verdict — the
# harness controls it, and a blocked run may still exit 0 (Cursor incident
# 2026-10-07: a131 delivered status=blocked in a fenced JSON block inside
# the result text while the process exited 0). The worker's delivery is a
# structured document in its final message, parsed here against the ONE
# delivery schema `orx task complete --evidence` enforces (verify owns it;
# this module never copies the rules). Shapes: a fenced ```json block (the
# contract the worker prompt teaches), or a bare JSON object as the whole
# message (a131-compatible tolerance). Natural-language statements are
# never interpreted into a delivery: missing, malformed, and
# self-contradictory documents are explicit protocol problems.

_FENCED_JSON_RE = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```",
                             re.DOTALL | re.IGNORECASE)
_FENCED_BARE_RE = re.compile(r"```[ \t]*\r?\n(.*?)```", re.DOTALL)

_DELIVERY_MISSING = (
    "no machine-readable delivery document found in the worker result text"
    " (expected one JSON object with status/summary/checks/artifacts,"
    " fenced as the final block of the message)"
)


def _delivery_schema_errors(document: dict) -> list[str]:
    """Field errors for one candidate delivery document, straight from the
    schema owner: the document is validated through ``verify.
    load_delivery_evidence`` — the exact path `orx task complete
    --evidence` uses — so a CLI delivery and an evidence file can never
    drift apart. The scratch file is the seam to verify's file-shaped
    validator, not a second copy of the rules."""
    import tempfile

    from orx import verify

    with tempfile.NamedTemporaryFile(
        "w", suffix="-cli-delivery.json", delete=False
    ) as handle:
        handle.write(json.dumps(document))
        scratch = Path(handle.name)
    try:
        _data, errors = verify.load_delivery_evidence(scratch)
    finally:
        scratch.unlink(missing_ok=True)
    return errors


def _iter_json_objects(text: str):
    """Every balanced JSON object embedded in ``text``, with its span.
    Objects the worker quoted for other reasons (check output, markers,
    envelopes) are filtered by the caller — only a ``status`` key makes a
    candidate a delivery attempt."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield index, end, value


def _choose_delivery(documents: list[dict]) -> tuple[dict | None, str | None]:
    """One document from the candidate list: identical-status duplicates
    take the last (the worker's final word); disagreeing statuses are an
    explicit contradiction, never a silent pick."""
    statuses = [document.get("status") for document in documents]
    if len(set(statuses)) > 1:
        return None, (
            "multiple delivery documents disagree on status: "
            + " vs ".join(repr(status) for status in statuses)
            + " — exactly one final delivery is required"
        )
    return documents[-1], None


def extract_cli_delivery(text: str) -> tuple[dict | None, list[str]]:
    """The worker's structured delivery from a CLI worker's final message.

    Returns ``(delivery, problems)``: ``delivery`` is the one valid
    delivery document (None when there is none) and ``problems`` is the
    protocol diagnosis (empty when a delivery was read). Exactly one of
    the two is non-empty.

    Priority: a valid fenced document beats everything; a fenced block
    that fails to parse or validate is a malformed attempt (the worker
    tried the protocol); otherwise bare JSON objects are considered the
    same way; nothing delivery-shaped at all is a missing delivery.
    """
    fences = [m for m in _FENCED_JSON_RE.finditer(text)]
    fences += [m for m in _FENCED_BARE_RE.finditer(text)]
    spans = [(m.start(), m.end()) for m in fences]

    def is_fenced(start: int, end: int) -> bool:
        return any(fs <= start and end <= fe for fs, fe in spans)

    fenced_valid: list[dict] = []
    fenced_errors: list[str] = []
    loose_valid: list[dict] = []
    loose_errors: list[str] = []
    for start, end, obj in _iter_json_objects(text):
        if "status" not in obj:
            continue  # JSON the worker quoted for other reasons
        errors = _delivery_schema_errors(obj)
        if errors:
            target = fenced_errors if is_fenced(start, end) else loose_errors
            target.extend(errors)
        else:
            target_list = fenced_valid if is_fenced(start, end) else loose_valid
            target_list.append(obj)

    # A fence whose content is not parseable JSON at all never reaches the
    # object scan — diagnose it here, or the attempt reads as "missing".
    for match in fences:
        content = match.group(1).strip()
        if not content:
            continue
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            fenced_errors.append(f"fenced JSON block does not parse: {exc}")

    if fenced_valid:
        delivery, problem = _choose_delivery(fenced_valid)
        return (delivery, []) if problem is None else (None, [problem])
    if fenced_errors:
        return None, fenced_errors
    if loose_valid:
        delivery, problem = _choose_delivery(loose_valid)
        return (delivery, []) if problem is None else (None, [problem])
    if loose_errors:
        return None, loose_errors
    return None, [_DELIVERY_MISSING]


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    cwd: Path
    timeout: int
    stdin_text: str | None = None
    last_message_path: Path | None = None
    # What the effort would be recorded as if the child process reports
    # nothing (set by adapters that pass a validated effort flag).
    planned_effort: EffortOutcome | None = None
    label: str = ""  # adapter name, for logs
    # Isolation the adapter actually enforced by passing a real sandbox
    # flag: "read_only" | "workspace_write" (docs/pbv-mapping.md §4.5).
    # None = the adapter makes no isolation claim; host/prompt-only
    # discipline is recorded by the caller, not the adapter.
    sandbox: str | None = None
    # G008 T003: the resolution decision behind the model argument, as
    # ResolvedModel.to_dict() — the full semantic request, the exact CLI
    # slug, the strategy, and the catalog generation it resolved against.
    # None when the adapter resolves no model (shell). Records
    # the REQUEST; observed reality stays with the run result.
    model_resolution: dict | None = None
    # G010 T003: makes THIS launch manageable by the process that created
    # it. Dispatch attaches a cheap read-only predicate (closed over the
    # goal status); runtime polls it while waiting and, when it fires,
    # stops the launch's own process group (SIGTERM, escalating) and waits
    # for the actual exit. Adapters never set this; it is not part of the
    # adapter contract, only of the caller's ownership of its own child.
    stop_check: object = None


class Adapter(Protocol):
    """Every harness adapter implements this surface. M1 P5 adds an optional
    usage_observation(launch, run_result) -> dict | None; None means the
    harness reports nothing (unknown is a legal, stored outcome). M1.2 adds
    optional interpret_capture() -> Capture so a miss records why, and so a
    harness session id can be stored without being invented. G009 T002 adds
    the worker delivery vocabulary: adapters declare ``delivery_channel``
    ("message" when the final message carries the structured delivery —
    cursor/codex; "exit" when the exit code is the delivery channel —
    shell) and message-channel adapters implement optional
    response_error(launch, run_result) -> str | None, the harness
    response-level error that contradicts a passed delivery."""

    harness: str
    delivery_channel: str

    def probe(self) -> ProbeReport:
        """Local capability check (help text / binary presence). Never runs a
        completion. Results are cached in-process."""
        ...

    def build_worker_launch(self, *, root: Path, scratch: Path, profile, prompt: str,
                            timeout: int) -> Launch: ...

    def build_planner_launch(self, *, root: Path, scratch: Path, profile, prompt: str,
                             timeout: int, schema_path: Path | None) -> Launch: ...

    def build_verifier_launch(self, *, root: Path, scratch: Path, profile, prompt: str,
                              timeout: int) -> Launch: ...

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        """Decide requested/actual/effort_source from what the child produced."""
        ...

    def extract_text(self, launch: Launch, run_result) -> str:
        """The agent's message text (stdout, or the --output-last-message file)."""
        ...


def scan_marker(text: str, marker: str) -> str | None:
    """Find the last ``MARKER=value`` line in captured output."""
    value = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker + "="):
            value = stripped[len(marker) + 1:].strip() or value
    return value


# Failure classification patterns (M1 P4). Ordered: first match wins.
# Evidence sources: the post-M0 real runs (cursor "Cannot use this model",
# codex 400 invalid_json_schema, "Connection lost, reconnecting") plus the
# standard CLI error vocabularies of both harnesses.
_CLASSIFICATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("not logged in", "auth_required"),
    ("login required", "auth_required"),
    ("please log in", "auth_required"),
    ("unauthorized", "auth_required"),
    ("api key", "auth_required"),
    ("quota", "quota_exhausted"),
    ("usage limit", "quota_exhausted"),
    ("plan limit", "quota_exhausted"),
    ("billing", "quota_exhausted"),
    ("rate limit", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("429", "rate_limited"),
    ("context length", "context_exceeded"),
    ("context window", "context_exceeded"),
    ("prompt is too long", "context_exceeded"),
    ("cannot use this model", "model_unavailable"),
    ("model not found", "model_unavailable"),
    ("unknown model", "model_unavailable"),
    ("invalid_json_schema", "invalid_request"),
    ("invalid request", "invalid_request"),
    ("invalid_api_key", "invalid_request"),
    ("bad request", "invalid_request"),
    ("connection lost", "temporary_failure"),
    ("reconnect", "temporary_failure"),
    ("temporarily unavailable", "temporary_failure"),
    ("econnreset", "temporary_failure"),
    ("etimedout", "temporary_failure"),
    ("network error", "temporary_failure"),
)


def error_kind_from_text(text: str) -> str:
    """Best-effort ErrorKind for one blob of harness/process text (the
    shared pattern pass `classify_failure` applies to a launch's captured
    output; G009 T004 also feeds it recorded failure reasons, so recovery
    decisions classify history and live launches with one vocabulary).
    Anything unrecognized is PROCESS_FAILURE — the safe default that never
    auto-retries."""
    lowered = (text or "").lower()
    if lowered.strip():
        for needle, kind in _CLASSIFICATION_PATTERNS:
            if needle in lowered:
                return kind
    return "process_failure"


def classify_failure(run_result) -> str:
    """Best-effort ErrorKind for a failed launch. Anything unrecognized is
    PROCESS_FAILURE — the safe default that never auto-retries."""
    return error_kind_from_text(
        captured_stderr(run_result) + "\n" + captured_stdout(run_result)
    )


# G009 T004 — same-session continuation capability, verified 2026-10-07 from
# each CLI's own help output (read-only invocations; no paid calls):
#
#   cursor agent --help      (3.23.12): "--resume [chatId]  Select a session
#                                     to resume" and "--continue  Continue
#                                     previous session"
#   codex exec resume --help (0.160.0): "Resume a previous session by id or
#                                     pick the most recent with --last"
#                                     (works in exec mode: [SESSION_ID]
#                                     [PROMPT])
#   shell                    a plain executable — no session exists at all,
#                                     so there is nothing to resume
#
# zcode is a HOST harness (driver = 'host'): continuation is the host
# session model itself, not a CLI resume flag, so it maps to None
# ("not a CLI-launch question"). Unknown values also map to None — the
# guidance never claims a capability that was not verified.
HARNESS_RESUME_SUPPORT: dict[str, bool] = {
    "cursor": True,
    "codex": True,
    "shell": False,
}

# The exact continuation command per resume-capable harness, from the same
# help output — the retry guidance quotes what the operator can actually run.
HARNESS_RESUME_COMMAND: dict[str, str] = {
    "cursor": "cursor agent --resume <session id>",
    "codex": "codex exec resume <session id>",
}


def harness_resume_supported(harness: str) -> bool | None:
    """Whether a CLI harness can continue an existing session. True/False
    for the verified CLI harnesses; None when the question does not apply
    (host harnesses, unknown names)."""
    return HARNESS_RESUME_SUPPORT.get(harness)


# Quota reset extraction (G005). Codex embeds the exact reset moment in its
# usage-limit failures ("... or try again at Oct 6th, 2026 2:16 AM."); the
# same vocabulary is accepted for the other harnesses ("resets at ...").
_RESET_MARKERS: tuple[str, ...] = (
    "try again at ", "try again on ", "resets at ", "reset at ",
)
_RESET_TIME_FORMATS: tuple[str, ...] = (
    "%b %d, %Y %I:%M %p", "%b %d %Y %I:%M %p",
    "%b %d, %Y %H:%M", "%b %d %Y %H:%M",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
)
# "Oct 6th, 2026" — the ordinal suffix is decoration strptime cannot eat.
_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)


def parse_quota_reset(text: str) -> str | None:
    """ISO-8601 timestamp of the quota reset named in harness failure output,
    when the harness printed one. Harnesses render it in the machine's local
    zone, so a naive parse is anchored to local time. None when no marker has
    a parsable time after it (callers keep "never recovers" semantics)."""
    if not text:
        return None
    lowered = text.lower()
    for marker in _RESET_MARKERS:
        start = lowered.find(marker)
        while start != -1:
            fragment = text[start + len(marker):start + len(marker) + 48]
            # The codex JSONL event ends the sentence inside a quoted string;
            # offer the candidates with JSON decoration stripped too.
            candidates = [
                fragment,
                fragment.split('"')[0],
                fragment.split("}")[0],
            ]
            for candidate in candidates:
                cleaned = _ORDINAL_SUFFIX.sub("", candidate.strip().rstrip('.,;"\\'))
                for fmt in _RESET_TIME_FORMATS:
                    try:
                        moment = datetime.strptime(cleaned, fmt)
                    except ValueError:
                        continue
                    return moment.astimezone().isoformat(timespec="seconds")
            start = lowered.find(marker, start + len(marker))
    return None


def quota_reset_from(run_result) -> str | None:
    """The quota reset time printed in a launch's captured output, if any."""
    return parse_quota_reset(captured_stderr(run_result) + "\n" + captured_stdout(run_result))
