"""Runtime wrapper: fake success, nonzero, timeout, garbage output, redaction,
truncation, and the verification denylist."""

from __future__ import annotations

import json
import sys
import time
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


# -- manageable launches: stop request, signal, and the actual exit (G010 T003) --


def test_manageable_launch_without_stop_behaves_normally(tmp_path):
    """A stop_check that never fires is a plain launch: same success shape,
    stdin still delivered through the writer thread, no stop fields set."""
    launch = Launch(
        argv=["/bin/sh", "-c", "cat"], cwd=tmp_path, timeout=10,
        stdin_text="payload-through-stdin",
        stop_check=lambda: False,
    )
    result = runtime.run_launch(launch)
    assert result.ok and result.exit_code == 0
    assert "payload-through-stdin" in result.stdout
    assert result.stop_requested is False
    assert result.stop_signal is None


def test_manageable_launch_stop_records_request_signal_and_actual_exit(tmp_path):
    """The stop the CALLER requested: SIGTERM to the launch's own process
    group (the launched process and its controlled children), the actual
    exit waited for and reported, and no timeout flag — the stop is the
    fact, not a deadline."""
    import threading

    released = threading.Event()
    holder: dict = {}

    def stop_check() -> bool:
        return released.is_set()

    launch = Launch(
        # a group with a controlled child, so the group kill is observable
        argv=["/bin/sh", "-c", "sleep 30 & wait"], cwd=tmp_path, timeout=60,
        stdin_text="",
        stop_check=stop_check,
    )

    def run_it():
        holder["result"] = runtime.run_launch(launch)

    thread = threading.Thread(target=run_it)
    start = time.monotonic()
    thread.start()
    time.sleep(0.5)  # the launch is mid-wait when the cancel lands
    released.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "the stop must end the wait, not hang"
    result = holder["result"]
    assert result.stop_requested is True
    assert result.stop_signal == "SIGTERM"
    assert result.stop_escalated is False
    assert result.timed_out is False
    assert result.exit_code is not None  # the actual, reaped exit — a fact
    assert result.exit_code != 0         # died by signal, not a clean exit
    assert result.duration_sec < 20      # far short of `sleep 30`


def test_manageable_launch_escalates_to_sigkill_when_ignored(tmp_path):
    """A process group that ignores SIGTERM is stopped anyway: the grace
    window escalates to SIGKILL and the escalation is recorded."""
    launch = Launch(
        # trap '' TERM makes the shell AND its child ignore SIGTERM
        argv=["/bin/sh", "-c", "trap '' TERM; sleep 30"], cwd=tmp_path,
        timeout=60, stdin_text="",
    )
    result = runtime._run_manageable(
        launch.argv, cwd=tmp_path, timeout=60, stdin_text="",
        limit=runtime.STREAM_LIMIT, stop_check=lambda: True,
        poll=0.05, grace=0.3,
    )
    assert result.stop_requested is True
    assert result.stop_signal == "SIGKILL"
    assert result.stop_escalated is True
    assert result.exit_code is not None and result.exit_code != 0
    assert result.duration_sec < 15
