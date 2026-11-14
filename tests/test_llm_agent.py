"""M3 agent harness, driven by a scripted stand-in for the model (no API key, no network).

The stub returns the content blocks a real response would carry; everything else is the real loop,
the real tools and the real task check.
"""

import shutil
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from .conftest import ROOT, need

sys.path.insert(0, str(ROOT / "evals/ab_interface"))
import llm_agent as la  # noqa: E402

FIX = "(count > MAX)", "(count >= MAX)"


class StubModel:
    """client.beta.messages.create(...) -> next scripted response; records every request."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        step = self.script.pop(0)
        step = step(kw["messages"]) if callable(step) else step
        blocks = [NS(type="tool_use", id=f"tu{len(self.requests)}_{i}", name=n, input=a) for i, (n, a) in enumerate(step)]
        return NS(content=blocks or [NS(type="text", text="done")], stop_reason="tool_use" if blocks else "end_turn",
                  usage=NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=5))


def last_tool_result(messages):
    return messages[-1]["content"][0]["content"]


@need("iverilog", "vvp", "sby", "yosys")
def test_a1_scripted_repair_is_solved_and_accounted(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))

    def write_fix(messages):  # uses what the previous tool call returned, like a model would
        import json
        src = "\n".join(json.loads(last_tool_result(messages))["lines"]).replace(*FIX) + "\n"
        return [("snapshot_write", {"design_ref": "main", "path": "rtl/counter.v", "content": src, "branch": "solution"})]

    stub = StubModel([
        [("snapshot_read", {"design_ref": "main", "path": "rtl/counter.v"})],
        write_fix,
        [("sim_run", {"design_ref": "solution", "tb_ref": "tb/tb_counter.sv"}), ("no_such_tool", {})],  # parallel calls
        [("snapshot_import", {"path": str(ROOT / "examples/physical"), "label": "solution"})],  # hidden tool, by name
        [],
    ])
    (res,) = la.run(stub, [la.counter_repair()], ["A1"], "stub-model", "medium", str(tmp_path), image="unused")
    # solved proves the hidden snapshot_import did not run: it would have moved `solution` to another design
    assert res.solved and res.stop == "end_turn" and res.turns == 5
    assert res.tool_calls == 5 and res.tool_errors == 2  # unknown and hidden tools are error results, not crashes
    assert res.input_tokens == 5 * 105 and res.output_tokens == 50
    assert "unknown tool snapshot_import" in stub.requests[4]["messages"][8]["content"][0]["content"]
    req = stub.requests[2]
    assert req["fallbacks"] == "default" and req["output_config"] == {"effort": "medium"}
    names = {t["name"] for t in req["tools"]}
    assert "sim_run" in names and "snapshot_import" not in names
    # both results of the parallel turn go back in ONE user message, errors flagged
    results = stub.requests[3]["messages"][6]["content"]  # the list is shared, so index by position
    assert [r["is_error"] for r in results] == [False, True]


@need("iverilog", "vvp", "sby", "yosys")
def test_a1_unsolved_when_agent_stops_without_fixing(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))
    (res,) = la.run(StubModel([[]]), [la.counter_repair()], ["A1"], "stub-model", "medium", str(tmp_path), "unused")
    assert not res.solved and res.tool_calls == 0


def test_turn_limit_and_refusal_stop_the_loop():
    spin = StubModel([[("shell", {"command": "true"})]] * 5)
    res = la.Result("t", "A0")
    la.run_agent(spin, "m", "low", "sys", "go", [la.SHELL_TOOL], lambda n, a: ("ok", False), res, max_turns=3)
    assert res.stop == "turn_limit" and res.turns == 3
    refuse = NS(beta=NS(messages=NS(create=lambda **kw: NS(
        content=[], stop_reason="refusal", usage=NS(input_tokens=1, output_tokens=0)))))
    res = la.Result("t", "A0")
    la.run_agent(refuse, "m", "low", "sys", "go", [la.SHELL_TOOL], lambda n, a: ("ok", False), res)
    assert res.stop == "refusal" and res.tool_calls == 0


def test_results_table():
    rs = [la.Result("a", "A0", solved=True, tool_calls=4, tool_errors=1, input_tokens=900, output_tokens=100, wall_s=120),
          la.Result("b", "A0", tool_calls=4, input_tokens=1000), la.Result("a", "A1")]
    rows = la.table(rs).splitlines()[2:]
    assert rows[0] == "| A0 | 2 | 1/2 | 2,000 | 1/8 (12.5%) | 2.0 |"  # the failed task's tokens count against the solve
    assert rows[1] == "| A1 | 1 | 0/1 | - | - | - |"


def _oss_image():
    import json
    return json.loads((ROOT / "images/images.lock.json").read_text())["oss-tools"]["image"]


def _docker_image_ready():
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "image", "inspect", _oss_image()], capture_output=True).returncode == 0


@need("iverilog", "vvp", "sby", "yosys")
@pytest.mark.skipif(not _docker_image_ready(), reason="A0 shell runs in the pinned oss-tools image")
def test_a0_scripted_shell_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))
    stub = StubModel([
        [("shell", {"command": "iverilog -g2012 -o /tmp/s -s tb rtl/counter.v tb/tb_counter.sv && vvp -n /tmp/s"})],
        [("shell", {"command": "sed -i 's/(count > MAX)/(count >= MAX)/' rtl/counter.v && grep -c '>= MAX' rtl/counter.v"})],
        [("shell", {"command": "exit 3"})],
        [],
    ])
    (res,) = la.run(stub, [la.counter_repair()], ["A0"], "stub-model", "medium", str(tmp_path), _oss_image())
    assert res.solved and res.tool_calls == 3 and res.tool_errors == 1
    first = stub.requests[1]["messages"][2]["content"][0]["content"]
    assert "TEST range FAIL" in first and "[exit code 0]" in first  # the raw log reaches the model
