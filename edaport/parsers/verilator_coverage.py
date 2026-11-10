"""Verilator coverage.dat: `C '<key>' <count>` per coverage point."""

import re

_POINT = re.compile(r"^C '.*' (\d+)$")


def parse(text: str) -> dict:
    counts = [int(m.group(1)) for line in text.splitlines() if (m := _POINT.match(line))]
    return {"points": len(counts), "hit": sum(1 for c in counts if c > 0)}
