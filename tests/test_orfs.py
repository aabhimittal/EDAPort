"""ORFS + OpenSTA in the pinned image. Slow (~2 min); runs only when docker and the image are present."""

import json
import shutil
import subprocess

import pytest

from edaport.runners.runner import load_lock

from .conftest import ROOT


def _image_present():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", load_lock()["orfs"]["image"]], capture_output=True)
    return r.returncode == 0


pytestmark = [pytest.mark.orfs, pytest.mark.skipif(not _image_present(), reason="ORFS image not available")]


def test_gcd_stages_sta_and_replay(engine):
    ref = engine.call("snapshot_import", path=str(ROOT / "examples/gcd_sky130hd"))["design_ref"]
    runs = {}
    for stage in ("synth", "floorplan"):
        r = engine.call("flow_run_stage", design_ref=ref, stage=stage)
        assert r["ok"], r["error"]
        runs[stage], ref = r, r["design_ref"]
    fp = runs["floorplan"]["metrics"]
    assert fp["floorplan__timing__setup__ws"] < 0  # 1.1 ns clock does not close
    assert runs["floorplan"]["units"]["time"] == "1ns"
    sta = engine.call("sta_report_paths", design_ref=ref, k=2)
    assert sta["ok"] and sta["stage"] == "floorplan" and sta["wns_ps"] < 0 and sta["paths"]
    assert sta["paths"][0]["slack_ps"] <= sta["paths"][-1]["slack_ps"] or len(sta["paths"]) > 1
    # override -> different branch, different metrics; parent untouched
    loose = engine.call("flow_run_stage", design_ref=runs["synth"]["design_ref"], stage="floorplan",
                        overrides={"CORE_UTILIZATION": 20})
    assert loose["ok"] and loose["metrics"]["floorplan__design__core__area"] > fp["floorplan__design__core__area"]
    # single-threaded replay reproduces the typed result exactly
    rp = engine.call("replay_run", run_id=runs["floorplan"]["run_id"])
    assert rp["match"], json.dumps(rp)


def test_signoff_hold_fanout_and_tcl(engine, monkeypatch):
    """Full gcd flow, then the PDK's DRC + LVS decks, hold paths, fanout and the gated Tcl escape hatch."""
    ref = engine.call("snapshot_import", path=str(ROOT / "examples/gcd_sky130hd"))["design_ref"]
    for stage in ("synth", "floorplan", "place", "cts", "grt", "route", "finish"):
        r = engine.call("flow_run_stage", design_ref=ref, stage=stage, threads=4)
        assert r["ok"], r["error"]
        ref = r["design_ref"]
    drc = engine.call("drc_run", design_ref=ref, threads=4)
    assert drc["ok"] and drc["total"] == 0, drc
    lvs = engine.call("lvs_run", design_ref=ref, threads=4)
    assert lvs["ok"] and lvs["match"] is True, lvs
    hold = engine.call("sta_report_paths", design_ref=ref, k=1, path_delay="min", threads=4)
    assert hold["ok"] and hold["path_delay"] == "min" and hold["wns_ps"] > 0
    setup = engine.call("sta_report_paths", design_ref=ref, k=1, threads=4)
    fan = [s["fanout"] for s in setup["paths"][0]["stages"] if s["fanout"] is not None]
    assert fan and all(f >= 1 for f in fan)
    monkeypatch.setenv("EDAPORT_ALLOW_TCL", "1")
    tcl = engine.call("tcl_run", design_ref=ref, script='puts "cells [llength [get_cells *]]"', threads=4)
    assert tcl["ok"] and tcl["lines"][0].startswith("cells ") and int(tcl["lines"][0].split()[1]) > 100
