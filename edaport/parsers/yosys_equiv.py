"""Yosys equiv_status: proven/unproven $equiv cell counts and the unproven signal pairs."""

import re

_FOUND = re.compile(r"Found (\d+) \$equiv cells")
_COUNT = re.compile(r"Of those cells (\d+) are proven and (\d+) are unproven")
_UNPROVEN = re.compile(r"Unproven \$equiv \S+: (\S+ ?(?:\[\d+\])?) (\S+ ?(?:\[\d+\])?)")


def parse(log: str) -> dict:
    status = log[log.rfind("Executing EQUIV_STATUS"):] if "Executing EQUIV_STATUS" in log else ""
    found = _FOUND.search(status)
    if not found:
        return {"equivalent": None, "proven": 0, "total": 0, "unproven": []}
    total = int(found.group(1))
    m = _COUNT.search(status)
    proven = int(m.group(1)) if m else total
    unproven = [f"{a.strip()} <> {b.strip()}" for a, b in _UNPROVEN.findall(status)]
    return {"equivalent": proven == total, "proven": proven, "total": total, "unproven": unproven}
