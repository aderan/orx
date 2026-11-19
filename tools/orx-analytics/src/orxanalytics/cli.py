"""Command line entry: probe / projects / collect / report / session."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, discovery, metrics, probe
from .store import Store, DEFAULT_DATA_DIR


def _store(args) -> Store:
    return Store(Path(args.data_dir) if args.data_dir else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="orx-analytics",
        description="Read-only execution observability across projects "
                    "and agents")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data-dir", default=None,
                    help=f"analytics home (default {DEFAULT_DATA_DIR}); "
                         f"env ORX_ANALYTICS_HOME overrides")
    if os.environ.get("ORX_ANALYTICS_HOME"):
        ap.set_defaults(data_dir=os.environ["ORX_ANALYTICS_HOME"])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="probe local sources honestly")
    p.add_argument("--project", action="append", default=[])
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("projects", help="manage the project registry")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("root", nargs="?")
    p.add_argument("--name")

    p = sub.add_parser(
        "discover", help="merge ORX index, registry and a bounded scan")
    p.add_argument("--index", default=None, metavar="PATH",
                   help="projects-index.json to consume (default: ORX "
                        "data-dir resolution)")
    p.add_argument("--scan", action="append", default=[], metavar="ROOT",
                   help="working directory to bounded-scan (repeatable)")
    p.add_argument("--max-depth", type=int,
                   default=discovery.DEFAULT_MAX_DEPTH,
                   help="scan depth below each root (default %(default)s)")
    p.add_argument("--register", action="store_true",
                   help="explicitly register discovered projects that are "
                        "not registered yet")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("collect", help="one incremental read-only pass")
    p.add_argument("--project", action="append", default=[],
                   help="restrict ORX snapshots to these projects")
    p.add_argument("--skip-global", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="six-reading run report (ORX project)")
    p.add_argument("project")
    p.add_argument("--run")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("session", help="native zcode session report")
    p.add_argument("session_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bundle", help="export an evidence bundle")
    p.add_argument("kind", choices=["run", "session"])
    p.add_argument("target", help="project root (run) or session id")
    p.add_argument("--run", help="run id for kind=run (default latest)")
    p.add_argument("--dest", required=True)
    p.add_argument("--title")
    p.add_argument("--objective", default="")
    p.add_argument("--fact", action="append", default=[])
    p.add_argument("--hypothesis", action="append", default=[])
    p.add_argument("--excluded", action="append", default=[])
    p.add_argument("--next", action="append", default=[])

    p = sub.add_parser("provenance", help="import provenance summary")

    p = sub.add_parser("findings", help="list findings / draft issues")
    p.add_argument("action", choices=["list", "draft", "dismiss"])
    p.add_argument("id", nargs="?", help="finding id (draft/dismiss)")
    p.add_argument("--project")
    p.add_argument("--note", default="")

    p = sub.add_parser(
        "watch", help="collection demand on the coordination engine")
    p.add_argument("action", choices=["start", "stop", "status", "once"])
    p.add_argument("--interval", type=int, default=600,
                   help="legacy: only the once round's sleep; the engine "
                        "owns collection cadence")

    args = ap.parse_args(argv)

    if args.cmd == "probe":
        result = probe.probe_all([Path(x) for x in args.project])
        if args.json:
            print(json.dumps(result, indent=1, ensure_ascii=False))
        else:
            for r in result["results"] + result["orx_projects"]:
                mark = "ok  " if r["ok"] else "GAP "
                print(f"[{mark}] {r['kind']}: {r['detail']}")
                for g in r["gaps"]:
                    print(f"        gap: {g}")
        return 0

    if args.cmd == "projects":
        from . import registry
        store = _store(args)
        try:
            if args.action == "add":
                if not args.root:
                    ap.error("projects add needs a root path")
                row = registry.add_project(store, args.root, args.name)
                print(f"registered {row['project_key']} "
                      f"(orx={row['orx_enabled']}) {row['note']}")
            elif args.action == "remove":
                if not args.root:
                    ap.error("projects remove needs a root path or key")
                print("removed" if registry.remove_project(store, args.root)
                      else "not found")
            else:
                for r in registry.list_projects(store):
                    print(f"{r['project_key']}\t{r['name']}\t"
                          f"orx={r['orx_enabled']}")
        finally:
            store.close()
        return 0

    if args.cmd == "discover":
        store = _store(args)
        try:
            res = discovery.discover(
                store, index_file=args.index, scan_roots=args.scan or None,
                max_depth=args.max_depth)
            if args.register and res["new_projects"]:
                res["registered"] = discovery.register_discovered(
                    store, res["new_projects"])
        finally:
            store.close()
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False,
                             default=str))
        else:
            ix = res["index"]
            print(f"index: {ix['status']} {ix['detail']}")
            for n in res["notices"]:
                print(f"notice: {n}")
            if res["scan"]:
                s = res["scan"]
                tail = " (truncated: scan budget reached)" if s["truncated"] \
                    else ""
                print(f"scan: {len(s['projects'])} project(s) in "
                      f"{s['scanned_dirs']} dir(s){tail}")
            for p in res["projects"]:
                print(f"{p['project']}\t{p['name']}\t"
                      f"sources={','.join(p['sources'])}\t"
                      f"{p['data_health']['health']}")
            for r in res.get("registered", []):
                print(f"registered {r['project']} "
                      f"(action={r['action']})")
        return 0

    if args.cmd == "collect":
        from .collect import collect
        store = _store(args)
        try:
            res = collect(store, args.project or None,
                          skip_global=args.skip_global)
        finally:
            store.close()
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False,
                             default=str))
        else:
            for r in res["global"]:
                if "error" in r:
                    print(f"[ERR ] {r['source']}: {r['error']}")
                else:
                    c = r["counts"]
                    print(f"[ok  ] {r['source']}: {c}")
            for r in res["orx"]:
                if "error" in r:
                    print(f"[ERR ] orx {r['project']}: {r['error']}")
                else:
                    print(f"[ok  ] orx {r['project']}: snapshot "
                          f"{r['snapshot_id']} "
                          f"({r['counts'].get('orx_attempts', '?')} attempts)")
        return 0 if all("error" not in r for r in res["global"]) and \
            all("error" not in r for r in res["orx"]) else 1

    if args.cmd == "report":
        store = _store(args)
        try:
            key = str(Path(args.project).expanduser().resolve())
            rep = metrics.run_report(store, key, args.run)
        finally:
            store.close()
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return 0

    if args.cmd == "session":
        store = _store(args)
        try:
            rep = metrics.session_report(store, args.session_id)
        finally:
            store.close()
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return 0

    if args.cmd == "bundle":
        from . import bundle as bundle_mod
        from pathlib import Path as _P
        store = _store(args)
        try:
            dest = _P(args.dest).expanduser()
            kw = dict(title=args.title, objective=args.objective,
                      facts=args.fact, hypotheses=args.hypothesis,
                      excluded=args.excluded, next_steps=args.next)
            if args.kind == "run":
                key = str(_P(args.target).expanduser().resolve())
                out = bundle_mod.export_run_bundle(
                    store, key, args.run, dest, **kw)
            else:
                out = bundle_mod.export_session_bundle(
                    store, args.target, dest, **kw)
        finally:
            store.close()
        print(f"bundle written to {out}; run `python3 {out}/recompute.py` "
              f"to re-verify")
        return 0

    if args.cmd == "provenance":
        from .collect import provenance
        store = _store(args)
        try:
            rows = provenance(store)
        finally:
            store.close()
        for r in rows:
            err = f" ERROR={r['last_error']}" if r["last_error"] else ""
            print(f"{r['source_id']}\tlast={r['last_import_at']}{err}\t"
                  f"{json.dumps(r['counts'], ensure_ascii=False)}")
        return 0

    if args.cmd == "findings":
        from . import findings as findings_mod
        store = _store(args)
        try:
            if args.action == "list":
                rows = findings_mod.list_findings(store, args.project)
                for r in rows:
                    print(f"[{r['status']}] #{r['id']} {r['rule']} "
                          f"{r['title']} (last {r['last_seen']})")
            elif args.action == "draft":
                if not args.id:
                    ap.error("findings draft needs a finding id")
                print(json.dumps(findings_mod.draft_issue(store, int(args.id)),
                                 indent=1, ensure_ascii=False))
            elif args.action == "dismiss":
                if not args.id:
                    ap.error("findings dismiss needs a finding id")
                findings_mod.dismiss_finding(store, int(args.id),
                                             args.note or "dismissed")
                print(f"finding {args.id} dismissed")
        finally:
            store.close()
        return 0

    if args.cmd == "watch":
        from . import watch as watch_mod
        data_dir = Path(args.data_dir) if args.data_dir else None
        home = data_dir or watch_mod.Store().data_dir
        if args.action == "start":
            out = watch_mod.start(home)
        elif args.action == "stop":
            out = watch_mod.stop(home)
        elif args.action == "status":
            out = watch_mod.status(home)
        else:
            watch_mod.run_one_round(home, args.interval, once=True)
            out = {"round": "done", "state": watch_mod.status(home)}
        print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
