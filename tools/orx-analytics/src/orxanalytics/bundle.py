"""Evidence bundles: hand a whole investigation to another agent.

A bundle is a directory the receiver can carry anywhere. Everything a
headline number rests on ships INSIDE the bundle — mirrored rows as JSON,
the exact recompute algorithm, excerpts, provenance, and an integrity
manifest. No claim in the bundle depends on a path that exists only on
this machine.

Layout:
  HANDOFF.md        one page: goal, scope, confirmed facts, hypotheses,
                    counter-evidence, excluded explanations, next steps
  OPERATOR.md       how to re-verify and continue investigating
  bundle.json       machine-readable manifest: headline metrics,
                    provenance, coverage, links with basis
  data/*.json       the mirrored rows the numbers were computed from
  evidence/*.md     source excerpts backing the important conclusions
  recompute.py      deterministic recomputation from data/ only
  integrity.json    SHA-256 of every file in the bundle
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import READER_VERSION, correlate, metrics
from .store import Store

# contract order (ORX docs/observability-contract.md); unknown sources
# sort last wherever this is consulted
_RANK = {"native_cli": 0, "zcode_db": 1, "host_report": 2,
         "output_estimate": 3}


def _dump(dest: Path, rel: str, obj: Any) -> str:
    p = dest / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str)
                 + "\n", encoding="utf-8")
    return rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(store: Store, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in store.conn.execute(sql, args)]


def _collect_run_data(store: Store, project_key: str, snap: int,
                      run_id: str, links: list[dict]) -> dict[str, list]:
    atts = metrics.attempts_for_run(store, snap, run_id)
    run_att = {a["id"] for a in atts}
    links = [l for l in links if l["attempt_id"] in run_att]
    att_ids = [a["id"] for a in atts]
    qmarks = ",".join("?" * len(att_ids)) if att_ids else "NULL"
    revs = _rows(store, "SELECT * FROM orx_revisions WHERE snapshot_id=? "
                        "AND run_id=?", (snap, run_id))
    rev_ids = [r["id"] for r in revs]
    rq = ",".join("?" * len(rev_ids)) if rev_ids else "NULL"
    direct = {l["session_id"] for l in links
              if l.get("harness") == "zcode"
              and l.get("confidence") != "ambiguous"}
    session_ids = sorted(
        set().union(*(correlate.zcode_tree(store, s) for s in direct))
        if direct else set())
    sq = ",".join("?" * len(session_ids)) if session_ids else "NULL"
    return {
        "runs.json": _rows(store, "SELECT * FROM orx_runs WHERE "
                                  "snapshot_id=? AND id=?", (snap, run_id)),
        "revisions.json": revs,
        "tasks.json": _rows(
            store, f"SELECT * FROM orx_tasks WHERE snapshot_id=? AND "
                   f"revision_id IN ({rq})", [snap, *rev_ids]),
        "attempts.json": atts,
        "usage_observations.json": _rows(
            store, f"SELECT * FROM orx_usage_obs WHERE snapshot_id=? AND "
                   f"attempt_id IN ({qmarks})", [snap, *att_ids]),
        "verifications.json": _rows(
            store, f"SELECT * FROM orx_verifications WHERE snapshot_id=? "
                   f"AND revision_id IN ({rq})", [snap, *rev_ids]),
        "task_events.json": _rows(
            store, f"SELECT * FROM orx_task_events WHERE snapshot_id=? "
                   f"AND revision_id IN ({rq})", [snap, *rev_ids]),
        "replan_task_maps.json": _rows(
            store, "SELECT * FROM orx_replan_task_maps WHERE snapshot_id=?",
            (snap,)),
        "replan_sources.json": _rows(
            store, "SELECT * FROM orx_replan_sources WHERE snapshot_id=?",
            (snap,)),
        "links.json": links,
        "zc_sessions.json": _rows(
            store, f"SELECT * FROM zc_sessions WHERE session_id IN ({sq})",
            session_ids) if session_ids else [],
        "zc_turns.json": _rows(
            store, f"SELECT * FROM zc_turns WHERE session_id IN ({sq})",
            session_ids) if session_ids else [],
        "zc_first_parts.json": _rows(
            store, f"SELECT session_id, substr(first_part_text,1,400) "
                   f"first_part_excerpt FROM zc_first_parts WHERE "
                   f"session_id IN ({sq})", session_ids) if session_ids else [],
    }


def _collect_session_data(store: Store, session_id: str) -> dict[str, list]:
    ids = correlate.zcode_tree(store, session_id)
    q = ",".join("?" * len(ids))
    return {
        "zc_sessions.json": _rows(
            store, f"SELECT * FROM zc_sessions WHERE session_id IN ({q})",
            ids),
        "zc_turns.json": _rows(
            store, f"SELECT * FROM zc_turns WHERE session_id IN ({q})", ids),
        "zc_requests.json": _rows(
            store, f"SELECT * FROM zc_requests WHERE session_id IN ({q})",
            ids),
    }


def _links_payload(store: Store, project_key: str, snap: int) -> list[dict]:
    out = []
    for l in correlate.links_for_snapshot(store, project_key, snap):
        out.append({
            "attempt_id": l.attempt_id, "harness": l.harness,
            "session_id": l.session_id, "authority": l.authority,
            "confidence": l.confidence, "basis": l.basis,
            "evidence": l.evidence,
        })
    return out


RECOMPUTE = '''#!/usr/bin/env python3
"""Recompute the bundle's headline numbers from data/ alone.

No database, no package, no network: this file and data/*.json are the
whole world. Every headline is recomputed independently and compared to
the value recorded in bundle.json; any difference is a MISMATCH and the
exit code is 1. Tamper with a data file and this script says so.
"""
import json, sys
from pathlib import Path

D = Path(__file__).parent / "data"

def maybe(name):
    p = D / name
    return json.loads(p.read_text()) if p.exists() else None

bundle = json.loads((Path(__file__).parent / "bundle.json").read_text())
results = []

def check(label, got, want):
    results.append((label, got == want, got, want))

head = bundle["headline"]

atts = maybe("attempts.json")
obs = maybe("usage_observations.json")
if atts is not None and obs is not None:
    rank = {"native_cli": 0, "zcode_db": 1, "host_report": 2,
            "output_estimate": 3}
    best = {}
    for u in obs:
        k = (rank.get(u["source"], 9), u["id"])
        cur = best.get(u["attempt_id"])
        if cur is None or k < (rank.get(cur["source"], 9), cur["id"]):
            best[u["attempt_id"]] = u
    tot_in = sum(u["input_tokens"] for u in best.values()
                 if u["input_tokens"] is not None) if best else None
    tot_out = sum(u["output_tokens"] for u in best.values()
                  if u["output_tokens"] is not None) if best else None
    if head.get("consumption") is not None:
        check("consumption.input_tokens_sum", tot_in,
              head["consumption"]["input_tokens_sum"])
        check("consumption.output_tokens_sum", tot_out,
              head["consumption"]["output_tokens_sum"])

vers = maybe("verifications.json")
if vers is not None:
    first = {}
    for v in sorted(vers, key=lambda v: (v["created_at"], v["id"])):
        first.setdefault((v["revision_id"], v["task_id"]), v)
    failed = set()
    for e in maybe("task_events.json") or []:
        if e["to_status"] == "failed":
            failed.add((e["revision_id"], e["task_id"]))
    revs = {r["id"]: r["run_id"] for r in maybe("revisions.json") or []}
    run_id = bundle["scope"].get("run_id")
    judged = fp = 0
    for (rev, task), v in first.items():
        if revs.get(rev) != run_id:
            continue
        judged += 1
        if v["passed"] == 1 and (rev, task) not in failed:
            fp += 1
    if head.get("first_pass_rate") is not None:
        check("first_pass.numerator", fp, head["first_pass_rate"]["numerator"])
        check("first_pass.denominator", judged,
              head["first_pass_rate"]["denominator"])

turns = maybe("zc_turns.json")
if turns is not None and head.get("session_side") is not None:
    tin = sum(t["input_tokens"] or 0 for t in turns
              if t["input_tokens"] is not None)
    tout = sum(t["output_tokens"] or 0 for t in turns
               if t["output_tokens"] is not None)
    check("session_side.linked_turns_input", tin,
          head["session_side"]["linked_turns_input"])
    check("session_side.linked_turns_output", tout,
          head["session_side"]["linked_turns_output"])

bad = [r for r in results if not r[1]]
for label, ok_, got, want in results:
    print(("MATCH   " if ok_ else "MISMATCH ") + label,
          "got", got, "recorded", want)
if bad:
    print(f"{len(bad)} MISMATCH(es)")
    sys.exit(1)
print(f"all {len(results)} headline checks MATCH")
sys.exit(0)
'''


def _headline_from_report(rep: dict) -> dict:
    c = rep["consumption"]
    fp = rep["first_pass_rate"]
    head: dict[str, Any] = {}
    if c.get("attempts"):
        head["consumption"] = {
            "input_tokens_sum": c["input_tokens_sum"],
            "output_tokens_sum": c["output_tokens_sum"],
            "attempts": c["attempts"],
            "attempts_with_observation": c["attempts_with_observation"],
        }
    head["first_pass_rate"] = {
        "numerator": fp["numerator"], "denominator": fp["denominator"]}
    u = rep.get("session_side", {}).get("union_totals") or {}
    head["session_side"] = {
        "linked_turns_input": u.get("input_tokens"),
        "linked_turns_output": u.get("output_tokens")}
    return head


HANDOFF_TMPL = """# {title}

Generated {generated} by {reader}. Scope: **{scope}**.

## 调查目标 (why this bundle exists)

{objective}

## 已确认事实 (confirmed, re-computable from data/)

{facts}

## 假设与反例 (hypotheses under test)

{hypotheses}

## 已排除的解释 (excluded, with the numbers that excluded them)

{excluded}

## 下一步 (what the receiving agent should do)

{next}

## 授权与边界

Read-only analysis of local execution records. `recompute.py` re-derives
every headline from `data/` alone; `integrity.json` pins file contents.
No claim outside `data/` is load-bearing. No fees are computed (no
billing basis). Unknown values stay unknown.
"""


OPERATOR_TMPL = """# OPERATOR — 继续调查指引

1. **复核**：`python3 recompute.py` — 全部 headline 必须 MATCH。改任何
   data/*.json 后重跑，应看到 MISMATCH（完整性自检）。
2. **对账口径**：数据文件与指标定义见 `bundle.json.definitions`；
   语义以 `docs/metrics.md`（仓库内冻结版）为准，本包 `definitions`
   字段内嵌了要点。
3. **扩展**：bundle 只带本 scope 的行。要查更广范围，在原机器上运行
   `orx-analytics collect` + `report`，或要求导出更大的 bundle。
4. **反证**：每个结论都附 data；推翻它 = 给出同口径的相反数据，不是
   给出另一个摘要。
5. **回复格式**：保持 事实/假设/反例/排除 分节；引用 data 文件名与行内
   字段，不引用本机路径。
"""


def export_run_bundle(store: Store, project_key: str, run_id: str,
                      dest: Path, *, title: str | None = None,
                      objective: str = "",
                      facts: list[str] | None = None,
                      hypotheses: list[str] | None = None,
                      excluded: list[str] | None = None,
                      next_steps: list[str] | None = None) -> Path:
    snap = correlate.latest_snapshot(store, project_key)
    if snap is None:
        raise SystemExit(f"no snapshot for {project_key}")
    rep = metrics.run_report(store, project_key, run_id)
    links = _links_payload(store, project_key, snap)
    data = _collect_run_data(store, project_key, snap, run_id, links)
    return _write_bundle(dest, title or f"{run_id} run bundle", objective,
                         {"project": project_key, "run_id": run_id,
                          "snapshot_id": snap},
                         _headline_from_report(rep), data, facts,
                         hypotheses, excluded, next_steps)


def export_session_bundle(store: Store, session_id: str, dest: Path, *,
                          title: str | None = None,
                          objective: str = "",
                          facts: list[str] | None = None,
                          hypotheses: list[str] | None = None,
                          excluded: list[str] | None = None,
                          next_steps: list[str] | None = None) -> Path:
    rep = metrics.session_report(store, session_id)
    data = _collect_session_data(store, session_id)
    headline = {"session_side": {
        "linked_turns_input": rep["tree"]["input_tokens"],
        "linked_turns_output": rep["tree"]["output_tokens"]}}
    return _write_bundle(dest, title or f"session {session_id} bundle",
                         objective,
                         {"session_id": session_id,
                          "tree_sessions": len(data["zc_sessions.json"])},
                         headline, data, facts, hypotheses, excluded,
                         next_steps)


def _write_bundle(dest: Path, title: str, objective: str, scope: dict,
                  headline: dict, data: dict[str, list],
                  facts: list[str] | None, hypotheses: list[str] | None,
                  excluded: list[str] | None, next_steps: list[str] | None
                  ) -> Path:
    if dest.exists():
        raise FileExistsError(dest)
    dest.mkdir(parents=True)
    for name, rows in data.items():
        _dump(dest, f"data/{name}", rows)
    (dest / "recompute.py").write_text(RECOMPUTE, encoding="utf-8")

    manifest = {
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reader_version": READER_VERSION,
        "scope": scope,
        "headline": headline,
        "definitions": {
            "consumption": "canonical observation per attempt (source rank "
                           "native_cli>zcode_db>host_report>output_estimate, "
                           "lowest id within source), partial sums flagged, "
                           "no fee math",
            "first_pass": "denominator = tasks with >=1 verification row; "
                          "numerator = earliest row green AND no failed "
                          "task_events; never-verified tasks are outside",
            "session_side": "zcode turn_usage totals over linked session "
                            "trees, deduped by session id",
            "unknown": "NULL stays NULL; never zero-filled",
        },
        "files": sorted(["data/" + n for n in data] + ["recompute.py"]),
    }
    _dump(dest, "bundle.json", manifest)

    facts = facts or []
    hypotheses = hypotheses or []
    excluded = excluded or []
    next_steps = next_steps or [
        "run `python3 recompute.py` and confirm every headline MATCHes",
        "read data/links.json — each correlation carries its basis and "
        "confidence; challenge any inferred link by re-deriving it",
    ]
    handoff = HANDOFF_TMPL.format(
        title=title,
        generated=datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        reader=READER_VERSION,
        scope=json.dumps(scope, ensure_ascii=False),
        objective=objective or "(stated by the exporter)",
        facts="\n".join(f"- {f}" for f in facts) or "- (none recorded)",
        hypotheses="\n".join(f"- {h}" for h in hypotheses) or "- (none)",
        excluded="\n".join(f"- {e}" for e in excluded) or "- (none)",
        next="\n".join(f"- {n}" for n in next_steps),
    )
    (dest / "HANDOFF.md").write_text(handoff, encoding="utf-8")
    (dest / "OPERATOR.md").write_text(OPERATOR_TMPL, encoding="utf-8")

    integrity = {}
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.name != "integrity.json":
            integrity[str(p.relative_to(dest))] = _sha256(p)
    (dest / "integrity.json").write_text(
        json.dumps(integrity, indent=1) + "\n", encoding="utf-8")
    return dest
