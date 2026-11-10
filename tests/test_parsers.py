"""Golden tests: real tool output in fixtures/ -> parser -> fixtures/golden/<name>.json.

Parser drift after a tool upgrade shows up here. Regenerate goldens deliberately with UPDATE_GOLDEN=1.
"""

import json
import os

import pytest

from edaport.parsers import (iverilog_lint, klayout_lyrdb, netgen_lvs, opensta_json, orfs_metrics, sby_status,
                             sim_log, vcd, verilator_lint, yosys_equiv, yosys_synth)

from .conftest import FIX


def rd(p):
    return (FIX / p).read_text()


CASES = {
    "verilator_lint_errors": lambda: verilator_lint.parse(rd("verilator/lint_errors.log")),
    "verilator_lint_unused": lambda: verilator_lint.parse(rd("verilator/lint_unused.log")),
    "iverilog_lint_errors": lambda: iverilog_lint.parse(rd("iverilog/lint_errors.log")),
    "yosys_synth_counter": lambda: yosys_synth.parse(rd("yosys/synth_counter.log"), rd("yosys/stat_counter.json")),
    "sim_icarus_fail": lambda: sim_log.parse(rd("sim/icarus_fail.log")),
    "vcd_window": lambda: vcd.signal_window(rd("sim/icarus_fail.vcd"), 116.0, before_ns=20, after_ns=10),
    "sby_bmc_fail": lambda: sby_status.parse(rd("sby/bmc_fail.log")),
    "equiv_pass": lambda: yosys_equiv.parse(rd("equiv/equiv_pass.log")),
    "equiv_fail": lambda: yosys_equiv.parse(rd("equiv/equiv_fail.log")),
    "orfs_metrics_cts": lambda: orfs_metrics.parse({"4_1_cts": json.loads(rd("orfs/4_1_cts.json"))}),
    "orfs_metrics_floorplan": lambda: orfs_metrics.parse({"2_1_floorplan": json.loads(rd("orfs/2_1_floorplan.json"))}),
    "opensta_gcd": lambda: opensta_json.parse(rd("opensta/gcd_sky130hd_report_checks.log")),
    "klayout_demo": lambda: klayout_lyrdb.parse(rd("klayout/demo.lyrdb")),
    "netgen_mismatch": lambda: netgen_lvs.parse(rd("netgen/lvs_mismatch.out"), rd("netgen/lvs_mismatch.json")),
    "netgen_match": lambda: netgen_lvs.parse(rd("netgen/lvs_match.out"), None),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden(name):
    got = json.loads(json.dumps(CASES[name]()))  # tuples -> lists, as on the wire
    golden = FIX / "golden" / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") or not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(golden.read_text())


def test_key_facts():
    """Spot-check the goldens encode what a human reads in the raw logs."""
    v = verilator_lint.parse(rd("verilator/lint_errors.log"))
    assert [e["line"] for e in v["errors"]] == [5, 5] and v["warnings_grouped"] == {"WIDTHTRUNC": 2}
    s = yosys_synth.parse(rd("yosys/synth_counter.log"), rd("yosys/stat_counter.json"))
    assert s["area_um2"] == 111.5 and s["cells"] == 47 and s["logic_depth"] == 6
    f = sby_status.parse(rd("sby/bmc_fail.log"))
    assert f["status"] == "fail" and f["failed_props"][0]["step"] == 12
    sta = opensta_json.parse(rd("opensta/gcd_sky130hd_report_checks.log"))
    assert sta["wns_ps"] == pytest.approx(-1034.482, abs=1e-3) and len(sta["paths"]) == 4  # 2 per path group
    assert {p["group"] for p in sta["paths"]} == {"core_clock", "vclk_core_clock"}
    path = sta["paths"][0]
    assert path["slack_ps"] == pytest.approx(-1030.0) and path["end"].endswith("/D")
    # stage delays add up to the data arrival time (clock latency included in the launch stage)
    assert sum(st["delay_ps"] for st in path["stages"]) == pytest.approx(path["arrival_ps"], abs=15)
    assert equiv_unproven_pairs() == 8
    d = klayout_lyrdb.parse(rd("klayout/demo.lyrdb"))
    assert d["total"] == 2 and {v["rule"] for v in d["violations"]} == {"m1.1", "m1.2"}
    m = orfs_metrics.parse({"x": json.loads(rd("orfs/2_1_floorplan.json"))})
    assert m["units"]["time"] == "1ns" and "floorplan__timing__setup__ws" in m["metrics"]
    assert not any("runtime" in k or "mem" in k for k in m["metrics"])


def equiv_unproven_pairs():
    r = yosys_equiv.parse(rd("equiv/equiv_fail.log"))
    assert r["equivalent"] is False and r["proven"] == 9 and r["total"] == 17
    return len(r["unproven"])


def test_sim_log_edge_cases():
    assert sim_log.parse("TEST a PASS @5\n$finish called")["status"] == "pass"
    assert sim_log.parse("TEST a PASS @5\n")["status"] == "error"  # never finished
    assert sim_log.parse("%Error: tb.sv:3: Assertion failed\n$finish")["status"] == "fail"
    assert sim_log.parse("hello\n$finish")["status"] == "error"  # no checks reported
