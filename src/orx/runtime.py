"""The only subprocess wrapper.

argv/shell command, fixed cwd, timeout, cancellation, output capture with
redaction and truncation. Verification commands run through here; future
adapters translate a Launch record into argv and call this module instead of
subprocess themselves.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STREAM_LIMIT = 256 * 1024  # per stream

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)(api_key|token)(\s*=\s*)[^\s&;'\"]+"), r"\1\2[REDACTED]"),
)

# Verification commands are constrained: cwd pinned to the project root,
# timeout, and a denylist. Submitting the plan is the authorization to run the
# listed checks; ORX never invents commands.
FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[\s;&|])sudo(\s|$)"), "sudo"),
    (re.compile(r"(^|\s)mkfs(\s|\.|$)"), "mkfs"),
    (re.compile(r"rm\s+(-[a-zA-Z]*[rR][a-zA-Z]*f?|-[a-zA-Z]*f[a-zA-Z]*r?)\s+(/|~)(\s|$)"), "rm -rf on / or ~"),
    (re.compile(r"(--force(?!-push)|-f)\b.*\bgit\s+push|git\s+push\s+.*(--force|-f)\b"), "force push"),
    (re.compile(r"--force-push"), "force push"),
    (re.compile(r"diskutil\s+erase"), "disk erase"),
    (re.compile(r"dd\s+.*of=/dev/"), "dd to raw device"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = STREAM_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} bytes]"


def forbidden_command(command: str) -> str | None:
    """Return a denial reason when the command matches the denylist."""
    for pattern, label in FORBIDDEN_PATTERNS:
        if pattern.search(command):
            return label
    return None


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def run_shell(command: str, cwd: Path, timeout: int, stream_limit: int | None = None) -> CommandResult:
    limit = stream_limit or STREAM_LIMIT
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd),
            capture_output=True,
            errors="replace",
            timeout=timeout,
            text=True,
        )
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            stdout=truncate(redact(proc.stdout), limit),
            stderr=truncate(redact(proc.stderr), limit),
            duration_sec=time.monotonic() - start,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            exit_code=None,
            stdout=truncate(redact(_decode(exc.stdout)), limit),
            stderr=truncate(redact(_decode(exc.stderr)), limit),
            duration_sec=time.monotonic() - start,
            timed_out=True,
        )


def run_argv(argv: list[str], cwd: Path, timeout: int, stdin_text: str | None = None,
             stream_limit: int | None = None) -> CommandResult:
    """Run argv with captured output. Output is redacted and truncated to
    STREAM_LIMIT by default; callers that must receive parseable bulk output
    (e.g. the Codex model catalog, >256 KiB since 0.160) pass a larger
    stream_limit — truncation would corrupt the JSON."""
    limit = stream_limit or STREAM_LIMIT
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            errors="replace",
            timeout=timeout,
            text=True,
            input=stdin_text,
        )
        return CommandResult(
            command=" ".join(argv),
            exit_code=proc.returncode,
            stdout=truncate(redact(proc.stdout), limit),
            stderr=truncate(redact(proc.stderr), limit),
            duration_sec=time.monotonic() - start,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=" ".join(argv),
            exit_code=None,
            stdout=truncate(redact(_decode(exc.stdout)), limit),
            stderr=truncate(redact(_decode(exc.stderr)), limit),
            duration_sec=time.monotonic() - start,
            timed_out=True,
        )


def run_launch(launch) -> CommandResult:
    """Execute an adapters.Launch record. The only path real agents take."""
    return run_argv(
        launch.argv,
        cwd=launch.cwd,
        timeout=launch.timeout,
        stdin_text=launch.stdin_text,
    )
