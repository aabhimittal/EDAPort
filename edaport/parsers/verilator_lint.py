"""Verilator --lint-only output: `%Warning-CODE: file:line:col: msg` / `%Error[-CODE]: file:line:col: msg`."""

import re

from .common import group

_DIAG = re.compile(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?: (?:([^:\s]+):(\d+):(?:\d+:)? )?(.*)$")


def parse(log: str) -> dict:
    errors, warnings = [], []
    for line in log.splitlines():
        m = _DIAG.match(line)
        if not m:
            continue
        sev, code, file, lineno, msg = m.groups()
        if sev == "Error" and file is None and msg.startswith("Exiting due to"):
            continue
        d = {"file": file, "line": int(lineno) if lineno else None, "code": code, "msg": msg.strip()}
        (errors if sev == "Error" else warnings).append(d)
    return {"errors": errors, "warnings": warnings, "warnings_grouped": group(w["code"] or "OTHER" for w in warnings)}
