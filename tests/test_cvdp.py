"""CVDP adapter: manifest freeze, and the harness scorer on a synthetic task in CVDP's format.

The synthetic task is ours (not from the dataset), so the pass and fail paths are tested without running
third-party harness code. Needs docker and a sim image with icarus + cocotb + pytest.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from .conftest import ROOT

sys.path.insert(0, str(ROOT / "evals/cvdp"))
import cvdp  # noqa: E402

IMAGE = os.environ.get("EDAPORT_OSS_IMAGE") or json.loads((ROOT / "images/images.lock.json").read_text())["oss-tools"]["image"]

TASK = {
    "id": "synthetic_inverter_0001",
    "context": {"docs/spec.md": "y is the bitwise inverse of a (4 bits)."},
    "harness": {
        "docker-compose.yml": ("services:\n  direct:\n    image: __OSS_SIM_IMAGE__\n    volumes:\n"
                               "      - ./src/:/src/:ro\n    working_dir: /code/rundir\n    env_file: ./src/.env\n"
                               "    command: pytest -o cache_dir=/code/rundir/.cache /src/test_runner.py -v\n"),
        "src/.env": ("SIM = icarus\nTOPLEVEL_LANG = verilog\nVERILOG_SOURCES = /code/rtl/inv.sv\n"
                     "TOPLEVEL = inv\nMODULE = test_inv\nPYTHONPATH = /src\n"),
        "src/test_runner.py": ("import os\nfrom cocotb_tools.runner import get_runner\n\n\ndef test_runner():\n"
                               "    r = get_runner(os.getenv('SIM'))\n"
                               "    r.build(sources=os.getenv('VERILOG_SOURCES').split(), hdl_toplevel=os.getenv('TOPLEVEL'),\n"
                               "            always=True, timescale=('1ns', '1ns'))\n"
                               "    r.test(hdl_toplevel=os.getenv('TOPLEVEL'), test_module=os.getenv('MODULE'))\n"),
        "src/test_inv.py": ("import cocotb\nfrom cocotb.triggers import Timer\n\n\n@cocotb.test()\n"
                            "async def inverts(dut):\n    for a in range(16):\n        dut.a.value = a\n"
                            "        await Timer(1, unit='ns')\n        assert int(dut.y.value) == (~a & 0xF)\n"),
    },
}
GOOD = "module inv(input [3:0] a, output [3:0] y); assign y = ~a; endmodule\n"
BAD = "module inv(input [3:0] a, output [3:0] y); assign y = a; endmodule\n"


def test_manifest_is_frozen():
    m = cvdp.MANIFEST
    assert len(m["tasks"]) == 92 and len({t["id"] for t in m["tasks"]}) == 92
    assert len(m["sha256"]) == 64 and m["url"].startswith("https://huggingface.co/datasets/nvidia/")
    assert {t["category"] for t in m["tasks"]} == set(cvdp.CATEGORY)


def test_fetch_rejects_wrong_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(cvdp, "CACHE", tmp_path)
    (tmp_path / cvdp.MANIFEST["file"]).write_text("tampered\n")
    with pytest.raises(RuntimeError, match="not the frozen task set"):
        cvdp.fetch()


def _sim_image_ready():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "run", "--rm", IMAGE, "pytest", "--version"], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _sim_image_ready(), reason="needs docker + a sim image with pytest and cocotb")
def test_score_pass_and_fail_paths(engine):
    missing = cvdp.score(engine, cvdp.import_task(engine, TASK), TASK, IMAGE)
    assert not missing["passed"] and missing["services"] == {"direct": 1}
    bad = cvdp.score(engine, cvdp.import_task(engine, TASK, {"rtl/inv.sv": BAD}), TASK, IMAGE)
    assert not bad["passed"] and bad["detail"].startswith("test")
    good = cvdp.score(engine, cvdp.import_task(engine, TASK, {"rtl/inv.sv": GOOD}), TASK, IMAGE)
    assert good["passed"] and good["services"] == {"direct": 0}, good
