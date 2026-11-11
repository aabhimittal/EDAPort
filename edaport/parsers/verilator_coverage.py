"""Verilator coverage.dat: `C '<key>' <count>` per coverage point; key fields are \\x01name\\x02value pairs."""

import re

_POINT = re.compile(r"^C '(.*)' (\d+)$")


def _points(text: str):
    for line in text.splitlines():
        if m := _POINT.match(line):
            fields = dict(kv.split("\x02", 1) for kv in m.group(1).split("\x01") if "\x02" in kv)
            yield fields, int(m.group(2))


def parse(text: str) -> dict:
    counts = [c for _, c in _points(text)]
    return {"points": len(counts), "hit": sum(1 for c in counts if c > 0)}


def report(text: str) -> list[dict]:
    """Per source file: points, hit, and the lines that still have an unhit point (by coverage type)."""
    files: dict[str, dict] = {}
    for f, count in _points(text):
        entry = files.setdefault(f.get("f", "?"), {"file": f.get("f", "?"), "points": 0, "hit": 0, "uncovered": {}})
        entry["points"] += 1
        if count > 0:
            entry["hit"] += 1
        elif f.get("l", "").isdigit():
            kind = f.get("page", "other").split("/")[0].removeprefix("v_")
            entry["uncovered"].setdefault(kind, set()).add(int(f["l"]))
    out = []
    for entry in sorted(files.values(), key=lambda e: e["file"]):
        entry["uncovered"] = {k: sorted(v) for k, v in sorted(entry["uncovered"].items())}
        out.append(entry)
    return out
