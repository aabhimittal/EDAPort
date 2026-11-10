"""Netgen LVS: final verdict from the text report, mismatch details from the -json comparison file."""

import json


def parse(report: str, comp_json: str | None) -> dict:
    if "Netlists do not match" in report or "Circuits do not match" in report:
        match = False
    elif "Circuits match uniquely" in report or "Netlists match uniquely" in report:
        match = True
    else:
        match = None
    mismatches = []
    for circuit in json.loads(comp_json) if comp_json else []:
        name = "/".join(dict.fromkeys(circuit.get("name", [])))
        dl, ds = (circuit.get("devices") or [[], []])[:2]
        if sorted(map(tuple, dl)) != sorted(map(tuple, ds)):
            mismatches.append(f"{name}: device counts layout={_fmt(dl)} schematic={_fmt(ds)}")
        nl, ns = circuit.get("nets", [None, None])[:2]
        if nl != ns:
            mismatches.append(f"{name}: net count layout={nl} schematic={ns}")
        for side_l, side_s in _pairs(circuit.get("badnets")):
            mismatches.append(f"{name}: net group layout[{_group(side_l)}] vs schematic[{_group(side_s)}]")
        for side_l, side_s in _pairs(circuit.get("badelements")):
            mismatches.append(f"{name}: device group layout[{_group(side_l)}] vs schematic[{_group(side_s)}]")
    return {"match": match, "mismatches": mismatches}


def _pairs(groups):
    for g in groups or []:
        if isinstance(g, list) and len(g) == 2:
            yield g[0], g[1]


def _fmt(devs):
    return ",".join(f"{d[0]}x{d[1]}" for d in devs)


def _group(side):
    parts = []
    for entry in side or []:
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list):
            conns = ",".join(f"{c[0]}.{c[1]}={c[2]}" for c in entry[1] if isinstance(c, list) and len(c) == 3)
            parts.append(f"{entry[0]}({conns})")
    return "; ".join(parts)
