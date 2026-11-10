"""End-to-end demo: an MCP client drives the EDAPort server (stdio) through a debug -> fix -> sign-off loop.

    python examples/demo.py            # counter design: lint, sim, formal, fix on a branch, synth, equiv, replay
    python examples/demo.py --orfs     # + gcd on sky130hd through ORFS stages and OpenSTA (needs docker)

For each step it prints the typed response size next to the raw tool output it replaces.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import anyio
from mcp.client.client import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from edaport.server.engine import Engine  # noqa: E402  (read-only: raw log sizes for the comparison)
from edaport.util import est_tokens  # noqa: E402


class Demo:
    def __init__(self, client: Client, home: str):
        self.c, self.engine = client, Engine(home)

    async def step(self, title: str, tool: str, **args) -> dict:
        res = (await self.c.call_tool(tool, args)).structured_content
        rec = self.engine.log.get(res["run_id"])
        raw = self.engine.store.get_blob(rec["log_ref"]).decode(errors="replace") if rec.get("log_ref") else ""
        size = f"typed {est_tokens(res):>5} tok" + (f" | raw log {len(raw) // 4 + 1:>6} tok" if raw else "")
        print(f"\n== {title}  [{tool} -> {res['run_id']}]  ({size})")
        show = {k: v for k, v in res.items()
                if k not in ("run_id", "tool", "parent_ref", "more_handle", "error") and v not in (None, {}, [], False)}
        if res.get("error"):
            show["error"] = res["error"]
        text = json.dumps(show, indent=1)
        print(text if len(text) < 1800 else text[:1800] + "\n ...")
        return res


async def counter_flow(d: Demo):
    ex = str(ROOT / "examples" / "counter")
    await d.step("Import the design as a content-addressed snapshot", "snapshot_import", path=ex, label="main")
    await d.step("Lint", "lint_run", design_ref="main")
    sim = await d.step("Simulate (seed 7): the counter overshoots MAX", "sim_run", design_ref="main",
                 tb_ref="tb/tb_counter.sv", seed=7, budget_tokens=700)
    await d.step("Formal BMC finds the same bug with a counterexample trace", "formal_run", design_ref="main",
                 sby_ref="formal/counter.sby")
    src = (await d.c.call_tool("snapshot_read", {"design_ref": "main", "path": "rtl/counter.v"})).structured_content
    fixed = "\n".join(src["lines"]).replace("(count > MAX)", "(count >= MAX)") + "\n"
    await d.step("Fix on a branch (main is untouched)", "snapshot_write", design_ref="main",
                       path="rtl/counter.v", content=fixed, branch="fix")
    await d.step("Re-simulate the fix", "sim_run", design_ref="fix", tb_ref="tb/tb_counter.sv", seed=7)
    await d.step("Prove the property (k-induction)", "formal_run", design_ref="fix", sby_ref="formal/counter.sby",
                 mode="prove", depth=10)
    syn = await d.step("Synthesize to the demo liberty", "synth_run", design_ref="fix", top="counter",
                       liberty_ref="lib/demo.lib")
    await d.step("Gate netlist vs fixed RTL", "equiv_run", gold_ref="fix", gate_ref=syn["design_ref"], top="counter")
    await d.step("Gate netlist vs the ORIGINAL RTL (must not be equivalent)", "equiv_run", gold_ref="main",
                 gate_ref=syn["design_ref"], top="counter", budget_tokens=300)
    await d.step("What changed between main and the synthesized fix?", "snapshot_diff", a_ref="main",
                 b_ref=syn["design_ref"])
    await d.step("Replay the synthesis run: same inputs -> same digest", "replay_run", run_id=syn["run_id"])
    await d.step("Grep the raw evidence behind the sim failure", "log_page", run_id=sim["run_id"],
                 pattern="TEST|finish")
    await d.step("Roll branch 'fix' back to main (previous_ref allows undo)", "snapshot_restore",
                 design_ref="main", label="fix")


async def orfs_flow(d: Demo):
    ex = str(ROOT / "examples" / "gcd_sky130hd")
    r = await d.step("Import gcd (sky130hd)", "snapshot_import", path=ex, label="gcd")
    ref = r["design_ref"]
    for stage in ("synth", "floorplan", "place", "cts", "grt", "route"):
        r = await d.step(f"ORFS {stage}", "flow_run_stage", design_ref=ref, stage=stage, budget_tokens=500)
        if not r["ok"]:
            return
        ref = r["design_ref"]
    await d.step("Worst setup paths after route", "sta_report_paths", design_ref=ref, k=3, budget_tokens=1500)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orfs", action="store_true", help="also run gcd through ORFS + OpenSTA (docker)")
    ap.add_argument("--home", help="state dir (default: fresh temp dir)")
    a = ap.parse_args()
    home = a.home or tempfile.mkdtemp(prefix="edaport-demo-")
    env = {**os.environ, "EDAPORT_IMPORT_ROOTS": str(ROOT), "PYTHONPATH": str(ROOT)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "edaport.cli", "--home", home, "serve"],
                                   env=env)
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print(f"EDAPort MCP server: {len(tools)} tools; state in {home}")
        d = Demo(client, home)
        await counter_flow(d)
        if a.orfs:
            await orfs_flow(d)
    print(f"\nReplay log: {home}/runs.jsonl  (try: edaport --home {home} runs)")


if __name__ == "__main__":
    anyio.run(main)
