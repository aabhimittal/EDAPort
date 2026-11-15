"""ORFS timing-closure tasks in the LLM harness, driven by the scripted stand-in model. Slow (ORFS image)."""

import json
import sys

import pytest

from .conftest import ROOT
from .test_llm_agent import StubModel, last_tool_result
from .test_orfs import _image_present

sys.path.insert(0, str(ROOT / "evals/ab_interface"))
import llm_agent as la  # noqa: E402

pytestmark = [pytest.mark.orfs, pytest.mark.skipif(not _image_present(), reason="ORFS image not available")]
STAGES = ("synth", "floorplan", "place", "cts", "grt", "route")


def gcd_task():
    (task,) = [t for t in la.timing_tasks(None) if t.id == "timing_sky130hd_gcd"]
    return task


def test_task_set_matches_the_frozen_tasks():
    tasks = la.timing_tasks(None)
    assert len(tasks) == 10 and all(t.a0_image.startswith("openroad/orfs@sha256:") for t in tasks)
    assert "at most 3.005 ns" in gcd_task().prompt  # 1.25 x the 2.404 ns the scripted policy reached


def test_a1_scripted_timing_closure(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))

    def write_sdc(messages):
        lines = json.loads(last_tool_result(messages))["lines"]
        text = "\n".join("set clk_period 2.6" if l.startswith("set clk_period") else l for l in lines) + "\n"
        return [("snapshot_write", {"design_ref": "main", "path": "constraint.sdc", "content": text})]

    def stage(name):  # chain on the design_ref the previous call returned
        return lambda messages: [("flow_run_stage", {"design_ref": json.loads(last_tool_result(messages))["design_ref"],
                                                     "stage": name, "threads": 4, "budget_tokens": 400})]

    def publish(messages):
        return [("snapshot_branch", {"design_ref": json.loads(last_tool_result(messages))["design_ref"],
                                     "label": "solution"})]

    stub = StubModel([[("snapshot_read", {"design_ref": "main", "path": "constraint.sdc"})], write_sdc,
                      *[stage(s) for s in STAGES], publish, []])
    task = gcd_task()
    (res,) = la.run(stub, [task], ["A1"], "stub-model", "medium", str(tmp_path), image="unused")
    assert res.solved and res.tool_errors == 0 and res.tool_calls == 9, res
    # Same routed design, tighter target: the check reads the clock the design was routed at, so it fails.
    engine = la.Engine(tmp_path / f"{task.id}-A1")
    ref, start = engine.store.resolve("solution"), engine.store.resolve("main")
    assert la.timing_check(engine, ref, start, limit=3.005) and not la.timing_check(engine, ref, start, limit=2.5)
    # Editing the SDC after routing does not fool it either.
    late = engine.store.write_file(ref, "constraint.sdc", b"set clk_period 1.0\n")
    assert la.timing_check(engine, late, start, limit=3.005)  # still judged on the routed 2.6 ns
    assert not la.timing_check(engine, engine.store.write_file(ref, "config.mk", b"# changed\n"), start, 3.005)
    assert not la.timing_check(engine, start, start, 3.005)  # never routed


def test_a0_scripted_timing_closure_in_orfs_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))
    stub = StubModel([
        [("shell", {"command": "sed -i 's/^set clk_period .*/set clk_period 2.6/' constraint.sdc && grep clk_period constraint.sdc"})],
        [("shell", {"command": f"{la.ORFS_MAKE} route > flow.log 2>&1; echo rc=$?; tail -2 flow.log"})],
        [],
    ])
    (res,) = la.run(stub, [gcd_task()], ["A0"], "stub-model", "medium", str(tmp_path), image="unused")
    assert "rc=0" in stub.requests[2]["messages"][4]["content"][0]["content"]
    assert res.solved and res.tool_calls == 2 and res.tool_errors == 0, res
