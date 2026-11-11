"""JUnit results.xml as written by cocotb: one <testcase> per test, <failure>/<error> child on failure."""

import xml.etree.ElementTree as ET


def parse(xml_text: str) -> dict:
    tests = []
    for tc in ET.fromstring(xml_text).iter("testcase"):
        bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        t = tc.get("sim_time_ns")
        tests.append({"name": tc.get("name", "?"), "status": "fail" if bad is not None else "pass",
                      "time_ns": float(t) if t else None,
                      # first line only: the rest is a traceback with scratch paths, which would break replay
                      "msg": ((bad.get("message") or bad.text or "").strip().split("\n")[0][:300]
                              if bad is not None else "")})
    first = next((t for t in tests if t["status"] == "fail"), None)
    return {"status": "fail" if first else ("pass" if tests else "error"), "tests": tests,
            "first_failure": {"test": first["name"], "time_ns": first["time_ns"], "msg": first["msg"]} if first else None}
