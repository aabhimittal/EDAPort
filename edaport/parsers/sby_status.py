"""SymbiYosys log: DONE (<STATUS>, rc=N), failed assertion/cover summaries, trace files."""

import re

_DONE = re.compile(r"DONE \((\w+), rc=\d+\)")
_FAILED = re.compile(r"summary:\s+failed assertion (\S+) at (\S+) in step (\d+)")
_UNREACHED = re.compile(r"summary:\s+unreached cover statement (\S+) at (\S+)")
_TRACE = re.compile(r"summary: counterexample trace(?: \[\S+\])?: (\S+)")
_STATUS = {"PASS": "pass", "FAIL": "fail", "UNKNOWN": "unknown", "TIMEOUT": "timeout", "ERROR": "error"}


def parse(log: str) -> dict:
    done = _DONE.findall(log)
    status = _STATUS.get(done[-1], "error") if done else "error"
    traces = _TRACE.findall(log)
    props = []
    for i, (name, loc, step) in enumerate(_FAILED.findall(log)):
        props.append({"name": name, "step": int(step), "location": loc,
                      "trace_path": traces[i] if i < len(traces) else (traces[-1] if traces else None)})
    for name, loc in _UNREACHED.findall(log):
        props.append({"name": name, "step": None, "location": loc, "trace_path": None})
    return {"status": status, "failed_props": props}

