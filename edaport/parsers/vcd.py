"""Minimal VCD reader: extract value changes of top-scope signals in a time window."""

import re

_TIMESCALE_NS = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}


def signal_window(text: str, t_ns: float, before_ns: float = 50, after_ns: float = 10, max_signals: int = 16) -> list[dict]:
    """Return [{signal, changes: [(time_ns, value)]}] for signals in the first two scope levels.

    The value in effect at the window start is included as the first change, so each trace is self-contained.
    """
    header, _, body = text.partition("$enddefinitions")
    m = re.search(r"\$timescale\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*\$end", header)
    scale = int(m.group(1)) * _TIMESCALE_NS[m.group(2)] if m else 1.0
    ids, depth, scope = {}, 0, []
    for tok in re.finditer(r"\$scope\s+\S+\s+(\S+)\s+\$end|\$upscope|\$var\s+\S+\s+\d+\s+(\S+)\s+(\S+)(?:\s+\[[^\]]*\])?\s+\$end", header):
        if tok.group(1):
            scope.append(tok.group(1))
        elif tok.group(0).startswith("$upscope"):
            scope.pop()
        else:
            path = scope[1:] if scope[:1] == ["TOP"] else scope  # verilator wraps everything in TOP
            if 1 <= len(path) <= 2:
                ids.setdefault(tok.group(2), ".".join(path + [tok.group(3)]))
    lo, hi = t_ns - before_ns, t_ns + after_ns
    current, changes, now = {}, {i: [] for i in ids}, 0.0
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        if line[0] == "#":
            now = int(line[1:]) * scale
            if now > hi:
                break
            continue
        if line[0] in "bBrR":
            val, _, ident = line[1:].partition(" ")
        else:
            val, ident = line[0], line[1:]
        if ident not in ids:
            continue
        val = _num(val)
        if now <= lo:
            current[ident] = val
        else:
            changes[ident].append((round(now, 3), val))
    out = []
    for ident, name in ids.items():
        trace = ([(round(lo, 3), current[ident])] if ident in current else []) + changes[ident]
        if trace:
            out.append({"signal": name, "changes": trace})
    # Most informative first: signals that move inside the window, DUT-level before testbench-level.
    out.sort(key=lambda t: (len(t["changes"]) < 2, t["signal"].count(".") < 2))
    return out[:max_signals]


def _num(val: str) -> str:
    """Binary vector -> decimal string (x/z kept as-is) so windows stay short and readable."""
    return str(int(val, 2)) if len(val) > 1 and set(val) <= {"0", "1"} else val
