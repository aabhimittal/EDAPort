"""edaport CLI: serve the MCP server, or drive the same engine from a shell."""

import argparse
import json
import sys

from .server.engine import Engine


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edaport", description=__doc__)
    ap.add_argument("--home", help="state dir (default $EDAPORT_HOME or ./.edaport)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server on stdio")
    p = sub.add_parser("import", help="snapshot a design directory")
    p.add_argument("path")
    p.add_argument("--label")
    p = sub.add_parser("call", help="call a tool: edaport call lint_run '{\"design_ref\": \"main\"}'")
    p.add_argument("tool")
    p.add_argument("args", nargs="?", default="{}")
    p = sub.add_parser("replay", help="re-execute a run (or --all EDA runs) and compare result digests")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--all", action="store_true", help="replay every EDA run in the log; prints the determinism rate")
    p = sub.add_parser("runs", help="list the replay log")
    p.add_argument("--last", type=int, default=20)
    p = sub.add_parser("log", help="print a run's raw log")
    p.add_argument("run_id")
    p.add_argument("--pattern")
    sub.add_parser("branches", help="list branch labels")
    p = sub.add_parser("gc", help="delete objects unreachable from branches and the replay log")
    p.add_argument("--keep-runs", type=int, help="keep only what the last N runs reference (plus branches)")
    for name, hlp in (("push", "upload a snapshot to s3://bucket/prefix"), ("pull", "download one from there")):
        p = sub.add_parser(name, help=hlp + " (needs boto3)")
        p.add_argument("ref", help="design_ref or branch label (pull: design_ref)")
        p.add_argument("url")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server.mcp_server import build_server
        build_server(Engine(a.home)).run("stdio")
        return 0
    e = Engine(a.home)
    if a.cmd == "import":
        return _out(e.call("snapshot_import", path=a.path, label=a.label))
    if a.cmd == "call":
        return _out(e.call(a.tool, **json.loads(a.args)))
    if a.cmd == "replay" and a.all:
        ids = [r["run_id"] for r in e.log.records() if r.get("log_ref") and r["ok"]]
        bad = [i for i in ids if not e.call("replay_run", run_id=i).get("match")]
        print(f"determinism: {len(ids) - len(bad)}/{len(ids)} replays matched" + (f"; mismatched: {bad}" if bad else ""))
        return 1 if bad else 0
    if a.cmd == "replay":
        res = e.call("replay_run", run_id=a.run_id)
        _out(res)
        return 0 if res.get("match") else 1
    if a.cmd == "runs":
        for r in e.log.records()[-a.last:]:
            print(f"{r['run_id']}  {r['t']}  {r['tool']:<18} ok={r['ok']!s:<5} wall={r.get('wall_s', '-')}s "
                  f"{(r.get('output_ref') or '')[:19]}")
        return 0
    if a.cmd == "log":
        page = {"next_offset": 0}
        while page["next_offset"] is not None:
            page = e.call("log_page", run_id=a.run_id, pattern=a.pattern, offset=page["next_offset"],
                          max_lines=1000, budget_tokens=10 ** 9)
            if not page["ok"]:
                return _out(page)
            print("\n".join(page["lines"]))
        return 0
    if a.cmd == "branches":
        for k, v in e.store.list_branches().items():
            print(f"{k}\t{v}")
        return 0
    if a.cmd == "gc":
        print(f"removed {e.gc(a.keep_runs)} objects")
        return 0
    if a.cmd in ("push", "pull"):
        from .snapshot import s3
        n = (s3.push if a.cmd == "push" else s3.pull)(e.store, a.ref, a.url)
        print(f"{a.cmd}: {n} objects transferred")
        return 0


def _out(res: dict) -> int:
    json.dump(res, sys.stdout, indent=2)
    print()
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
