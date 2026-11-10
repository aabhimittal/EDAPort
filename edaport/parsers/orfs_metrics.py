"""ORFS per-step metrics JSON (logs/<platform>/<design>/<variant>/<step>.json).

Curated to quality-of-result keys; runtime/memory keys are dropped so results replay deterministically.
"""

import re

_KEEP = re.compile(
    r"timing__(setup|hold)__(ws|tns)$|design__instance__(count|area|utilization)(__stdcell)?$|"
    r"route__wirelength(__estimated)?$|drc_errors$|antenna__violating__(nets|pins)$|power__total$|"
    r"clock__skew__(setup|hold)$|design__(io|die__area|core__area)$|timing__drv__max_(slew|cap|fanout)$|"
    r"route__overflow$|flow__errors__count$|timing__fmax$"
)
_VOLATILE = re.compile(r"(runtime|__run_s|_s$|mem|__iter:)")
_UNIT = re.compile(r"run__flow__platform__(\w+)_units$")


def parse(step_metrics: dict[str, dict]) -> dict:
    """step_metrics: {step_name: metrics_json} for the steps of one stage, in order."""
    metrics, units, warnings = {}, {}, {}
    for _, data in sorted(step_metrics.items()):
        for k, v in data.items():
            if m := _UNIT.match(k):
                units[m.group(1)] = v
            elif k.startswith("flow__warnings__count:"):
                code = k.split(":", 1)[1]
                warnings[code] = warnings.get(code, 0) + int(v)
            elif _KEEP.search(k) and not _VOLATILE.search(k):
                metrics[k] = v  # later steps of the stage override earlier ones
    return {"metrics": metrics, "units": units, "warnings_grouped": warnings}
