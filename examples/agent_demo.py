"""Closed-loop agent demo with NO LLM and no API key.

A scripted (rule-based) agent drives the EDAPort MCP server over stdio, the same way a model would,
making every decision from typed tool results only:

  1. Repair: sim fails -> try one-operator patches, each on its own branch -> keep the ones that pass
     simulation AND a formal proof -> break ties by coverage.
  2. Timing closure (--orfs): gcd/sky130hd misses its 1.1 ns clock -> relax the clock from the STA slack,
     re-run the flow on a branch, verify after routing, repeat until WNS >= 0.

It stands in for the reference ReAct agent: what an LLM adds is choosing better candidates, not a
different tool loop.

    python examples/agent_demo.py [--orfs] [--md transcript.md]
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

import anyio
from mcp.client.client import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
OPS = [">", ">=", "<", "<=", "==", "!="]
CMP = re.compile(r"\((\w+) (>=|<=|==|!=|>|<) (\w+)\)")
OUT: list[str] = []


def say(line: str = ""):
    print(line, flush=True)
    OUT.append(line)


class Agent:
    def __init__(self, client: Client):
        self.c, self.calls = client, 0

    async def tool(self, name: str, **args) -> dict:
        self.calls += 1
        return (await self.c.call_tool(name, args)).structured_content


def candidates(lines: list[str]):
    """One-operator mutations of comparisons in design code. The FORMAL block is the spec: never touch it."""
    spec = False
    for i, line in enumerate(lines):
        if "`ifdef FORMAL" in line:
            spec = True
        elif "`endif" in line:
            spec = False
        m = None if spec else CMP.search(line)
        if m:
            for op in OPS:
                if op != m.group(2):
                    patched = line[:m.start(2)] + op + line[m.end(2):]
                    yield i, f"line {i + 1}: {m.group(0)} -> ({m.group(1)} {op} {m.group(3)})", patched


async def repair(a: Agent):
    say("## 1. Repair loop (counter)\n")
    await a.tool("snapshot_import", path=str(ROOT / "examples/counter"), label="main")
    sim = await a.tool("sim_run", design_ref="main", tb_ref="tb/tb_counter.sv", seed=7, budget_tokens=600)
    ff = sim["first_failure"]
    say(f"- `sim_run` on main: **{sim['status']}**; first failure `{ff['test']}` at {ff['time_ns']} ns: {ff['msg']}")
    watch = next(s for s in ff["signal_window"] if s["signal"].endswith("count"))
    say(f"- signal window `{watch['signal']}`: " + " ".join(f"{v}@{t:g}" for t, v in watch["changes"]))
    src = (await a.tool("snapshot_read", design_ref="main", path="rtl/counter.v"))["lines"]
    cands = list(candidates(src))
    say(f"- {len(cands)} one-operator candidate patches, each on its own branch (snapshots make this free)\n")
    say("| candidate | sim | formal (prove) |")
    say("|---|---|---|")
    survivors = []
    for n, (i, desc, patched) in enumerate(cands):
        text = "\n".join(src[:i] + [patched] + src[i + 1:]) + "\n"
        ref = (await a.tool("snapshot_write", design_ref="main", path="rtl/counter.v", content=text,
                            branch=f"cand/{n}"))["design_ref"]
        s = await a.tool("sim_run", design_ref=ref, tb_ref="tb/tb_counter.sv", seed=7, budget_tokens=300)
        formal = "-"
        if s["status"] == "pass":  # only spend formal effort on candidates that survive simulation
            f = await a.tool("formal_run", design_ref=ref, sby_ref="formal/counter.sby", mode="prove", depth=12)
            formal = f["status"]
            if formal == "pass":
                survivors.append((desc, ref))
        say(f"| {desc} | {s['status']} | {formal} |")
    say(f"\n- {len(survivors)} candidates pass both. Sim and formal cannot tell them apart, so rank by coverage:")
    best = None
    for desc, ref in survivors:
        cov = (await a.tool("sim_run", design_ref=ref, tb_ref="tb/tb_counter.sv", seed=7, simulator="verilator",
                            coverage=True, budget_tokens=300))["coverage"]
        say(f"  - {desc}: {cov['hit']}/{cov['points']} coverage points hit")
        if best is None or cov["hit"] > best[0]:
            best = (cov["hit"], desc, ref)
    _, desc, ref = best
    await a.tool("snapshot_branch", design_ref=ref, label="fix")
    syn = await a.tool("synth_run", design_ref="fix", top="counter", liberty_ref="lib/demo.lib")
    eq = await a.tool("equiv_run", gold_ref="fix", gate_ref=syn["design_ref"], top="counter")
    rp = await a.tool("replay_run", run_id=syn["run_id"])
    say(f"- chosen: **{desc}** -> branch `fix`")
    say(f"- sign-off: synth {syn['cells']} cells / {syn['area_um2']} um2, netlist equivalent={eq['equivalent']}, "
        f"replay match={rp['match']}")
    say("- caveat: a tie on coverage is broken by candidate order; a weak testbench stays weak with typed tools\n")


async def close_timing(a: Agent):
    say("## 2. Timing closure loop (gcd, sky130hd, ORFS + OpenSTA)\n")
    ref = (await a.tool("snapshot_import", path=str(ROOT / "examples/gcd_sky130hd"), label="gcd"))["design_ref"]
    sdc = (await a.tool("snapshot_read", design_ref=ref, path="constraint.sdc"))["lines"]
    line_no = next(i for i, l in enumerate(sdc) if l.startswith("set clk_period"))
    period = float(sdc[line_no].split()[-1])
    say("| iteration | clock (ns) | checked after | WNS (ps) | TNS (ps) | verdict |")
    say("|---|---|---|---|---|---|")

    async def run(ref, upto):
        for stage in ("synth", "floorplan", "place", "cts", "grt", "route")[:upto]:
            r = await a.tool("flow_run_stage", design_ref=ref, stage=stage, budget_tokens=400)
            if not r["ok"]:
                raise SystemExit(f"{stage} failed: {r['error']}")
            ref = r["design_ref"]
        return ref, await a.tool("sta_report_paths", design_ref=ref, k=1, budget_tokens=600), r

    base, sta, _ = await run(ref, 2)  # cheap estimate: synth + floorplan only
    say(f"| baseline | {period:g} | floorplan | {sta['wns_ps']:.0f} | {sta['tns_ps']:.0f} | violated |")
    worst = sta["paths"][0]
    for it in range(1, 5):
        period = round(period - sta["wns_ps"] / 1000 * 1.05 + 0.005, 2)  # slack deficit + 5% margin
        sdc[line_no] = f"set clk_period {period}"
        ref = (await a.tool("snapshot_write", design_ref=ref, path="constraint.sdc", content="\n".join(sdc) + "\n",
                            branch=f"gcd-period-{period}"))["design_ref"]
        routed, sta, route = await run(ref, 6)
        met = sta["wns_ps"] >= 0
        say(f"| {it} | {period:g} | route | {sta['wns_ps']:.0f} | {sta['tns_ps']:.0f} | "
            f"{'**met**' if met else 'violated'} |")
        if met:
            break
    else:
        raise SystemExit("timing not closed in 4 iterations")
    say(f"\n- baseline worst path: `{worst['start']}` -> `{worst['end']}`, {len(worst['stages'])} cells, "
        f"arrival {worst['arrival_ps']:.0f} ps vs required {worst['required_ps']:.0f} ps")
    d = (await a.tool("snapshot_diff", a_ref=base, b_ref=routed, budget_tokens=10 ** 6))["metric_deltas"]
    say(f"- closed at **{period:g} ns** ({1000 / period:.0f} MHz) on branch `gcd-period-{period}`; "
        f"baseline snapshot untouched")
    say(f"- post-route: DRC errors {route['metrics'].get('detailedroute__route__drc_errors')}, "
        f"wirelength {route['metrics'].get('detailedroute__route__wirelength')} um; "
        f"`snapshot_diff` reports {len(d)} metric deltas vs baseline\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orfs", action="store_true", help="also run the timing-closure loop (docker + ORFS image)")
    ap.add_argument("--md", help="write the transcript as markdown (e.g. $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args()
    home = tempfile.mkdtemp(prefix="edaport-agent-")
    env = {**os.environ, "EDAPORT_IMPORT_ROOTS": str(ROOT), "PYTHONPATH": str(ROOT)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "edaport.cli", "--home", home, "serve"],
                                   env=env)
    say("# EDAPort scripted-agent demo (no LLM, no API key)\n")
    async with Client(params) as client:
        agent = Agent(client)
        await repair(agent)
        if args.orfs:
            await close_timing(agent)
        say(f"{agent.calls} MCP tool calls; every one is in the replay log (`edaport --home {home} runs`).")
    if args.md:
        with open(args.md, "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    anyio.run(main)
