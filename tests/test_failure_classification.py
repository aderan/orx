"""Failure evidence must not turn successful stream metadata into cooldowns."""

import json

import pytest

from orx import dispatch, health
from orx.adapters.base import classify_failure, error_kind_from_text
from orx.runtime import CommandResult


def result(stdout="", stderr="", timed_out=False):
    return CommandResult(command="fixture", exit_code=1, stdout=stdout, stderr=stderr,
                         duration_sec=240 if timed_out else 0.01, timed_out=timed_out)


@pytest.mark.parametrize("stream", [
    {"type": "model.streaming", "timestamp": 1795054290000, "eventId": "event-429",
     "payload": {"kind": "reasoning_delta", "delta": "rate limit and quota are discussed here"}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "HTTP 429 is an example in the user's source code", "usage": {"output_tokens": 429}},
    {"type": "tool.updated", "payload": {"kind": "result",
     "result": {"success": True, "content": "quota rate limit API key"}}},
])
def test_metadata_and_success_content_do_not_classify_as_provider_errors(stream):
    text = json.dumps(stream)
    assert classify_failure(result(stdout=text)) == "process_failure"
    assert health.resource_error_kind(text) == "process_failure"


def test_timeout_precedes_incidental_stream_content_and_never_cools_profile(project):
    stream = json.dumps({"type": "model.streaming", "timestamp": 1795054290000,
                         "payload": {"kind": "reasoning_delta", "delta": "an API key and quota"}})
    failed = result(stdout=stream, timed_out=True)
    assert classify_failure(failed) == "timeout"
    dispatch._record_attempt_health(project.store, "host-worker", failed)
    row = project.store.resource_row("host-worker")
    assert row.last_error_kind == "timeout"
    assert row.status != "cooldown"
    assert row.cooldown_until is None


@pytest.mark.parametrize("error", [
    {"type": "error", "status": 429},
    {"type": "error", "error": {"code": "rate_limit_error", "message": "Request rejected"}},
    {"type": "turn.failed", "error": {"message": "HTTP 429"}},
    {"type": "error", "payload": {"statusCode": 429}},
])
def test_structured_rate_limit_errors_are_recognized(error):
    assert classify_failure(result(stdout=json.dumps(error))) == "rate_limited"


@pytest.mark.parametrize("text", [
    "HTTP 429", "HTTP/1.1 429 Too Many Requests", "status code: 429",
    "429 Too Many Requests", "rate limit exceeded", "[ERROR] HTTP 429 Too Many Requests",
])
def test_explicit_rate_limit_messages_still_classify(text):
    assert error_kind_from_text(text) == "rate_limited"


@pytest.mark.parametrize("text", [
    "processed 429 tokens", "event-id-429-abcd", "timestamp 1795054290000",
    "there were 1429 bytes", '{"type":"model.streaming","payload":',
])
def test_plain_numbers_and_partial_json_are_not_http_errors(text):
    assert error_kind_from_text(text) == "process_failure"


def test_real_rate_limit_alone_still_cools_profile(project):
    dispatch._record_attempt_health(project.store, "host-worker", result(stderr="HTTP 429"))
    row = project.store.resource_row("host-worker")
    assert row.status == "cooldown"
    assert row.last_error_kind == "rate_limited"


@pytest.mark.parametrize("stream,expected", [
    ({"type": "result", "is_error": True, "errors": [{"code": "rate_limit_error"}]}, "rate_limited"),
    ({"type": "error", "message": "...invalid_json_schema..."}, "invalid_request"),
    ({"type": [], "timestamp": 1795054290000}, "process_failure"),
    ({"type": "model.streaming", "payload": {"delta": "请求过于频繁，额度已用尽"}}, "process_failure"),
])
def test_error_envelopes_and_unrelated_payloads_remain_distinct(stream, expected):
    assert health.resource_error_kind(json.dumps(stream, ensure_ascii=False)) == expected


def test_timeout_dispatch_does_not_fall_back_or_cool_profile(tmp_path, monkeypatch):
    from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec

    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake", "host-worker"]',
    )
    project = make_project(tmp_path, config, HOST_PROFILES_TOML)
    try:
        goal = dispatch.create_goal(project, "classify failure", ["done"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["done"])]))
        stream = json.dumps({"type":"model.streaming","timestamp":1795054290000})
        monkeypatch.setattr("orx.runtime.run_launch", lambda _: result(stdout=stream, timed_out=True))
        outcome = dispatch.run_slice(project)
        assert outcome["host_required"] == []
        assert outcome["failed"][0]["error_kind"] == "timeout"
        assert outcome["failed"][0]["reason"] == "timed out after 240s"
        row = project.store.resource_row("cli-fake")
        assert row.last_error_kind == "timeout"
        assert row.cooldown_until is None
    finally:
        project.close()
