"""Integration: real tools on the example designs. Every response is validated against its JSON schema."""

import jsonschema
import pytest

from edaport.models import RESULT_MODELS

from .conftest import ROOT, need

SCHEMAS = {name: model.model_json_schema() for name, model in RESULT_MODELS.items()}


def check(res):
    jsonschema.validate(res, SCHEMAS[res["tool"]])
    return res


@need("verilator", "iverilog")
def test_lint(engine, counter):
    r = check(engine.call("lint_run", design_ref="main"))
    assert r["ok"] and r["warnings_grouped"] == {"UNUSEDSIGNAL": 1}
    assert r["warnings"][0]["file"] == "rtl/counter.v" and r["warnings"][0]["line"] == 9
    assert check(engine.call("lint_run", design_ref="main", tool="iverilog"))["errors"] == []
    bad = check(engine.call("lint_run", design_ref="main", tool="spyglass"))
    assert not bad["ok"] and bad["error"]["kind"] == "bad_input"


@need("iverilog", "vvp")
def test_sim_fail_then_fix_on_branch(engine, counter):
    r = check(engine.call("sim_run", design_ref="main", tb_ref="tb/tb_counter.sv", seed=7))
    assert r["status"] == "fail" and r["first_failure"]["test"] == "range"
    assert r["first_failure"]["time_ns"] == 116.0
    window = {s["signal"]: s["changes"] for s in r["first_failure"]["signal_window"]}
    assert ["115.0", "10"] in [[f"{t}", v] for t, v in window["tb.dut.count"]]
    src = engine.store.read_file("main", "rtl/counter.v").decode().replace("(count > MAX)", "(count >= MAX)")
    w = check(engine.call("snapshot_write", design_ref="main", path="rtl/counter.v", content=src, branch="fix"))
    ok = check(engine.call("sim_run", design_ref="fix", tb_ref="tb/tb_counter.sv", seed=7))
    assert ok["status"] == "pass" and ok["parent_ref"] == w["design_ref"]
    assert engine.store.resolve("main") == counter  # main never moved


@need("verilator")
def test_verilator_sim_with_coverage(engine, counter):
    r = check(engine.call("sim_run", design_ref="main", tb_ref="tb/tb_counter.sv", simulator="verilator",
                          coverage=True, seed=7))
    assert r["status"] == "fail" and r["first_failure"]["test"] == "range"
    assert r["coverage"]["points"] > 0 and r["coverage_ref"].startswith("sha256:")


@need("yosys")
def test_synth_equiv_replay(engine, counter):
    s = check(engine.call("synth_run", design_ref="main", top="counter", liberty_ref="lib/demo.lib"))
    assert s["ok"] and s["area_um2"] == 111.5 and s["logic_depth"] == 6 and s["design_ref"] != counter
    assert s["netlist_ref"] == f"{s['design_ref']}:build/synth/counter.v"
    assert engine.store.manifest(s["design_ref"])["meta"]["metrics"]["synth"]["area_um2"] == 111.5
    e = check(engine.call("equiv_run", gold_ref="main", gate_ref=s["design_ref"], top="counter"))
    assert e["equivalent"] is True and e["proven"] == e["total"] > 0
    rp = check(engine.call("replay_run", run_id=s["run_id"]))
    assert rp["match"] and rp["output_ref_match"]
    bad = check(engine.call("synth_run", design_ref="main", top="counter; rm -rf /"))
    assert not bad["ok"] and bad["error"]["kind"] == "bad_input"


@need("sby", "yosys-smtbmc", "z3")
def test_formal(engine, counter):
    r = check(engine.call("formal_run", design_ref="main", sby_ref="formal/counter.sby"))
    assert r["status"] == "fail" and r["failed_props"][0]["name"] == "counter.count_in_range"
    assert r["failed_props"][0]["step"] == 12 and engine.store.has(r["failed_props"][0]["trace_ref"])
    src = engine.store.read_file("main", "rtl/counter.v").decode().replace("(count > MAX)", "(count >= MAX)")
    fix = engine.call("snapshot_write", design_ref="main", path="rtl/counter.v", content=src)["design_ref"]
    p = check(engine.call("formal_run", design_ref=fix, sby_ref="formal/counter.sby", mode="prove", depth=8))
    assert p["status"] == "pass" and p["mode"] == "prove" and p["depth"] == 8


@need("klayout", "netgen-lvs")
def test_drc_lvs(engine):
    ref = engine.call("snapshot_import", path=str(ROOT / "examples/physical"))["design_ref"]
    d = check(engine.call("drc_run", design_ref=ref, deck="demo.drc"))
    assert d["total"] == 2 and d["violations"][0]["bbox_um"] is not None
    lv = check(engine.call("lvs_run", design_ref=ref, layout="inv_layout.spice", schematic="inv_schematic.spice",
                           top="inv"))
    assert lv["match"] is False and "nmos.gate" in lv["mismatches"][0]


@need("iverilog", "vvp")
def test_truncation_paging_and_raw_log(engine, counter):
    r = check(engine.call("sim_run", design_ref="main", tb_ref="tb/tb_counter.sv", seed=7, budget_tokens=250))
    assert r["truncated"] and r["more_handle"] == r["run_id"]
    assert "first_failure.signal_window" in r["omitted"]
    page = check(engine.call("result_page", more_handle=r["more_handle"], path="first_failure.signal_window",
                             limit=500))
    assert page["total"] == len(r["first_failure"]["signal_window"]) + r["omitted"]["first_failure.signal_window"]
    log = check(engine.call("log_page", run_id=r["run_id"], pattern=r"TEST \w+ FAIL"))
    assert log["lines"] == ["5: TEST range FAIL @116 count=10 exceeds MAX=9"]
    assert not check(engine.call("log_page", run_id=r["run_id"], pattern="("))["ok"]


def test_input_validation(engine, counter):
    for tool, args in [("snapshot_write", {"design_ref": "main", "path": "../../etc/x", "content": "x"}),
                       ("snapshot_write", {"design_ref": "main", "path": "build/x", "content": "x"}),
                       ("snapshot_import", {"path": "/etc"}),
                       ("snapshot_read", {"design_ref": "nope"}),
                       ("flow_run_stage", {"design_ref": "main", "stage": "place", "overrides": {"X": "1; rm"}}),
                       ("flow_run_stage", {"design_ref": "main", "stage": "dance"}),
                       ("replay_run", {"run_id": "r-9999"})]:
        r = check(engine.call(tool, **args))
        assert not r["ok"] and r["error"]["kind"] == "bad_input", (tool, args)


def test_missing_tool_is_typed_error(engine, counter, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    r = check(engine.call("lint_run", design_ref="main"))
    assert not r["ok"] and r["error"]["kind"] == "tool_missing"


def test_replay_log_records(engine, counter):
    engine.call("snapshot_read", design_ref="main")
    recs = engine.log.records()
    assert [r["run_id"] for r in recs] == ["r-0001", "r-0002"]
    assert all({"tool", "args", "result_digest", "result_ref", "t"} <= set(r) for r in recs)
    assert recs[1]["args"]["design_ref"] == counter  # labels are resolved before logging
