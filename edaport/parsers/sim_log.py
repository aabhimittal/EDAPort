"""Simulation transcript (Icarus vvp / Verilator binary).

Test convention, one line per check printed by the testbench:
    TEST <name> PASS|FAIL @<time_ns> [message]
Simulator-level failures ($error/$fatal/assertion messages) count as failures even without TEST lines.
"""

import re

_TEST = re.compile(r"^TEST (\S+) (PASS|FAIL)(?: @(\d+(?:\.\d+)?))?\s*(.*)$")
_FATAL = re.compile(r"^(?:%Error|%Fatal|ERROR:|FATAL:|Error:|Fatal:)|\$(?:fatal|error)\b|Assertion failed", re.I)
_FINISHED = re.compile(r"\$finish|Verilog \$finish|\$stop")


def parse(log: str) -> dict:
    tests, fatal = [], None
    for raw in log.splitlines():
        line = raw.strip()
        if m := _TEST.match(line):
            name, st, t, msg = m.groups()
            tests.append({"name": name, "status": st.lower(), "time_ns": float(t) if t else None, "msg": msg.strip()})
        elif fatal is None and _FATAL.search(line):
            fatal = line
    finished = bool(_FINISHED.search(log))
    first = next((t for t in tests if t["status"] == "fail"), None)
    if first:
        status = "fail"
        first_failure = {"test": first["name"], "time_ns": first["time_ns"], "msg": first["msg"]}
    elif fatal:
        status = "fail"
        first_failure = {"test": "<simulator>", "time_ns": None, "msg": fatal}
    elif tests and finished:
        status, first_failure = "pass", None
    else:
        status, first_failure = "error", None  # no checks reported or the sim never finished
    return {"status": status, "tests": tests, "first_failure": first_failure}
