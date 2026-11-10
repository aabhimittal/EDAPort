"""Icarus `iverilog -t null` output: `file:line: error|warning: msg`."""

import re

from .common import group, group_key

_DIAG = re.compile(r"^([^:\s]+):(\d+): (error|warning|syntax error)(?:: (.*))?$")


def parse(log: str) -> dict:
    errors, warnings = [], []
    for line in log.splitlines():
        m = _DIAG.match(line.strip())
        if not m:
            continue
        file, lineno, sev, msg = m.groups()
        d = {"file": file, "line": int(lineno), "code": None, "msg": (msg or sev).strip()}
        (warnings if sev == "warning" else errors).append(d)
    return {"errors": errors, "warnings": warnings, "warnings_grouped": group(group_key(w["msg"]) for w in warnings)}
