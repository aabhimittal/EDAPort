"""Features added after v0.1: cocotb, coverage report, Tcl gate, S3 sync, gc retention, CLI replay --all."""

import json

import pytest

from edaport.cli import main as cli
from edaport.parsers import junit_xml, opensta_json, verilator_coverage
from edaport.snapshot.store import Store

from .conftest import FIX, ROOT, need
from .test_engine import check


def test_junit_parser():
    xml = ("<testsuites><testsuite><testcase name='a'/><testcase name='b'>"
           "<failure message='count=10 exceeds MAX=9&#10;at /tmp/x/rtl.v'/></testcase></testsuite></testsuites>")
    r = junit_xml.parse(xml)
    assert r["status"] == "fail" and [t["status"] for t in r["tests"]] == ["pass", "fail"]
    assert r["first_failure"] == {"test": "b", "time_ns": None, "msg": "count=10 exceeds MAX=9"}  # no scratch path
    assert junit_xml.parse("<testsuites/>")["status"] == "error"


def test_sta_fanout_from_marker_lines():
    log = "EDAPORT_FO _029_ 3\n" + (FIX / "opensta/gcd_sky130hd_report_checks.log").read_text()
    stages = opensta_json.parse(log)["paths"][0]["stages"]
    assert any(s["fanout"] == 3 for s in stages if s["net"] == "_029_")
    assert all(s["fanout"] is None for s in stages if s["net"] != "_029_")


def test_coverage_report_parser():
    dat = ("C '\x01f\x02rtl/a.v\x01l\x0212\x01page\x02v_line/a' 0\n"
           "C '\x01f\x02rtl/a.v\x01l\x0213\x01page\x02v_line/a' 5\n"
           "C '\x01f\x02rtl/a.v\x01l\x029\x01page\x02v_toggle/a' 0\n")
    assert verilator_coverage.parse(dat) == {"points": 3, "hit": 1}
    assert verilator_coverage.report(dat) == [
        {"file": "rtl/a.v", "points": 3, "hit": 1, "uncovered": {"line": [12], "toggle": [9]}}]


@need("iverilog", "vvp")
def test_cocotb_sim_and_replay(engine, counter):
    pytest.importorskip("cocotb")
    r = check(engine.call("sim_run", design_ref="main", tb_ref="tb/test_counter.py", top="counter", seed=3))
    assert r["ok"] and r["status"] == "fail"
    assert {t["name"]: t["status"] for t in r["tests"]} == {"count_stays_in_range": "fail", "reset_clears": "pass"}
    assert r["first_failure"]["msg"] == "count=10 exceeds MAX=9"
    assert check(engine.call("replay_run", run_id=r["run_id"]))["match"]


@need("verilator")
def test_coverage_report_and_gc_keeps_referenced_blobs(engine, counter):
    r = engine.call("sim_run", design_ref="main", tb_ref="tb/tb_counter.sv", simulator="verilator", coverage=True)
    engine.gc()  # coverage_ref is only named inside the result blob; gc must still keep it
    rep = check(engine.call("coverage_report", coverage_ref=r["coverage_ref"]))
    assert rep["ok"] and rep["points"] == r["coverage"]["points"]
    rtl = next(f for f in rep["files"] if f["file"] == "rtl/counter.v")
    assert rtl["hit"] < rtl["points"] and rtl["uncovered"]
    assert not check(engine.call("coverage_report", coverage_ref=counter))["ok"]  # a manifest is not coverage


def test_tcl_passthrough_is_off_by_default(engine, counter, monkeypatch):
    monkeypatch.delenv("EDAPORT_ALLOW_TCL", raising=False)
    r = check(engine.call("tcl_run", design_ref="main", script="puts hi"))
    assert not r["ok"] and "EDAPORT_ALLOW_TCL" in r["error"]["msg"]
    monkeypatch.setenv("EDAPORT_ALLOW_TCL", "1")
    r = check(engine.call("tcl_run", design_ref="main", script="puts hi"))  # allowed, but nothing to load
    assert not r["ok"] and "no ORFS results" in r["error"]["msg"]
    assert engine.log.get(r["run_id"])["args"]["script"] == "puts hi"  # passthrough is always logged


def test_flow_signoff_needs_finish_stage(engine, counter):
    for tool in ("drc_run", "lvs_run"):
        r = check(engine.call(tool, design_ref="main"))
        assert not r["ok"] and r["error"]["kind"] == "bad_input"
    r = check(engine.call("lvs_run", design_ref="main", layout="a.spice"))
    assert not r["ok"] and "together" in r["error"]["msg"]


def test_gc_keep_runs(engine, counter):
    a = engine.call("snapshot_write", design_ref="main", path="x.txt", content="old")["design_ref"]
    engine.call("snapshot_write", design_ref="main", path="y.txt", content="new")
    engine.gc(keep_runs=1)
    assert not engine.store.has(a) and engine.store.has(counter)  # old run evicted, branch head kept


def test_s3_push_pull_roundtrip(engine, counter, tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from edaport.snapshot import s3
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="edaport-test")
        n_files = len(set(engine.store.manifest(counter)["files"].values()))
        assert s3.push(engine.store, "main", "s3://edaport-test/team") == n_files + 1
        assert s3.push(engine.store, "main", "s3://edaport-test/team") == 0  # content-addressed: nothing to resend
        other = Store(tmp_path / "other")
        assert s3.pull(other, counter, "s3://edaport-test/team") == n_files
        assert other.manifest(counter) == engine.store.manifest(counter)
        assert other.read_file(counter, "rtl/counter.v") == engine.store.read_file(counter, "rtl/counter.v")


@need("yosys")
def test_cli_replay_all(engine, counter, capsys):
    engine.call("synth_run", design_ref="main", top="counter")
    engine.call("lint_run", design_ref="main", tool="iverilog")
    assert cli(["--home", str(engine.home), "replay", "--all"]) == 0
    assert "determinism: 2/2 replays matched" in capsys.readouterr().out
