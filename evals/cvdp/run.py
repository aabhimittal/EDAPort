"""CVDP baseline, no model needed: score every task's untouched context with the task's own harness.

Nothing should pass: a harness that passes the unsolved task cannot measure anything. The run also shows
whether each harness executes at all in the sim image (compile error / failed test vs. infrastructure).

    python evals/cvdp/run.py [--image IMAGE] [--tasks id1,id2]

It executes the dataset's harness code (pytest + cocotb) in containers. Resumes from baseline_results.json.
An LLM agent run plugs in at the same two calls: cvdp.import_task(...) -> agent edits via MCP -> cvdp.score(...).
"""

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1])]
import cvdp  # noqa: E402
from edaport.runners.runner import load_lock  # noqa: E402
from edaport.server.engine import Engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=load_lock()["oss-tools"]["image"])
    ap.add_argument("--tasks")
    ap.add_argument("--home", default=str(HERE / ".cache" / "edaport"))
    a = ap.parse_args()
    tasks, engine = cvdp.load(), Engine(a.home)
    out = HERE / "baseline_results.json"
    done = {r["task"]: r for r in json.loads(out.read_text())} if out.exists() else {}
    ids = a.tasks.split(",") if a.tasks else sorted(tasks)
    for tid in ids:
        if tid in done:
            continue
        done[tid] = cvdp.score(engine, cvdp.import_task(engine, tasks[tid]), tasks[tid], a.image)
        out.write_text(json.dumps([done[k] for k in sorted(done)], indent=1) + "\n")
        print(f"{tid}: {'PASS' if done[tid]['passed'] else 'fail'}  {done[tid]['detail'][:110]}", flush=True)
    rows = [done[k] for k in sorted(done)]
    meta = {t["id"]: t for t in cvdp.MANIFEST["tasks"]}
    by = collections.defaultdict(collections.Counter)
    for r in rows:
        by[meta[r["task"]]["category"]]["passed" if r["passed"] else r["detail"].split(":")[0]] += 1
    kinds = sorted({k for c in by.values() for k in c})
    lines = ["# CVDP baseline (no model)\n",
             f"Task set: `{cvdp.MANIFEST['file']}`, sha256 `{cvdp.MANIFEST['sha256']}`.\n",
             f"Untouched task context scored with each task's own harness ({len(rows)} of "
             f"{len(cvdp.MANIFEST['tasks'])} tasks run). Expected: 0 pass.\n",
             "| category | tasks | " + " | ".join(kinds) + " |", "|---|---:|" + "---:|" * len(kinds)]
    for cat in sorted(by):
        lines.append(f"| {cat} ({cvdp.CATEGORY.get(cat, '?')}) | {sum(by[cat].values())} | "
                     + " | ".join(str(by[cat][k]) for k in kinds) + " |")
    lines.append(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} pass untouched.")
    (HERE / "baseline_results.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[2:]))


if __name__ == "__main__":
    main()
