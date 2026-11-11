"""OpenSTA `report_checks -format json` (times in seconds) + EDAPORT_WNS/TNS/FO marker lines."""

import json
import re

_MARK = re.compile(r"^EDAPORT_(WNS|TNS) (\S+)$", re.M)
_FO = re.compile(r"^EDAPORT_FO (\S+) (\d+)$", re.M)
PS = 1e12


def _ps(v):
    return None if v is None else round(float(v) * PS, 3)


def parse(log: str) -> dict:
    marks = {k: float(v) for k, v in _MARK.findall(log) if v not in ("INF", "-INF", "inf", "-inf")}
    fanout = {net: int(n) for net, n in _FO.findall(log)}  # OpenSTA's JSON has no fanout; dumped per net
    checks = []
    i = log.find('{"checks"')
    if i >= 0:
        checks = json.JSONDecoder().raw_decode(log[i:])[0]["checks"]
    paths = []
    for c in checks:
        stages, prev = [], None
        for p in c.get("source_path", []):
            arr = p.get("arrival")
            delay = _ps(arr - prev) if arr is not None and prev is not None else _ps(arr)
            if arr is not None:
                prev = arr
            inst = p.get("instance")
            if stages and inst and stages[-1]["_inst"] == inst:
                # input pin row + output pin row of the same cell -> one stage (wire + cell delay)
                last = stages[-1]
                last.update(pin=p.get("pin", ""), net=p.get("net"),
                            delay_ps=round((last["delay_ps"] or 0) + (delay or 0), 3))
                continue
            stages.append({"_inst": inst, "cell": p.get("cell") or None, "pin": p.get("pin", ""),
                           "net": p.get("net"), "delay_ps": delay, "fanout": None})
        for st in stages:
            del st["_inst"]
            st["fanout"] = fanout.get(st["net"])
        paths.append({"slack_ps": _ps(c.get("slack")), "start": c.get("startpoint"), "end": c.get("endpoint"),
                      "clock": c.get("target_clock") or c.get("source_clock"), "group": c.get("path_group"),
                      "arrival_ps": _ps(c.get("data_arrival_time")), "required_ps": _ps(c.get("required_time")),
                      "stages": stages})
    return {"wns_ps": _ps(marks.get("WNS")), "tns_ps": _ps(marks.get("TNS")), "paths": paths}
