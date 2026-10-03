"""Shared adapter vocabulary.

Adapters translate a :class:`Launch` record into argv plus result
interpretation. They never call ``subprocess`` themselves — :mod:`orx.runtime`
is the only process wrapper. One shared effort map, one ``EffortOutcome``
record; no per-adapter copies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# ORX effort -> provider reasoning level (codex model_reasoning_effort,
# cursor model bracket effort=). Defined once; adapters reference this.
EFFORT_MAP: dict[str, str] = {
    "quick": "low",
    "standard": "medium",
    "deep": "high",
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


class Adapter(Protocol):
    """Every harness adapter implements this surface. M1 P5 adds an optional
    usage_observation(launch, run_result) -> dict | None; None means the
    harness reports nothing (unknown is a legal, stored outcome)."""

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
    text = (run_result.stderr or "") + "\n" + (run_result.stdout or "")
    lowered = text.lower()
    if lowered.strip():
        for needle, kind in _CLASSIFICATION_PATTERNS:
            if needle in lowered:
                return kind
    return "process_failure"
