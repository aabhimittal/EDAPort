"""Yosys synthesis: `stat -json` output + log (warnings, `ltp` logic depth)."""

import json
import re

from .common import group, group_key

_LTP = re.compile(r"Longest topological path in \S+ \(length=(\d+)\)")
_WARN = re.compile(r"^(?:ABC: )?Warning: (.*)$")


def parse(log: str, stat_json: str | None) -> dict:
    out = {"cells": None, "cell_types": {}, "area_um2": None, "logic_depth": None}
    if stat_json:
        design = json.loads(stat_json).get("design", {})
        out["cells"] = design.get("num_cells")
        out["cell_types"] = design.get("num_cells_by_type", {})
        out["area_um2"] = design.get("area")  # present only when stat ran with -liberty
    depths = [int(m.group(1)) for m in _LTP.finditer(log)]
    out["logic_depth"] = max(depths) if depths else None
    out["warnings_grouped"] = group(group_key(m.group(1)) for line in log.splitlines() if (m := _WARN.match(line)))
    return out
