"""KLayout report database (.lyrdb XML): group markers per rule category with count and first bbox."""

import re
import xml.etree.ElementTree as ET

_COORD = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def _bbox(value: str):
    pts = [(float(x), float(y)) for x, y in _COORD.findall(value)]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def parse(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    desc = {c.findtext("name"): c.findtext("description") for c in root.iter("category")}
    rules: dict[str, dict] = {}
    for item in root.find("items") if root.find("items") is not None else []:
        rule = (item.findtext("category") or "").strip("'")
        mult = int(item.findtext("multiplicity") or 1)
        r = rules.setdefault(rule, {"rule": rule, "description": desc.get(rule),
                                    # shortcut: layer = rule-name prefix ("m1.2" -> "m1"); decks with other naming get None
                                    "layer": rule.split(".")[0] if "." in rule else None,
                                    "bbox_um": None, "count": 0})
        r["count"] += mult
        if r["bbox_um"] is None:
            r["bbox_um"] = _bbox(item.findtext("values/value") or "")
    violations = sorted(rules.values(), key=lambda r: (-r["count"], r["rule"]))
    return {"violations": violations, "total": sum(r["count"] for r in violations)}
