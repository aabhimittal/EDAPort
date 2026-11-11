"""Task-level eval on the ORFS timing-closure task set (evals/orfs_tasks), with a scripted policy (no LLM).

Task: take the design through routing and end with post-route setup WNS >= 0, changing only constraint.sdc.
Policy (stand-in for an LLM agent): run synth..route, read sta_report_paths; if violated, relax the clock by
1.25x the slack deficit + 1% on a new branch and try again (max 4 flow runs).

Per task it records the spec's metrics: solved, tool calls, tool-call errors, wall and CPU time, and tokens:
  A1 = typed responses the policy actually read
  A0 = raw logs of the same calls (counterfactual: what a shell agent would have had to read)
Results are appended to a JSON file after every task, so an interrupted run resumes where it stopped.

    python evals/ab_interface/timing_tasks.py [--tasks sky130hd_gcd,asap7_gcd] [--threads 4]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from edaport.server.engine import Engine  # noqa: E402
from edaport.util import est_tokens  # noqa: E402

TASKS = ROOT / "evals/orfs_tasks"
STAGES = ("synth", "floorplan", "place", "cts", "grt", "route")
MAX_FLOWS = 4


class Meter:
    """Wraps engine.call and accumulates the per-task metrics."""

    def __init__(self, engine: Engine):
        self.e = engine
        self.calls = self.errors = self.a1 = self.a0 = 0
        self.wall = self.cpu = 0.0
        self.replayable: list[str] = []

    def call(self, tool: str, **kw) -> dict:
        res = self.e.call(tool, **kw)
        rec = self.e.log.get(res["run_id"])
        self.calls += 1
        self.errors += not res["ok"]
        self.a1 += est_tokens(res)
        if rec.get("log_ref"):
            self.a0 += len(self.e.store.get_blob(rec["log_ref"])) // 4 + 1
            self.wall += rec.get("wall_s") or 0
            self.cpu += rec.get("cpu_s") or 0
            if res["ok"] and tool == "flow_run_stage" and kw["stage"] in ("synth", "floorplan"):
                self.replayable.append(res["run_id"])
        return res


def run_task(e: Engine, name: str, threads: int, stage_timeout: float) -> dict:
    m = Meter(e)
    out = {"task": name, "solved": False, "flows": 0, "error": None}
    ref = m.call("snapshot_import", path=str(TASKS / name))["design_ref"]
    sdc = m.call("snapshot_read", design_ref=ref, path="constraint.sdc", max_lines=1000)["lines"]
    line_no = next(i for i, l in enumerate(sdc) if l.startswith("set clk_period"))
    period = out["period_start"] = float(sdc[line_no].split()[-1])
    for flow in range(1, MAX_FLOWS + 1):
        out["flows"], cur = flow, ref
        for stage in STAGES:
            r = m.call("flow_run_stage", design_ref=cur, stage=stage, threads=threads, timeout_s=stage_timeout,
                       budget_tokens=500)
            if not r["ok"]:
                out["error"] = f"{stage}: {r['error']['kind']}: {r['error']['msg'][:160]}"
                break
            cur = r["design_ref"]
        if out["error"]:
            break
        sta = m.call("sta_report_paths", design_ref=cur, k=1, threads=threads, budget_tokens=600)
        if not sta["ok"]:
            out["error"] = f"sta: {sta['error']['msg'][:160]}"
            break
        unit_ps = 1000.0 if r["units"].get("time") == "1ns" else 1.0  # SDC is in platform units, STA in ps
        out.update(period_final=period, wns_ps=sta["wns_ps"], time_unit=r["units"].get("time"))
        if sta["wns_ps"] >= 0:
            out["solved"] = True
            break
        # The flow optimizes toward whatever target it is given, so a bare "add the deficit" step undershoots.
        period = round(period * 1.01 - sta["wns_ps"] / unit_ps * 1.25, 3)
        sdc[line_no] = f"set clk_period {period}"
        ref = m.call("snapshot_write", design_ref=ref, path="constraint.sdc", content="\n".join(sdc) + "\n")["design_ref"]
    replays = [e.call("replay_run", run_id=i)["match"] for i in m.replayable[:2]]  # first flow's synth + floorplan
    out.update(calls=m.calls, tool_errors=m.errors, a1_tokens=m.a1, a0_tokens=m.a0, wall_s=round(m.wall, 1),
               cpu_s=round(m.cpu, 1), replays=len(replays), replays_matched=sum(replays), threads=threads)
    return out


def table(rows: list[dict]) -> str:
    lines = ["| task | solved | flow runs | clock start -> final | post-route WNS (ps) | tool calls (errors) | "
             "A0 tokens | A1 tokens | wall (min) | CPU (h) |", "|---|:-:|---:|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        clk = f"{r['period_start']:g} -> {r.get('period_final', r['period_start']):g} {(r.get('time_unit') or '')[1:]}"
        wns = f"{r['wns_ps']:.0f}" if r.get("wns_ps") is not None else "-"
        lines.append(f"| {r['task']} | {'yes' if r['solved'] else 'NO'} | {r['flows']} | {clk} | {wns} | "
                     f"{r['calls']} ({r['tool_errors']}) | {r['a0_tokens']:,} | {r['a1_tokens']:,} | "
                     f"{r['wall_s'] / 60:.1f} | {r['cpu_s'] / 3600:.2f} |")
    solved = [r for r in rows if r["solved"]]
    calls, errs = sum(r["calls"] for r in rows), sum(r["tool_errors"] for r in rows)
    rep, rep_ok = sum(r["replays"] for r in rows), sum(r["replays_matched"] for r in rows)
    out = "\n".join(lines) + "\n\n"
    out += f"- Solve rate: {len(solved)}/{len(rows)}\n"
    if solved:
        a0, a1 = sum(r["a0_tokens"] for r in solved) / len(solved), sum(r["a1_tokens"] for r in solved) / len(solved)
        out += (f"- Tokens per solved task: A0 {a0:,.0f} (counterfactual raw logs) vs A1 {a1:,.0f} "
                f"({1 - a1 / a0:.1%} fewer)\n"
                f"- Per solved task: {sum(r['wall_s'] for r in solved) / len(solved) / 60:.1f} min wall, "
                f"{sum(r['cpu_s'] for r in solved) / len(solved) / 3600:.2f} CPU-hours\n")
    out += f"- Tool-call error rate: {errs}/{calls} ({errs / calls:.1%})\n"
    if rep:
        out += (f"- Determinism (synth + floorplan of each task replayed at {rows[0]['threads']} threads): "
                f"{rep_ok}/{rep} matched\n")
    for r in rows:
        if r["error"]:
            out += f"- {r['task']} failed: {r['error']}\n"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", help="comma-separated task names (default: all in evals/orfs_tasks)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--stage-timeout", type=float, default=3600)
    ap.add_argument("--home", default=str(ROOT / ".edaport-eval"))
    ap.add_argument("--json", default=str(ROOT / "evals/ab_interface/timing_results.json"))
    ap.add_argument("--out", default=str(ROOT / "evals/ab_interface/timing_results.md"))
    a = ap.parse_args()
    os.environ.setdefault("EDAPORT_IMPORT_ROOTS", str(ROOT))
    names = a.tasks.split(",") if a.tasks else [f"{p}_{d}" for d in ("gcd", "riscv32i", "aes", "ibex", "jpeg")
                                                for p in ("sky130hd", "asap7")]  # small designs first
    path = Path(a.json)
    done = {r["task"]: r for r in json.loads(path.read_text())} if path.exists() else {}
    e = Engine(a.home)
    for name in names:
        if name in done:
            continue
        t0 = time.time()
        done[name] = run_task(e, name, a.threads, a.stage_timeout)
        path.write_text(json.dumps(list(done.values()), indent=1) + "\n")
        print(f"{name}: solved={done[name]['solved']} flows={done[name]['flows']} {time.time() - t0:.0f}s "
              f"{done[name]['error'] or ''}", flush=True)
        e.gc(keep_runs=1)  # ODB snapshots are large: keep only the latest run's objects between tasks
    rows = [done[n] for n in sorted(done)]
    text = table(rows)
    Path(a.out).write_text(
        "# Timing-closure task eval (scripted policy, no LLM)\n\nGenerated by "
        "`python evals/ab_interface/timing_tasks.py`. Task set: `evals/orfs_tasks` (frozen by `TASKS.sha256`), "
        "ORFS image pinned in `images/images.lock.json`.\n\n" + text)
    print("\n" + text)


if __name__ == "__main__":
    main()
