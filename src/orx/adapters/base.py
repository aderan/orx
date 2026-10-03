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
    """Every harness adapter implements this surface."""

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
