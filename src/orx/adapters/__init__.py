"""Adapter registry: harness name -> adapter instance."""

from __future__ import annotations

from orx.records import ORXError

from .base import (  # re-exported vocabulary
    EFFORT_MAP,
    EffortOutcome,
    Launch,
    ProbeReport,
)
from .shell import ShellAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter

_ADAPTERS = {
    "shell": ShellAdapter(),
    "codex": CodexAdapter(),
    "cursor": CursorAdapter(),
}


def get_adapter(harness: str) -> object:
    adapter = _ADAPTERS.get(harness)
    if adapter is None:
        raise ORXError(f"no adapter for harness {harness!r}")
    return adapter


__all__ = [
    "EFFORT_MAP", "EffortOutcome", "Launch", "ProbeReport",
    "get_adapter", "ShellAdapter", "CodexAdapter", "CursorAdapter",
]
