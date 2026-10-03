"""External event sources (GitHub via `gh`) and the inbox watch loop.

`gh` owns GitHub credentials; ORX never stores or reads a token. The only
GitHub touchpoint is `gh issue list --json` / `gh auth status` subprocess
calls. The watch loop never launches a completion and never touches model
CLIs.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from orx import dispatch
from orx.records import ORXError

GH_AUTH_TIMEOUT_SEC = 15
GH_LIST_TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# gh availability


def check_gh() -> tuple[bool, str]:
    """(ok, detail) for the `gh` CLI: present on PATH and authenticated.
    Display-only; there is no login/logout here."""
    if shutil.which("gh") is None:
        return False, "gh not found on PATH (install GitHub CLI and run `gh auth login`)"
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=GH_AUTH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, f"gh auth status timed out after {GH_AUTH_TIMEOUT_SEC}s"
    except OSError as exc:
        return False, f"gh auth status failed to run: {exc}"
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout).splitlines() if ln.strip()]
        return False, lines[0] if lines else f"gh auth status exited {proc.returncode}"
    for line in proc.stdout.splitlines():
        if "Logged in" in line:
            return True, line.strip()
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return True, lines[0] if lines else "gh authenticated"


# ---------------------------------------------------------------------------
# GitHub source


def list_github_issues(labels: list[str]) -> list[dict]:
    """Open GitHub issues via `gh issue list --json`, normalized to
    {source, external_id, kind, title, body, url, labels}. Any gh failure
    yields an empty list — the caller reports the problem (check_gh)."""
    if shutil.which("gh") is None:
        return []
    cmd = [
        "gh", "issue", "list",
        "--json", "number,title,body,url,labels",
        "--limit", "50",
        "--state", "open",
    ]
    for label in labels or []:
        cmd += ["--label", label]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_LIST_TIMEOUT_SEC
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or "number" not in row:
            continue
        out.append({
            "source": "github",
            "external_id": str(row["number"]),
            "kind": "issue",
            "title": str(row.get("title") or ""),
            "body": str(row.get("body") or ""),
            "url": str(row.get("url") or ""),
            "labels": [
                label["name"] for label in row.get("labels") or []
                if isinstance(label, dict) and label.get("name")
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Acceptance


def _item_objective(item: dict) -> str:
    """Goal objective from the item title plus a one-line body summary."""
    title = " ".join((item.get("title") or "").split())
    body = " ".join((item.get("body") or "").split())
    if len(body) > 200:
        body = body[:197] + "..."
    if title and body:
        return f"{title}: {body}"
    return title or body or f"github issue {item.get('external_id')}"


def accept_item(project, item_id: int) -> dict:
    """Accept one inbox item: create the Goal via dispatch.create_goal and
    link it (inbox_decide accepted + goal_id). Raises the same loud
    one-active-Goal error create_goal raises when a Goal is active."""
    item = project.store.inbox_item_get(item_id)
    if item["status"] != "pending":
        raise ORXError(f"inbox item {item_id} is already {item['status']}")
    context_bits = [f"source: {item['source']} {item['kind']} {item['external_id']}"]
    if item["url"]:
        context_bits.append(f"url: {item['url']}")
    goal, run = dispatch.create_goal(
        project,
        objective=_item_objective(item),
        acceptance=[f"the source issue {item['external_id']} is resolved"],
        constraints=[],
        context="\n".join(context_bits),
    )
    decided = project.store.inbox_decide(item_id, "accepted", goal_id=goal.id)
    return {
        "item": decided,
        "goal": {
            "id": goal.id,
            "objective": goal.objective,
            "acceptance": goal.acceptance,
            "constraints": goal.constraints,
            "status": goal.status,
        },
        "run": {"id": run.id, "status": run.status},
    }


# ---------------------------------------------------------------------------
# Watch


def watch_once(store, labels: list[str], auto_accept: bool = False, project=None) -> dict:
    """One poll pass: fetch issues, dedupe into external_events, create inbox
    items for new events. Returns {fetched, new_events, new_items, skipped}.

    auto_accept=True accepts new pending items via accept_item (the same
    routine `orx inbox accept` uses) — only while NO Goal is active, so the
    one-active-Goal invariant holds: at most one item is accepted per pass
    and the rest stay pending. auto_accept requires the open `project`;
    without it the pass still records events and items."""
    if auto_accept and project is None:
        raise ORXError("auto_accept requires an open project")

    events = list_github_issues(labels)
    report = {"fetched": len(events), "new_events": 0, "new_items": 0, "skipped": 0}
    new_item_ids: list[int] = []
    for event in events:
        existing = store.external_event_find(event["source"], event["external_id"])
        event_id = store.external_event_add(
            event["source"], event["external_id"], event["kind"], event
        )
        if existing is not None:
            report["skipped"] += 1
            continue
        report["new_events"] += 1
        item_id = store.inbox_add(event_id, event["title"], event["body"], event["url"])
        report["new_items"] += 1
        new_item_ids.append(item_id)

    if auto_accept and new_item_ids and store.goal_active() is None:
        # Accepting creates the active Goal; later items in this pass (and
        # later passes) stay pending until it finishes.
        accept_item(project, new_item_ids[0])
    return report
