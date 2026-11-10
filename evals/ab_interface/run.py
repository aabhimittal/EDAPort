"""A/B interface eval, observation-cost arm (no LLM needed, fully reproducible).

For each task step the same tool runs once; we measure what an agent must read to get the answer:
  A0 (raw logs + shell): the tool's full stdout/stderr  -- what `cat`ing the log costs
  A1 (EDAPort):          the typed, bounded response
and check that A1 still carries the answer (a per-task predicate on the typed result).
Then every EDA run is replayed to measure the determinism rate.

The LLM-in-the-loop arm (solve rate, tokens per *solved* task) is milestone M3 and is not run here.

    python evals/ab_interface/run.py [--orfs] [--out evals/ab_interface/results.md]
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from edaport.server.engine import Engine  # noqa: E402
from edaport.util import est_tokens  # noqa: E402


def fix(src: str) -> str:
    return src.replace("(count > MAX)", "(count >= MAX)")


def tasks(e: Engine, orfs: bool):
    """Yield (task, tool, kwargs, answer_predicate). Steps may depend on earlier results via closures."""
    ctr = e.call("snapshot_import", path=str(ROOT / "examples/counter"), label="ab-main")["design_ref"]
    fixed = e.call("snapshot_write", design_ref=ctr, path="rtl/counter.v",
                   content=fix(e.store.read_file(ctr, "rtl/counter.v").decode()))["design_ref"]
    phys = e.call("snapshot_import", path=str(ROOT / "examples/physical"))["design_ref"]
    yield ("find lint issue", "lint_run", dict(design_ref=ctr),
           lambda r: r["warnings"][0]["line"] == 9)
    yield ("localize sim failure", "sim_run", dict(design_ref=ctr, tb_ref="tb/tb_counter.sv", seed=7),
           lambda r: r["first_failure"]["time_ns"] == 116.0 and r["first_failure"]["signal_window"])
    yield ("localize sim failure (verilator)", "sim_run",
           dict(design_ref=ctr, tb_ref="tb/tb_counter.sv", seed=7, simulator="verilator"),
           lambda r: r["status"] == "fail")
    yield ("formal counterexample", "formal_run", dict(design_ref=ctr, sby_ref="formal/counter.sby"),
           lambda r: r["failed_props"][0]["step"] == 12)
    yield ("prove fix", "formal_run", dict(design_ref=fixed, sby_ref="formal/counter.sby", mode="prove", depth=10),
           lambda r: r["status"] == "pass")
    syn = {}
    yield ("area + depth", "synth_run", dict(design_ref=fixed, top="counter", liberty_ref="lib/demo.lib"),
           lambda r: syn.update(r) or r["area_um2"] > 0)
    yield ("equivalence", "equiv_run", lambda: dict(gold_ref=fixed, gate_ref=syn["design_ref"], top="counter"),
           lambda r: r["equivalent"] is True)
    yield ("DRC triage", "drc_run", dict(design_ref=phys, deck="demo.drc"), lambda r: r["total"] == 2)
    yield ("LVS triage", "lvs_run", dict(design_ref=phys, layout="inv_layout.spice", schematic="inv_schematic.spice",
                                         top="inv"), lambda r: r["match"] is False and r["mismatches"])
    if orfs:
        st = {"ref": e.call("snapshot_import", path=str(ROOT / "examples/gcd_sky130hd"))["design_ref"]}
        for stage in ("synth", "floorplan", "place", "cts", "grt", "route"):
            yield (f"gcd {stage} QoR", "flow_run_stage", lambda s=stage: dict(design_ref=st["ref"], stage=s),
                   lambda r: st.update(ref=r["design_ref"]) or bool(r["metrics"]))
        yield ("gcd worst paths", "sta_report_paths", lambda: dict(design_ref=st["ref"], k=10),
               lambda r: r["wns_ps"] < 0 and r["paths"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orfs", action="store_true", help="include gcd/sky130hd ORFS + STA steps (docker)")
    ap.add_argument("--out", default=str(ROOT / "evals/ab_interface/results.md"))
    a = ap.parse_args()
    os.environ.setdefault("EDAPORT_IMPORT_ROOTS", str(ROOT))
    e = Engine(tempfile.mkdtemp(prefix="edaport-ab-"))
    rows, run_ids = [], []
    for task, tool, kwargs, answer in tasks(e, a.orfs):
        res = e.call(tool, **(kwargs() if callable(kwargs) else kwargs))
        rec = e.log.get(res["run_id"])
        raw = e.store.get_blob(rec["log_ref"]).decode(errors="replace")
        try:
            ok = bool(res["ok"] and answer(res))
        except (KeyError, IndexError, TypeError):
            ok = False
        rows.append((task, tool, len(raw) // 4 + 1, est_tokens(res), ok, res["truncated"]))
        run_ids.append(res["run_id"])
        print(f"{task:<34} A0 {rows[-1][2]:>7}  A1 {rows[-1][3]:>5}  answer={'yes' if ok else 'NO'}", flush=True)

    replays = [e.call("replay_run", run_id=r)["match"] for r in run_ids]
    a0, a1 = sum(r[2] for r in rows), sum(r[3] for r in rows)
    lines = ["| task | tool | A0 raw-log tokens | A1 EDAPort tokens | A1/A0 | answer in A1 | truncated |",
             "|---|---|---:|---:|---:|:-:|:-:|"]
    for task, tool, t0, t1, ok, tr in rows:
        lines.append(f"| {task} | `{tool}` | {t0:,} | {t1:,} | {t1 / t0:.1%} | {'yes' if ok else 'NO'} | "
                     f"{'yes' if tr else ''} |")
    lines.append(f"| **total** | | **{a0:,}** | **{a1:,}** | **{a1 / a0:.1%}** | "
                 f"{sum(r[4] for r in rows)}/{len(rows)} | |")
    summary = (f"\nObservation tokens: A1 uses {1 - a1 / a0:.1%} fewer than A0 over {len(rows)} steps.  \n"
               f"Determinism: {sum(replays)}/{len(replays)} replays matched "
               f"({sum(replays) / len(replays):.0%}), single-threaded.\n")
    text = "\n".join(lines) + "\n" + summary
    Path(a.out).write_text(
        "# A/B interface eval: observation cost (A0 raw logs vs A1 EDAPort)\n\n"
        "Generated by `make eval-ab`. Tokens are estimated as bytes/4. A0 is the full raw tool output "
        "an agent with a shell would read; A1 is the bounded typed response.\n\n"
        "Caveat: A0 counts only stdout/stderr. Where the evidence lives in a side file (DRC `.lyrdb`, sim VCD) "
        "A0 is understated, and for tiny logs A1 can be larger than A0: the win is on large logs.\n\n" + text)
    print("\n" + text)
    return 0 if all(r[4] for r in rows) and all(replays) else 1


if __name__ == "__main__":
    sys.exit(main())
