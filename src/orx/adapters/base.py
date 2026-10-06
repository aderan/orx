"""Shared adapter vocabulary.

Adapters translate a :class:`Launch` record into argv plus result
interpretation. They never call ``subprocess`` themselves — :mod:`orx.runtime`
is the only process wrapper. One shared effort map, one ``EffortOutcome``
record; no per-adapter copies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

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


class Adapter(Protocol):
    """Every harness adapter implements this surface. M1 P5 adds an optional
    usage_observation(launch, run_result) -> dict | None; None means the
    harness reports nothing (unknown is a legal, stored outcome). M1.2 adds
    optional interpret_capture() -> Capture so a miss records why, and so a
    harness session id can be stored without being invented."""

    harness: str

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


def classify_failure(run_result) -> str:
    """Best-effort ErrorKind for a failed launch. Anything unrecognized is
    PROCESS_FAILURE — the safe default that never auto-retries."""
    text = captured_stderr(run_result) + "\n" + captured_stdout(run_result)
    lowered = text.lower()
    if lowered.strip():
        for needle, kind in _CLASSIFICATION_PATTERNS:
            if needle in lowered:
                return kind
    return "process_failure"


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
