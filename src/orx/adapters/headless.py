"""Shared framing for harnesses with a terminal JSON event."""

from __future__ import annotations

import json
import shutil

from .base import ProbeReport


def probe_headless(harness: str) -> ProbeReport:
    from orx.probes import HARNESSES, probe_argv

    spec = HARNESSES[harness]
    if not shutil.which(spec.binary):
        return ProbeReport(False, f"{spec.binary} not found on PATH")
    code, help_text = probe_argv(list(spec.help_argv))
    missing = tuple(flag for flag in spec.required_flags if flag not in help_text)
    if code != 0 or missing:
        return ProbeReport(False, f"{spec.binary} help lacks the required headless interface", missing)
    return ProbeReport(True, f"{spec.binary} exposes the headless interface")


def events(stream: str):
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def terminal_result(stream: str) -> dict | None:
    """Only the last frame can close the invocation."""
    line = stream.strip().rsplit("\n", 1)[-1]
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("type") == "result" else None
