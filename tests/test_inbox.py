"""Inbox + GitHub source tests against a fake `gh` binary on PATH.

No network, no real gh, no paid model: the fake gh prints canned JSON for
`issue list` and text for `auth status`, and records its argv so label
policy is observable. PATH is fully replaced so a developer machine's real
`gh` can never leak into these tests."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import dispatch, sources
from orx.cli import app
from orx.config import load_effective
from orx.records import ConfigError, ORXError
from orx.state import Store

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, make_project

runner = CliRunner()

GH_ISSUES = [
    {
        "number": 101,
        "title": "Fix the flaky test",
        "body": "It flakes on CI.",
        "url": "https://github.com/acme/orx/issues/101",
        "labels": [{"name": "orx"}],
    },
    {
        "number": 102,
        "title": "Add dark mode",
        "body": "",
        "url": "https://github.com/acme/orx/issues/102",
        "labels": [],
    },
]

INBOX_SECTION = """
[inbox]
github_labels = ["orx", "bug"]
auto_accept = true
"""


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def make_gh(directory: Path, issues: list[dict] = GH_ISSUES) -> Path:
    """A fake gh: canned auth text, canned issue JSON, argv recorded.
    Absolute paths are baked in because subprocess launches it as bare "gh"
    ($0 is not the script location)."""
    import shlex

    issues_path = directory / "issues.json"
    issues_path.write_text(json.dumps(issues))
    args_path = directory / "issue-args"
    return make_bin(
        directory,
        "gh",
        'if [ "$1" = "auth" ]; then\n'
        '  echo "github.com"\n'
        '  echo "  * Logged in to github.com account tester (keyring)"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "issue" ]; then\n'
        f'  /bin/echo "$@" > {shlex.quote(str(args_path))}\n'
        f"  /bin/cat {shlex.quote(str(issues_path))}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )


@pytest.fixture
def gh_bin(tmp_path, monkeypatch):
    """Fake gh on a PATH that contains nothing else."""
    directory = tmp_path / "bin"
    directory.mkdir()
    make_gh(directory)
    monkeypatch.setenv("PATH", str(directory))
    return directory


@pytest.fixture
def no_gh_bin(tmp_path, monkeypatch):
    """A PATH with no gh at all."""
    directory = tmp_path / "empty-bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def setup_cli_project(tmp_path, monkeypatch, config_toml: str = HOST_CONFIG_TOML) -> Path:
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(config_toml)
    (tmp_path / ".orx" / "profiles.toml").write_text(HOST_PROFILES_TOML)
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def payload(result):
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# watch_once + store dedupe


def test_watch_once_dedupes_across_passes(tmp_path, monkeypatch, gh_bin):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        first = sources.watch_once(project.store, [])
        assert first == {"fetched": 2, "new_events": 2, "new_items": 2, "skipped": 0}
        second = sources.watch_once(project.store, [])
        assert second == {"fetched": 2, "new_events": 0, "new_items": 0, "skipped": 2}
        items = project.store.inbox_items()
        assert len(items) == 2
        assert {i["source"] for i in items} == {"github"}
        assert {i["external_id"] for i in items} == {"101", "102"}
        assert all(i["status"] == "pending" for i in items)
        assert all(i["goal_id"] is None for i in items)
    finally:
        project.close()


def test_external_event_add_never_rewrites_existing_row(project):
    first = project.store.external_event_add("github", "1", "issue", {"title": "v1"})
    second = project.store.external_event_add("github", "1", "issue", {"title": "v2"})
    assert first == second
    raw = project.store.conn.execute(
        "SELECT payload_json FROM external_events WHERE id = ?", (first,)
    ).fetchone()
    assert json.loads(raw["payload_json"]) == {"title": "v1"}


def test_inbox_add_creates_one_item_per_event(project):
    event_id = project.store.external_event_add("github", "9", "issue", {"title": "t"})
    a = project.store.inbox_add(event_id, "first title", "b", "u")
    b = project.store.inbox_add(event_id, "second title", "b2", "u2")
    assert a == b
    items = project.store.inbox_items()
    assert len(items) == 1 and items[0]["title"] == "first title"


def test_inbox_items_status_filter_rejects_unknown(project):
    with pytest.raises(ORXError):
        project.store.inbox_items("bogus")
    with pytest.raises(ORXError):
        project.store.inbox_decide(1, "pending")


def test_watch_once_auto_accept_requires_project(project):
    with pytest.raises(ORXError):
        sources.watch_once(project.store, [], auto_accept=True)


# ---------------------------------------------------------------------------
# CLI flow


def test_cli_watch_list_show_accept_flow(tmp_path, monkeypatch, gh_bin):
    setup_cli_project(tmp_path, monkeypatch)
    result = invoke("watch", "--once", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert (body["fetched"], body["new_events"], body["new_items"], body["skipped"]) == (2, 2, 2, 0)

    result = invoke("inbox", "list", "--json")
    assert result.exit_code == 0
    items = payload(result)["items"]
    assert payload(result)["count"] == 2
    first = items[0]
    assert first["source"] == "github"
    assert first["status"] == "pending"
    assert first["external_id"] == "101"

    result = invoke("inbox", "show", "--json", str(first["id"]))
    assert result.exit_code == 0
    shown = payload(result)["item"]
    assert shown["title"] == "Fix the flaky test"
    assert shown["url"] == "https://github.com/acme/orx/issues/101"

    result = invoke("inbox", "accept", "--json", str(first["id"]))
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["goal"]["id"] == "G001"
    assert body["item"]["status"] == "accepted"
    assert body["item"]["goal_id"] == "G001"
    assert body["goal"]["acceptance"] == ["the source issue 101 is resolved"]
    assert "Fix the flaky test" in body["goal"]["objective"]

    # one-active-Goal invariant: the second accept errors loudly, exit 1
    result = invoke("inbox", "accept", "--json", str(items[1]["id"]))
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert "already active" in body["error"]
    # the failed acceptance leaves the item pending
    result = invoke("inbox", "show", "--json", str(items[1]["id"]))
    assert payload(result)["item"]["status"] == "pending"


def test_cli_reject_and_dismiss_paths(tmp_path, monkeypatch, gh_bin):
    setup_cli_project(tmp_path, monkeypatch)
    invoke("watch", "--once", "--json")
    items = payload(invoke("inbox", "list", "--json"))["items"]

    result = invoke("inbox", "reject", "--json", str(items[0]["id"]))
    assert result.exit_code == 0
    rejected = payload(result)["item"]
    assert rejected["status"] == "rejected"
    assert rejected["goal_id"] is None
    assert rejected["decided_at"]

    result = invoke("inbox", "dismiss", "--json", str(items[1]["id"]))
    assert result.exit_code == 0
    assert payload(result)["item"]["status"] == "dismissed"

    result = invoke("inbox", "list", "--json", "--status", "rejected")
    assert [i["id"] for i in payload(result)["items"]] == [items[0]["id"]]
    result = invoke("inbox", "list", "--json", "--status", "pending")
    assert payload(result)["count"] == 0

    # no Goal was created by reject/dismiss
    project = dispatch.open_project()
    try:
        assert project.store.goal_active() is None
    finally:
        project.close()

    # unknown statuses and ids are errors
    assert invoke("inbox", "list", "--json", "--status", "bogus").exit_code == 1
    assert invoke("inbox", "show", "--json", "999").exit_code == 1
    assert invoke("inbox", "reject", "--json", "999").exit_code == 1


def test_cli_watch_fails_loudly_without_gh(tmp_path, monkeypatch, no_gh_bin):
    setup_cli_project(tmp_path, monkeypatch)
    result = invoke("watch", "--once", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "gh" in body["error"]


def test_cli_watch_long_mode_ctrl_c_exits_clean(tmp_path, monkeypatch, gh_bin):
    setup_cli_project(tmp_path, monkeypatch)
    import orx.cli as cli_mod

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", interrupt)
    result = invoke("watch", "--json", "--interval", "1")
    assert result.exit_code == 0
    body = payload(result)  # exactly one pass envelope before the interrupt
    assert body["ok"] is True and body["new_items"] == 2


def test_watch_policy_labels_reach_gh(tmp_path, monkeypatch, gh_bin):
    setup_cli_project(tmp_path, monkeypatch, config_toml=HOST_CONFIG_TOML + INBOX_SECTION)
    result = invoke("watch", "--once", "--json")
    assert result.exit_code == 0, result.stdout
    args = (gh_bin / "issue-args").read_text().split()
    assert "--label" in args
    assert "orx" in args and "bug" in args


def test_watch_auto_accept_accepts_one_item_only(tmp_path, monkeypatch, gh_bin):
    setup_cli_project(tmp_path, monkeypatch, config_toml=HOST_CONFIG_TOML + INBOX_SECTION)
    result = invoke("watch", "--once", "--json")
    assert result.exit_code == 0, result.stdout
    assert payload(result)["new_items"] == 2

    project = dispatch.open_project()
    try:
        items = project.store.inbox_items()
        accepted = [i for i in items if i["status"] == "accepted"]
        pending = [i for i in items if i["status"] == "pending"]
        assert len(accepted) == 1 and accepted[0]["goal_id"] == "G001"
        assert len(pending) == 1  # one-active-Goal stops the second accept
        assert project.store.goal_active().id == "G001"

        # next pass: everything dedupes, the active Goal still blocks accepts
        report = sources.watch_once(
            project.store, [], auto_accept=True, project=project
        )
        assert report == {"fetched": 2, "new_events": 0, "new_items": 0, "skipped": 2}
        assert len(project.store.inbox_items("pending")) == 1
    finally:
        project.close()


# ---------------------------------------------------------------------------
# auth status


def test_auth_status_outside_project(tmp_path, monkeypatch, gh_bin):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)  # no .orx anywhere up this tree
    result = invoke("auth", "status", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True and body["gh"] is True
    assert "Logged in" in body["detail"]


def test_auth_status_reports_missing_gh(tmp_path, monkeypatch, no_gh_bin):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    result = invoke("auth", "status", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "gh" in body["error"]


# ---------------------------------------------------------------------------
# [inbox] policy parsing


def _project_files(tmp_path, config_toml):
    orx = tmp_path / ".orx"
    orx.mkdir(parents=True, exist_ok=True)
    (orx / "config.toml").write_text(config_toml)
    (orx / "profiles.toml").write_text(HOST_PROFILES_TOML)
    return orx / "config.toml", orx / "profiles.toml"


def test_inbox_policy_layered_values(tmp_path):
    pc, pp = _project_files(tmp_path, HOST_CONFIG_TOML + INBOX_SECTION)
    eff = load_effective(pc, pp)
    assert eff.config.inbox_github_labels == ["orx", "bug"]
    assert eff.config.inbox_auto_accept is True
    assert eff.origins["inbox.github_labels"] == "project"
    assert eff.origins["inbox.auto_accept"] == "project"


def test_inbox_policy_defaults_and_unknown_keys_ignored(tmp_path):
    config = HOST_CONFIG_TOML + '\n[inbox]\nfuture_key = "whatever"\nanother = 3\n'
    pc, pp = _project_files(tmp_path, config)
    eff = load_effective(pc, pp)
    assert eff.config.inbox_github_labels == []
    assert eff.config.inbox_auto_accept is False
    assert eff.origins["inbox.github_labels"] == "default"
    assert eff.origins["inbox.auto_accept"] == "default"


def test_inbox_policy_invalid_types_rejected(tmp_path):
    pc, pp = _project_files(tmp_path, HOST_CONFIG_TOML + '\n[inbox]\nauto_accept = "yes"\n')
    with pytest.raises(ConfigError):
        load_effective(pc, pp)
    pc, pp = _project_files(tmp_path, HOST_CONFIG_TOML + '\n[inbox]\ngithub_labels = "orx"\n')
    with pytest.raises(ConfigError):
        load_effective(pc, pp)


# ---------------------------------------------------------------------------
# migration


def test_v2_to_v4_migration_preserves_rows(tmp_path):
    """A v2 database upgrades in place and keeps every row; the inbox tables
    become usable."""
    db = tmp_path / "v2.db"
    store = Store.open(db)  # code is v3; build a v2 db by hand
    store.close()
    conn = sqlite3.connect(db)
    conn.executescript("""
DROP TABLE inbox_items;
DROP TABLE external_events;
UPDATE meta SET value = '2' WHERE key = 'schema_version';
INSERT INTO resource_status(profile, status, note, updated_at)
  VALUES ('legacy2', 'cooldown', 'cap reached', '2026-01-01T00:00:00+00:00');
INSERT INTO goals(id, objective, constraints_json, acceptance_json, context, status,
                  created_at, updated_at)
  VALUES ('G001', 'legacy goal', '[]', '[]', '', 'done',
          '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
""")
    conn.commit()
    conn.close()

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 8
        row = reopened.resource_row("legacy2")
        assert (row.status, row.note) == ("cooldown", "cap reached")
        assert reopened.goal_get("G001").objective == "legacy goal"
        event_id = reopened.external_event_add("github", "7", "issue", {"title": "x"})
        item_id = reopened.inbox_add(event_id, "x", "", "")
        assert reopened.inbox_items("pending")[0]["id"] == item_id
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v2.db.migrate-*")), "stale migration backups"
