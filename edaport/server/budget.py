"""Size contract: fit a result into a token budget without ever silently dropping data.

Lists are trimmed largest-first (halving), long strings are clipped. Every cut is recorded
in `omitted` ({dotted.path: items_or_chars_dropped}) and the full result stays reachable
through `more_handle` (see result_page).
"""

import copy

from ..util import est_tokens

MIN_STR = 200


def _lists(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _lists(v, path + (k,))
    elif isinstance(obj, list):
        if len(obj) > 1:
            yield path, obj
        for i, v in enumerate(obj):
            yield from _lists(v, path + (i,))


def _strings(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                yield path + (k,), obj, k
            else:
                yield from _strings(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                yield path + (i,), obj, i
            else:
                yield from _strings(v, path + (i,))


def _dotted(path) -> str:
    return ".".join(str(p) for p in path)


def bound(result: dict, budget_tokens: int, more_handle: str) -> dict:
    """Return `result` unchanged if it fits, else a trimmed copy with truncated/more_handle/omitted set."""
    if est_tokens(result) <= budget_tokens:
        return result
    out = copy.deepcopy(result)
    omitted: dict[str, int] = {}
    out["truncated"], out["more_handle"], out["omitted"] = True, more_handle, omitted

    while est_tokens(out) > budget_tokens:
        cands = [(est_tokens(lst), path, lst) for path, lst in _lists(out)
                 if path and path[0] != "omitted"]
        if not cands:
            break
        _, path, lst = max(cands, key=lambda c: c[0])
        keep = len(lst) // 2
        key = _dotted(path)
        omitted[key] = omitted.get(key, 0) + len(lst) - keep
        del lst[keep:]

    while est_tokens(out) > budget_tokens:
        cands = [(len(c[k]), p, c, k) for p, c, k in _strings(out)
                 if len(c[k]) > MIN_STR and p[0] not in ("more_handle", "omitted")]
        if not cands:
            break
        n, path, container, k = max(cands, key=lambda c: c[0])
        keep = max(MIN_STR, n // 2)
        omitted[_dotted(path) + "#chars"] = omitted.get(_dotted(path) + "#chars", 0) + n - keep
        container[k] = container[k][:keep] + "...[truncated]"
    return out


def get_path(obj, dotted: str):
    """Follow a dotted path (as reported in `omitted`) into a full result."""
    for part in dotted.split(".") if dotted else []:
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            raise KeyError(f"no field {part!r} in path {dotted!r}")
    return obj
