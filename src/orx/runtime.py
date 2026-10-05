"""The only subprocess wrapper.

argv/shell command, fixed cwd, timeout, cancellation, output capture with
redaction and truncation. Verification commands run through here; future
adapters translate a Launch record into argv and call this module instead of
subprocess themselves.
"""

from __future__ import annotations

import re
import shlex
import shutil
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


# ---------------------------------------------------------------------------
# Static command preflight (R002 follow-up). Before a worker invests in a
# task, ORX checks each command verification entry WITHOUT executing it: the
# shared denylist above plus a PATH probe of the command's first token. This
# removes one measured R002 failure class — workers whose shell could not run
# the checks but implemented anyway and reported success. It does not prevent
# red tests or model mistakes, and it never judges the work.

# /bin/sh builtins: runnable even though no PATH entry matches them, so a
# PATH probe must not report them missing.
SHELL_BUILTINS: frozenset[str] = frozenset({
    ":", ".", "[", "break", "case", "cd", "command", "continue", "echo",
    "eval", "exec", "exit", "export", "false", "getopts", "hash", "pwd",
    "read", "readonly", "return", "set", "shift", "test", "times", "trap",
    "true", "type", "ulimit", "umask", "unset", "wait",
})

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GROUPING_TOKENS = frozenset({"(", ")", "{", "}", "!"})
_SEPARATOR_WORDS = frozenset({"&&", "||", ";", "|", "&", ";;", "\n"})
_SEPARATOR_SUBSTRINGS = ("&&", "||", ";", "|", "&", "\n")


def _cut_at_separator(word: str) -> str:
    """Trim a shell separator glued onto a word without spaces (``a&&b``)."""
    cut = len(word)
    for sep in _SEPARATOR_SUBSTRINGS:
        pos = word.find(sep)
        if pos != -1 and pos < cut:
            cut = pos
    return word[:cut]


def first_command_token(command: str) -> str | None:
    """The first command word of a shell command line, or None when the line
    has no plain command word.

    This is the PATH-probe target: the program /bin/sh would exec first.
    Leading variable assignments (``FOO=bar cmd``) and grouping tokens
    (``(``, ``{``, ``!``) are skipped; the scan stops at the first separator
    (``&&``, ``||``, ``;``, ``|``, ``&``). Conservative by design: a line
    that does not tokenize to a plain word yields None and the caller
    reports an undetermined probe instead of guessing.
    """
    text = (command or "").strip()
    if not text:
        return None
    try:
        words = shlex.split(text)
    except ValueError:
        # Unbalanced quotes: whitespace-split rather than fail outright.
        words = text.split()
    for word in words:
        word = _cut_at_separator(word)
        if not word or _ENV_ASSIGNMENT.match(word):
            continue
        if word in _GROUPING_TOKENS:
            continue
        if word in _SEPARATOR_WORDS:
            break
        return word
    return None


def probe_path_token(token: str) -> tuple[str, str | None]:
    """(status, resolved path or None) for one command token.

    ``builtin`` — a /bin/sh builtin, runnable regardless of PATH.
    ``found`` / ``missing`` — the shutil.which resolution; tokens carrying a
    directory component are checked directly, matching shell lookup rules.
    """
    if token in SHELL_BUILTINS:
        return "builtin", None
    resolved = shutil.which(token)
    if resolved is not None:
        return "found", resolved
    return "missing", None


def static_command_probe(command: str) -> dict:
    """Denylist + first-token PATH probe for one command string. Nothing is
    executed: this is the assignment-time preflight shared by every launch
    path, so a check that can never run as written is visible before a
    worker starts.

    ``blocked`` is true only for a denied command or a bare first token that
    is absent from PATH (and not a shell builtin) — tasks never install into
    PATH directories, so a bare missing name really cannot run. A path-like
    token (``./scripts/x.sh``, ``tools/bin/y``) may be a file the task
    itself creates, so a static miss downgrades to ``undetermined`` rather
    than a false ``blocked``; the worker's own start gate still decides by
    actually running the check.
    """
    denial = forbidden_command(command)
    token = first_command_token(command)
    if token is None:
        status, resolved = "undetermined", None
    else:
        status, resolved = probe_path_token(token)
        if status == "missing" and ("/" in token or "\\" in token):
            status, resolved = "undetermined", None
    return {
        "command": command,
        "denial": denial,
        "token": token,
        "token_status": status,
        "token_path": resolved,
        "runnable": denial is None and status in ("found", "builtin"),
        "blocked": denial is not None or status == "missing",
    }


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool
    # Full redacted streams. stdout/stderr are the bounded log view. Parsers
    # read the full text so a usage or session event past the log limit is
    # not discarded. Empty means the caller did not keep a separate copy.
    stdout_full: str = ""
    stderr_full: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _bounded(stdout: str, stderr: str, limit: int) -> tuple[str, str, str, str]:
    """Redact once. The log view is truncated; the full redacted text is what
    adapters parse. Secrets never remain in either copy."""
    out = redact(stdout)
    err = redact(stderr)
    return truncate(out, limit), truncate(err, limit), out, err


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
        stdout, stderr, stdout_full, stderr_full = _bounded(proc.stdout, proc.stderr, limit)
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.monotonic() - start,
            timed_out=False,
            stdout_full=stdout_full,
            stderr_full=stderr_full,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, stdout_full, stderr_full = _bounded(
            _decode(exc.stdout), _decode(exc.stderr), limit)
        return CommandResult(
            command=command,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.monotonic() - start,
            timed_out=True,
            stdout_full=stdout_full,
            stderr_full=stderr_full,
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
        stdout, stderr, stdout_full, stderr_full = _bounded(proc.stdout, proc.stderr, limit)
        return CommandResult(
            command=" ".join(argv),
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.monotonic() - start,
            timed_out=False,
            stdout_full=stdout_full,
            stderr_full=stderr_full,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, stdout_full, stderr_full = _bounded(
            _decode(exc.stdout), _decode(exc.stderr), limit)
        return CommandResult(
            command=" ".join(argv),
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.monotonic() - start,
            timed_out=True,
            stdout_full=stdout_full,
            stderr_full=stderr_full,
        )


# CLI agent streams exceed the 256 KiB default on real planner runs and a
# head truncation cut the FINAL events (codex turn.completed, cursor usage)
# first. The execution log stays inside this bound. Adapters parse
# CommandResult.stdout_full, which is the redacted stream before that cut,
# so a usage or session event the harness actually emitted still survives.
LAUNCH_STREAM_LIMIT = 2 * 1024 * 1024


def run_launch(launch) -> CommandResult:
    """Execute an adapters.Launch record. The only path real agents take."""
    return run_argv(
        launch.argv,
        cwd=launch.cwd,
        timeout=launch.timeout,
        stdin_text=launch.stdin_text,
        stream_limit=LAUNCH_STREAM_LIMIT,
    )
