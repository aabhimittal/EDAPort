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
