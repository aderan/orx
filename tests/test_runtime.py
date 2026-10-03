"""Runtime wrapper: fake success, nonzero, timeout, garbage output, redaction,
truncation, and the verification denylist."""

from __future__ import annotations

from pathlib import Path

from orx import runtime


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
