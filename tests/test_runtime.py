"""Runtime wrapper: fake success, nonzero, timeout, garbage output, redaction,
truncation, and the verification denylist."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orx import runtime
from orx.adapters.base import Launch
from orx.adapters.codex import CodexAdapter


def test_run_argv_success_and_nonzero(tmp_path):
    ok = runtime.run_argv(["/bin/sh", "-c", "exit 0"], cwd=tmp_path, timeout=10)
    assert ok.ok and ok.exit_code == 0
    bad = runtime.run_argv(["/bin/sh", "-c", "echo boom >&2; exit 3"], cwd=tmp_path, timeout=10)
    assert not bad.ok and bad.exit_code == 3
    assert "boom" in bad.stderr


def test_run_shell_captures_output(tmp_path):
    result = runtime.run_shell("echo hello; echo err >&2", cwd=tmp_path, timeout=10)
    assert result.ok
    assert "hello" in result.stdout and "err" in result.stderr


def test_timeout_kills_and_reports(tmp_path):
    result = runtime.run_shell("sleep 30", cwd=tmp_path, timeout=1)
    assert result.timed_out
    assert result.exit_code is None
    assert not result.ok
    assert result.duration_sec < 15


def test_garbage_binary_output_decoded(tmp_path):
    garbage = tmp_path / "garbage.out"
    garbage.write_bytes(b"\xff\xfe\x00binary\xff")
    result = runtime.run_shell(f"cat '{garbage}'", cwd=tmp_path, timeout=10)
    assert result.exit_code == 0  # decode errors are replaced, never raised
    assert "binary" in result.stdout


def test_redaction_patterns():
    text = (
        "token sk-abcdefghijklmnop123456 leaked\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "aws AKIAABCDEFGHIJKLMNOP qq\n"
        "ghp_abcdefghijklmnopqrst, github_pat_ABCDEFGHIJKLMNOPQRSTUV\n"
        "api_key=supersecret token=anothersecret\n"
        "safe words remain"
    )
    out = runtime.redact(text)
    assert "sk-" not in out.replace("sk-", "", 0) or "[REDACTED]" in out
    assert "sk-abcdefghijklmnop123456" not in out
    assert "Bearer abcdefghijklmnop" not in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "ghp_" not in out and "github_pat_" not in out
    assert "supersecret" not in out and "anothersecret" not in out
    assert "api_key=[REDACTED]" in out and "token=[REDACTED]" in out
    assert "safe words remain" in out


def test_truncate_caps_stream_size():
    huge = "x" * (runtime.STREAM_LIMIT + 1000)
    out = runtime.truncate(huge)
    assert len(out) < len(huge)
    assert out.endswith(f"[truncated at {runtime.STREAM_LIMIT} bytes]")
    assert runtime.truncate("small") == "small"


def test_launch_log_stays_bounded_while_full_stream_keeps_tail(tmp_path):
    """Output past LAUNCH_STREAM_LIMIT is cut from the log view and kept,
    redacted, for parsers. The last turn.completed wins; it is not added to
    the earlier one."""
    limit = runtime.LAUNCH_STREAM_LIMIT
    secret = "sk-abcdefghijklmnop123456"
    thread = "11111111-1111-4111-8111-111111111111"
    # A non-token character must break the sk- pattern. Otherwise redaction
    # consumes the alphanumeric pad and the stream shrinks under the log limit.
    lines = [
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps({"type": "item.completed", "item": {"text": secret + " " + ("z" * (limit + 100))}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 100, "output_tokens": 1}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 3, "cached_input_tokens": 4, "output_tokens": 2}}),
    ]
    blob = tmp_path / "blob.txt"
    blob.write_text("\n".join(lines) + "\n")
    script = tmp_path / "emit.py"
    script.write_text(
        "import pathlib, sys\n"
        "sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())\n"
    )
    launch = Launch(
        argv=[sys.executable, str(script), str(blob)],
        cwd=tmp_path, timeout=30, stdin_text="",
    )
    result = runtime.run_launch(launch)
    assert result.ok
    assert result.stdout.endswith(f"[truncated at {limit} bytes]")
    assert len(result.stdout_full) > limit
    assert secret not in result.stdout and secret not in result.stdout_full
    assert "[REDACTED]" in result.stdout and "[REDACTED]" in result.stdout_full
    assert '"output_tokens": 2' not in result.stdout
    assert '"output_tokens": 2' in result.stdout_full
    captured = CodexAdapter().interpret_capture(launch, result)
    assert captured.session_ref == thread
    assert captured.miss_reason is None
    assert captured.usage == {
        "input_tokens": 3,
        "output_tokens": 2,
        "cached_input_tokens": 4,
        "source": "native_cli",
        "accuracy": "exact",
    }
    assert captured.usage["input_tokens"] != 10 + 3


def test_denylist():
    for command in (
        "sudo rm file",
        "mkfs.ext4 /dev/sda1",
        "rm -rf /",
        "rm -rf ~",
        "git push --force origin main",
        "git push origin main --force",
        "diskutil eraseDisk apfs Test disk1",
        "dd if=x of=/dev/disk2",
    ):
        assert runtime.forbidden_command(command) is not None, command
    for command in (
        "pytest -q",
        "rm -rf build/",
        "echo force-push-topic",
        "cat src/main.py",
    ):
        assert runtime.forbidden_command(command) is None, command
